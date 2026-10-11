#!/usr/bin/env python3
# dispatch.py — OP-15 dispatch layer: verbs get hands (DESIGN-dispatch-handlers.md)
#
# Purity rule: reduction is deterministic, dispatch is effectful, never mixed.
# ActionResolver.reduce() stays pure; every side effect lives in a handler
# behind the Dispatcher, reached only through Action.__call__.

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ── outbox -------------------------------------------------------------
DEFAULT_OUTBOX = Path(__file__).resolve().parent / "outbox.jsonl"

def _outbox_line(outbox: Path, kind: str, action: Any, **extra) -> str:
    payload = {
        "kind": kind,
        "verb": action.verb,
        "target": action.target,
        "owner": action.owner,
        "authority": action.authority,
        "source_decision": action.source_decision,
        "reason": action.reason,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }
    line = json.dumps(payload, sort_keys=True)
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with open(outbox, "a") as f:
        f.write(line + "\n")
    return line


# ── result & context ---------------------------------------------------
@dataclass
class HandlerResult:
    ok: bool
    message: str = ""
    artifact: str = ""   # e.g. written spec path / outbox path


@dataclass
class DispatchContext:
    adapter: Any = None
    subject: Any = None
    candidates: Any = None
    action: Any = None            # the Action being dispatched
    outbox: Path | None = None    # append-only JSONL — the ONLY side effect in v1
    close_loop: bool = False      # recheck handler: arm a self-closing spec


Handler = Callable[[DispatchContext], HandlerResult]


# ── default handlers (domain-blind) ------------------------------------
def _h_dispatch(ctx: DispatchContext) -> HandlerResult:
    line = _outbox_line(ctx.outbox or DEFAULT_OUTBOX, "dispatch", ctx.action)
    return HandlerResult(ok=True, message=f"executed dispatch on {ctx.action.target}",
                         artifact=line)


def _h_escalate(ctx: DispatchContext) -> HandlerResult:
    line = _outbox_line(ctx.outbox or DEFAULT_OUTBOX, "escalation", ctx.action)
    return HandlerResult(ok=True, message=f"escalated to Ross: {ctx.action.reason or 'no reason'}",
                         artifact=line)


def _h_flag(ctx: DispatchContext) -> HandlerResult:
    line = _outbox_line(ctx.outbox or DEFAULT_OUTBOX, "flag", ctx.action)
    return HandlerResult(ok=True, message=f"flagged for review: {ctx.action.reason or 'no reason'}",
                         artifact=line)


def _h_recheck(ctx: DispatchContext) -> HandlerResult:
    """Auto-arm the loop: write the next spec (count+1, due from the action).

    The human/agent still schedules the cron; the ARM — writing the spec the
    driver executes — is now a handler. arm_recheck() is deterministic: the
    spec IS the input, so the loop stays byte-deterministic at every step.
    """
    from recheck_driver import arm_recheck
    a = ctx.action
    count = int((a.raw or {}).get("recheck_count", 0))
    due = a.due.isoformat(timespec="minutes") if a.due else \
        datetime.now(timezone.utc).isoformat(timespec="minutes")
    slug = (a.source_decision or a.target or "recheck").replace("|", "_").replace("/", "_")
    spec_path = Path(__file__).resolve().parent / "recheck_specs" / f"{slug}.json"
    arm_recheck(spec_path, adapter=(ctx.adapter.name if ctx.adapter else "qc-disposition"),
                subject=ctx.subject, candidates=ctx.candidates,
                source_key=a.source_decision, count=count, due=due,
                close_loop=ctx.close_loop)
    return HandlerResult(ok=True, message=f"armed recheck #{count + 1} at {due}",
                         artifact=str(spec_path))


DEFAULT_HANDLERS: dict[str, Handler] = {
    "dispatch": _h_dispatch,
    "escalate": _h_escalate,
    "flag": _h_flag,
    "recheck": _h_recheck,
}


# ── dispatcher ---------------------------------------------------------
class DispatchError(Exception):
    """A verb with no hand is a bug, not a no-op."""


class Dispatcher:
    """verb → handler. Resolution order: adapter handlers ∪ explicit ∪ defaults."""

    def __init__(self, subject: Any = None, candidates: Any = None,
                 adapter: Any = None,
                 handlers: dict[str, Handler] | None = None,
                 outbox: Path | None = None,
                 close_loop: bool = False):
        self.subject = subject
        self.candidates = candidates
        self.adapter = adapter
        self.outbox = outbox or DEFAULT_OUTBOX
        self.close_loop = close_loop   # recheck handler arms self-closing specs

        merged: dict[str, Handler] = {}
        if adapter is not None:
            merged.update(getattr(adapter, "handlers", lambda: {})())
        if handlers:
            merged.update(handlers)
        merged.update(DEFAULT_HANDLERS)   # defaults last — never shadowed out
        if handlers:
            # explicit handlers must beat defaults (they were passed on purpose)
            explicit = {**DEFAULT_HANDLERS, **handlers}
            merged = {**merged, **explicit}
        self._handlers = merged

    def _context(self, action: Any) -> DispatchContext:
        return DispatchContext(adapter=self.adapter, subject=self.subject,
                               candidates=self.candidates, action=action,
                               outbox=self.outbox, close_loop=self.close_loop)

    def run(self, action: Any) -> HandlerResult:
        try:
            handler = self._handlers[action.verb]
        except KeyError:
            raise DispatchError(
                f"no handler for verb {action.verb!r} — a verb with no hand is a bug")
        try:
            return handler(self._context(action))
        except Exception as exc:
            # A raising handler is a FAILED dispatch, not a crash: report it,
            # keep the action pending, and let the caller retry.
            return HandlerResult(ok=False,
                                 message=f"handler raised {type(exc).__name__}: {exc}")

    def dispatch(self, action: Any) -> HandlerResult:
        """Bind self, then let __call__ do the status transition and return the result."""
        action._dispatcher = self
        action()
        return HandlerResult(
            ok=action.status == "dispatched",
            message=action.raw.get("dispatch_result", action.raw.get("last_error", "")),
            artifact=action.raw.get("dispatch_artifact", ""),
        )