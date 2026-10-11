#!/usr/bin/env python3
# action_iteration.py
#
# OP-15 Action + ActionResolver — the reduction half of the Object Protocol.
# A Decision's __iter__ is its reduction: the deterministic set of actions
# its verdict implies. __call__ scores; __iter__ reduces.
#
# Design: vault/decision-connector/DESIGN-action-iteration.md (2026-10-05)
# Registry: vault/Systems/Object_Protocol/OP-15-action.md
#
# Determinism: same Decision fields in → same Action list out. Keys derive
# from zlib.crc32 over canonical (field, value) pairs. NEVER hash().
# No wall-clock in derivation (except `due`, a payload field, not a key).
#
# Idempotent dispatch: status pending → dispatched → done, one-way.
# Re-iterating a cached decision yields the same actions; __bool__ is False
# once dispatched. The Law: priority reorders ORDER, never membership.

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional


# ═══════════════════════════════════════════════════════════════
# OP-15 — Action (leaf object)
# ═══════════════════════════════════════════════════════════════

@dataclass
class Action:
    """The smallest unit a Decision reduces to. A leaf: __iter__ is
    NotImplemented by design — an action does not decompose."""

    verb: str                    # dispatch | escalate | flag | recheck | domain verb
    target: str                  # the candidate/subject the verb applies to
    owner: str = "ross"          # custody: ross | hermes | domain label
    authority: str = "elective"  # Authority Map class
    urgency: float = 0.5         # __lt__ sort key — order only, never membership
    status: str = "pending"      # pending | dispatched | done (one-way)
    source_decision: str = ""    # provenance: decision key
    reason: str = ""             # human line: the chain that produced it
    due: Optional[datetime] = None  # recheck actions carry a fire-time
    raw: dict[str, Any] = field(default_factory=dict)

    # -- identity --------------------------------------------------
    @property
    def key(self) -> str:
        """Deterministic id: crc32 over verb + target + source decision."""
        payload = "\x1f".join([self.verb, self.target, self.source_decision])
        return f"act-{zlib.crc32(payload.encode()) & 0xFFFFFFFF:08x}"

    def __str__(self) -> str:
        due_s = ""
        if self.due:
            due_s = f" due={self.due.isoformat(timespec='minutes')}"
        return f"→ {self.verb} {self.target} (u={self.urgency:.2f}){due_s}"

    def __repr__(self) -> str:
        return (f"Action(key={self.key}, verb={self.verb!r}, target={self.target!r}, "
                f"owner={self.owner!r}, status={self.status!r})")

    # -- protocol --------------------------------------------------
    def __bool__(self) -> bool:
        """Alive? → still pending. One-way: once dispatched, it stops being."""
        return self.status == "pending"

    def __lt__(self, other: "Action") -> bool:
        """Urgency sort, reversed — urgent first. ORDER only, never membership."""
        return self.urgency > other.urgency

    def __call__(self) -> "Action":
        """Dispatch. If a Dispatcher is bound (via Dispatcher.dispatch), the
        verb resolves to a handler and runs — side effects live there. On
        handler failure the action STAYS pending (retryable, last_error set);
        on success it goes dispatched. With no dispatcher bound this is the
        bare legacy transition. Idempotent — never re-runs a dispatched/done
        action. This is the ONLY mutation path."""
        if self.status != "pending":
            return self
        disp = getattr(self, "_dispatcher", None)
        if disp is None:
            self.status = "dispatched"
            return self
        result = disp.run(self)
        if result.ok:
            self.status = "dispatched"
            self.raw["dispatch_result"] = result.message
        else:
            self.raw["last_error"] = result.message
        if result.artifact:
            self.raw["dispatch_artifact"] = result.artifact
        return self

    def __getitem__(self, key: str):
        return getattr(self, key)

    def __iter__(self) -> Iterator["Action"]:
        """LEAF — does not decompose. Same class of law as OP-08 Person's
        __call__ and OP-06 The Pen's __repr__: the permanent exceptions."""
        raise TypeError(f"Action ({self.key}) is a leaf object — it does not "
                        f"decompose. Iterate the Decision, not the Action.")

    # -- recheck wire ----------------------------------------------
    def schedule_iso(self) -> Optional[str]:
        """Pure wire-out for the cron path: due → ISO 8601 schedule string.
        The cron job itself is a separate build, gated on this design."""
        return self.due.isoformat(timespec="seconds") if self.due else None


# ═══════════════════════════════════════════════════════════════
# ActionResolver — shared, domain-blind substrate
# ═══════════════════════════════════════════════════════════════

class ActionResolver:
    """Deterministic Decision → list[Action] reduction.

    Default grammar (mirrors DecisionConnector.route bands):

        noul inside dead band  → recheck (new route; the model said "no opinion")
        conf >= act_above      → dispatch (the chosen candidate)
        conf <= escalate_below → escalate (owner=ross)
        else                   → dispatch + flag (act, but reviewed)

    Control values are constructor params — pre-registered, never tuned
    post-hoc. Adapter override: optional `actions_for(decision)` member
    (7th contract member); absent → resolver default covers everything.
    """

    def __init__(self, adapter: Any = None,
                 act_above: float = 0.85, escalate_below: float = 0.60,
                 dead_low: float = 0.45, dead_high: float = 0.55,
                 recheck_delta: timedelta = timedelta(hours=6),
                 max_rechecks: int = 2):
        self.adapter = adapter
        self.act_above = act_above
        self.escalate_below = escalate_below
        self.dead_low = dead_low
        self.dead_high = dead_high
        self.recheck_delta = recheck_delta
        self.max_rechecks = max_rechecks   # hard cap, pre-registered (design noul #2)

    # -- public ----------------------------------------------------
    def reduce(self, d: Any) -> list[Action]:
        """The full reduction: resolver default + adapter override."""
        actions = self.default(d)
        actions.extend(self.adapter_actions(d))
        return actions

    def default(self, d: Any) -> list[Action]:
        from decision_connector import Decision  # local import: no cycle
        assert isinstance(d, Decision), "resolver reduces Decision objects only"

        c = d.confidence if d.confidence is not None else d.top_probability
        target = d.choice or d.candidate_key

        # Recheck: noul inside the dead band → the model's "no opinion" zone.
        # Not actable, not escalate-worthy — re-question later.
        # Loop guard (design noul #2): the recheck count rides in
        # Decision.raw["recheck_count"] — part of the input, so determinism
        # holds (same fields in → same actions out). At the hard cap the
        # action escalates-with-note instead of asking again. Backoff is
        # linear: each recheck waits recheck_delta × (count + 1).
        if d.noul is not None and self.dead_low <= d.noul <= self.dead_high:
            count = int(d.raw.get("recheck_count", 0) or 0)
            sd = d.subject_key + "\x1f" + d.candidate_key
            if count >= self.max_rechecks:
                return [Action(
                    verb="escalate", target=d.candidate_key, owner="ross",
                    authority="delegated", urgency=0.98,
                    source_decision=sd,
                    reason=(f"recheck cap reached ({count}/{self.max_rechecks}): "
                            f"noul={d.noul:.3f} still in dead band "
                            f"[{self.dead_low}, {self.dead_high}]"),
                )]
            return [Action(
                verb="recheck", target=d.candidate_key, owner="hermes",
                authority="delegated", urgency=0.7,
                source_decision=sd,
                reason=f"noul={d.noul:.3f} inside dead band "
                       f"[{self.dead_low}, {self.dead_high}]",
                due=datetime.now() + self.recheck_delta * (count + 1),
                raw={"recheck_count": count + 1},
            )]

        sd = d.subject_key + "\x1f" + d.candidate_key

        if c >= self.act_above:
            return [Action(verb="dispatch", target=target, owner="ross",
                           authority="elective", source_decision=sd,
                           urgency=min(0.99, 0.5 + c / 2), reason="high confidence")]
        if c <= self.escalate_below:
            return [Action(verb="escalate", target=d.candidate_key, owner="ross",
                           authority="elective", source_decision=sd,
                           urgency=0.95, reason=f"conf={c:.3f} below {self.escalate_below}")]
        # act_and_flag
        return [
            Action(verb="dispatch", target=target, owner="ross",
                   authority="elective", source_decision=sd,
                   urgency=min(0.99, 0.5 + c / 2), reason="mid confidence — act with review"),
            Action(verb="flag", target=d.candidate_key, owner="hermes",
                   authority="delegated", source_decision=sd, urgency=0.6,
                   reason=f"conf={c:.3f} in review band"),
        ]

    def adapter_actions(self, d: Any) -> list[Action]:
        """7th adapter member, OPTIONAL. Absent → []. Never raises."""
        fn = getattr(self.adapter, "actions_for", None)
        if fn is None:
            return []
        extra = fn(d)
        if not extra:
            return []
        return list(extra)


def reduce_decision(d: Any, adapter: Any = None) -> list[Action]:
    """Convenience: the reduction of one decision."""
    return ActionResolver(adapter).reduce(d)


if __name__ == "__main__":
    from decision_connector import Decision
    d_act = Decision(subject_key="veh-a", candidate_key="pad-slide-econo",
                     choice="pad-slide-econo", confidence=0.95)
    print("act   →", [str(a) for a in reduce_decision(d_act)])
    d_mid = Decision(subject_key="veh-a", candidate_key="pad-slide-econo",
                     choice="pad-slide-econo", confidence=0.72)
    print("mid   →", [str(a) for a in reduce_decision(d_mid)])
    d_esc = Decision(subject_key="def-x", candidate_key="escalate",
                     confidence=0.30)
    print("esc   →", [str(a) for a in reduce_decision(d_esc)])
    d_amb = Decision(subject_key="def-x", candidate_key="rework",
                     noul=0.51, confidence=0.9)
    print("rechk →", [str(a) for a in reduce_decision(d_amb)])
    print("dispatch idempotent:",
          reduce_decision(d_act)[0](), reduce_decision(d_act)[0].status)
