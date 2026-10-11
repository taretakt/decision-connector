#!/usr/bin/env python3
# recheck_driver.py — OP-03 recheck execution body (no_agent cron payload)
#
# Reads a recheck spec JSON, re-asks the SAME question fresh (force=True —
# a recheck must never be served from cache; the point is to re-ask),
# and prints the outcome. stdout is delivered verbatim by the cron layer.
#
# Output grammar (stable, parseable):
#   RESOLVED <verb> <target> [| <verb> <target> ...]   → band left, act now
#   RECHECK <count> <due-iso>                          → still ambiguous, next
#   ESCALATE <target> <reason>                          → cap hit, human takes it
#
# Spec JSON (written by arm_recheck()):
#   adapter: "qc-disposition" | "fitment"
#   subject / candidates: the domain payloads
#   source_key: the decision's deterministic key (provenance)
#   count: how many rechecks have already run (0 = first)
#   due: ISO timestamp of THIS fire (for the "next" message arithmetic)
#   offline_payload: [choice, noul, score, probs, conf] — test-only override
#
# Exit: 0 on any handled outcome, 2 on spec error, 3 on connector error.
# Usage: python3 recheck_driver.py --spec /path/to/spec.json

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from action_iteration import ActionResolver
from dispatch import Dispatcher, DEFAULT_OUTBOX as _DEFAULT_OUTBOX

HERE = Path(__file__).resolve().parent

MAX_RECHECKS = 2          # must match ActionResolver default — pre-registered
RECHECK_DELTA_H = 6       # base wait; linear backoff: delta × (count + 1)


def arm_recheck(spec_path: Path, adapter: str, subject: dict, candidates: dict,
                source_key: str, count: int, due: str,
                offline_payload=None, close_loop: bool = False) -> Path:
    """Arm the next recheck: write a spec the driver can execute.

    Agent-side: called when a recheck Action fires, to create the cron job's
    spec file. Deterministic — the spec IS the input, so the loop stays
    byte-deterministic at every step.

    close_loop=True → the loop is self-closing: the driver rewrites its own
    spec after each fire (advancing count, or flipping state→closed), so a
    RECURRING cron job drives the loop without a re-arm. False (default) →
    one-shot manual arm, the original behavior.
    """
    spec = {
        "adapter": adapter,
        "subject": subject,
        "candidates": candidates,
        "source_key": source_key,
        "count": count,
        "due": due,
        "close_loop": close_loop,
        "state": "open",
    }
    if offline_payload is not None:
        spec["offline_payload"] = offline_payload
    spec_path = Path(spec_path)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True))
    return spec_path


def load_spec(path: Path) -> dict:
    with open(path) as f:
        spec = json.load(f)
    for k in ("adapter", "subject", "candidates", "source_key", "count", "due"):
        if k not in spec:
            raise KeyError(f"spec missing {k!r}")
    return spec


def build_client(spec: dict):
    """Live TypeSafe client, or the offline fake when spec carries a test payload."""
    payload = spec.get("offline_payload")
    if payload is not None:
        from decision_connector import _FakeClient
        adapter_name = spec["adapter"]
        if adapter_name == "qc-disposition":
            sig = spec["subject"].get("defect_class", "surface_scratch")
            sev = spec["subject"].get("severity", "cosmetic_minor")
            table = {(sig, sev): payload}
        else:
            table = {"fitment": payload}
        return _FakeClient(table), None
    from typesafe_sdk import TypeSafeClient
    return TypeSafeClient(), TypeSafeClient


def build_adapter(name: str):
    from decision_connector import QCDispositionAdapter, FitmentAdapter
    if name == "qc-disposition":
        return QCDispositionAdapter()
    if name == "fitment":
        return FitmentAdapter()
    raise ValueError(f"unknown adapter {name!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--dispatch", action="store_true",
                    help="bridge: route non-recheck actions through the Dispatcher (hands + outbox)")
    args = ap.parse_args()
    outbox = Path(os.environ.get("DECISION_OUTBOX", str(_DEFAULT_OUTBOX)))

    try:
        spec = load_spec(args.spec)
        adapter = build_adapter(spec["adapter"])
        client, client_cls = build_client(spec)

        from decision_connector import DecisionConnector

        _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        _tmp.close()
        conn = DecisionConnector(adapter, db_path=Path(_tmp.name), client=client)

        decisions = conn.evaluate(
            spec["subject"], spec["candidates"],
            force=True,  # NEVER cache a recheck — stale state must not silence it
        )

        if spec.get("state") == "closed":
            # Loop already finished on a previous fire — silent watchdog:
            # empty stdout means no delivery, so a recurring job idles quietly.
            if client_cls is not None:
                client.close()
            try:
                conn.close()
                os.unlink(_tmp.name)
            except OSError:
                pass
            return 0

        count = spec["count"]
        dispatcher = None
        if args.dispatch:
            from dispatch import DispatchError  # fail-loud: a handless verb is a bug
            dispatcher = Dispatcher(adapter=adapter, subject=spec["subject"],
                                    candidates=spec["candidates"], outbox=outbox)
            dispatched: list[str] = []

        for d in decisions:
            d.raw["recheck_count"] = count  # count is input → determinism holds

        handled = False
        saw_recheck = False
        saw_escalate = False
        for d in decisions:
            actions = ActionResolver(adapter, max_rechecks=MAX_RECHECKS).reduce(d)
            rechecks = [a for a in actions if a.verb == "recheck"]
            escalates = [a for a in actions if a.verb == "escalate"]
            others = [a for a in actions if a.verb not in ("recheck", "escalate")]

            if rechecks and not escalates:
                nxt = count + 1
                wait = timedelta(hours=RECHECK_DELTA_H * nxt)   # linear backoff
                due = datetime.fromisoformat(spec["due"]) + wait
                print(f"RECHECK {nxt} {due.isoformat(timespec='minutes')}")
                handled = True
                saw_recheck = True
            elif escalates:
                esc = escalates[0]
                print(f"ESCALATE {d.candidate_key} {esc.reason}")
                handled = True
                saw_escalate = True
                if dispatcher is not None:
                    for a in escalates:
                        dispatcher.dispatch(a)
                        dispatched.append(f"{a.verb}:{a.target}")
            else:
                line = " | ".join(f"{a.verb} {a.target}" for a in others)
                if line:
                    print(f"RESOLVED {line}")
                    handled = True
                    if dispatcher is not None:
                        for a in others:          # recheck is NEVER here (else-branch excludes it)
                            dispatcher.dispatch(a)
                            dispatched.append(f"{a.verb}:{a.target}")

        # Optional self-closing loop: rewrite THIS spec so a recurring job
        # drives the loop without a re-arm. count+1 while still ambiguous;
        # flip state→closed once escalated or resolved.
        if spec.get("close_loop") and handled:
            if saw_recheck and not saw_escalate:
                spec["count"] = count + 1
                wait = timedelta(hours=RECHECK_DELTA_H * (count + 1))
                spec["due"] = (datetime.fromisoformat(spec["due"]) + wait) \
                    .isoformat(timespec="minutes")
                spec["state"] = "open"
            else:
                spec["state"] = "closed"
            args.spec.write_text(json.dumps(spec, indent=2, sort_keys=True))

        if client_cls is not None:
            client.close()
        try:
            conn.close()
            os.unlink(_tmp.name)
        except OSError:
            pass
        return 0
    except KeyError as e:
        print(f"SPEC-ERROR {e}", file=sys.stderr)
        return 2
    except Exception as e:  # connector failure — cron alerts on non-zero
        print(f"CONNECTOR-ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())