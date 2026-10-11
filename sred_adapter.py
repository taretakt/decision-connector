#!/usr/bin/env python3
# sred_adapter.py
#
# Domain: SR&ED claim disposition — the judgment layer of the BOOM SR&ED
# harness (design: vault/Ventures/sred-claim-adapter-design-2026-10-11.md).
#
#   subject    — an experimental thread: a branch/cluster of commits around one
#                problem, with the measured uncertainty trail attached.
#   candidates — dispositions the claim can take:
#                  claimable      systematic investigation, documented
#                                 uncertainty, qualifies for the T661.
#                  partial        real work, thin evidence — needs enrichment
#                                 before it can be claimed.
#                  not_claimable  routine development, no technological
#                                 uncertainty to argue.
#
# This is the credibility engine: same git history in → same disposition out,
# byte-deterministic. No sampler, no LLM-as-judge. The concrete decides; the
# boom only reports. `partial` and `not_claimable` exist so the system can say
# no — the clean-cut record is what makes aggressive claims believable.
#
# Usage:
#   uv run python3 sred_adapter.py     # offline demo + contract check
#
# Reduction (OP-15): optional actions_for() member maps dispositions to
# domain verbs; confidence-band routing falls through to ActionResolver.

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from decision_connector import (  # noqa: E402
    Choice,
    DecisionConnector,
    Noul,
    Score,
)

# ──────────────────────────────────────────────────────────────────────────
#  Dispositions the claim can take
# ──────────────────────────────────────────────────────────────────────────

DISPOSITIONS = {
    "claimable": "Systematic investigation with documented technological "
                 "uncertainty; qualifies for the T661 as claimed work.",
    "partial": "Real experimental work, but the evidence trail is thin — "
               "needs enrichment (ADRs, failure records, baseline measures) "
               "before it can be claimed.",
    "not_claimable": "Routine development; no technological uncertainty to "
                     "argue. Keep the clean-cut record.",
}


class SredClaimAdapter:
    """experimental thread × claim disposition → what the T661 says about it.

    subject   = one experimental thread (branch + commit range + PR/issue refs
                + the measured uncertainty trail from the artifact collector)
    candidates= claimable / partial / not_claimable
    """

    name = "sred.claim-disposition"
    # ONE multivariable decision over the dispositions; distribution fans out.
    evaluation_mode = "one_shot"

    def subject_key(self, thread: dict) -> str:
        # Stable identity: project + branch. NOT the commit hash (the commit
        # range is exactly what grows between evaluations).
        return f"{thread.get('project', '?')}|{thread.get('branch', '?')}"

    def candidate_keys(self, thread: dict, candidates=None) -> list[str]:
        return list(candidates or DISPOSITIONS.keys())

    def state_for(self, thread: dict, candidates=None) -> str:
        t = thread.get("thread") or {}
        lines = ["SR&ED EXPERIMENTAL THREAD — MEASURED UNCERTAINTY TRAIL",
                 f"  project         : {thread.get('project')}",
                 f"  branch          : {thread.get('branch')}",
                 f"  commit_range    : {thread.get('commit_range')}",
                 f"  prs             : {thread.get('prs')}",
                 f"  issues          : {thread.get('issues')}",
                 f"  window          : {thread.get('window')}",
                 "",
                 "MEASURED TRAIL (computed mechanically before this call)",
                 f"  commits         : {t.get('commits')}",
                 f"  failed_builds   : {t.get('failed_builds')}",
                 f"  hypothesis_recs : {t.get('hypothesis_records')}",
                 f"  adrs            : {t.get('adrs')}",
                 f"  baseline        : {t.get('baseline_measure')}",
                 f"  after_measure   : {t.get('after_measure')}",
                 f"  miter_verdicts  : {t.get('miter_verdicts')}",
                 "",
                 "AVAILABLE DISPOSITIONS:"]
        for c in self.candidate_keys(thread, candidates):
            lines.append(f"  - [{c}] {DISPOSITIONS.get(c, c)}")
        return "\n".join(lines)

    def questions_for(self, thread, candidates, candidate_key):
        return {
            "disposition": Choice(
                instructions=("Which disposition fits this experimental thread? "
                              "Weigh whether the work hit a genuine technical "
                              "uncertainty (not routine development), whether "
                              "the systematic method is documented (hypotheses, "
                              "baseline, failures), and whether the evidence "
                              "trail could survive a CRA review. A thin trail "
                              "on real work is 'partial', not 'claimable' — "
                              "claiming it would be the fraud engine's move."),
                criteria={c: DISPOSITIONS[c]
                          for c in self.candidate_keys(thread, candidates)},
            ),
            "uncertainty_demonstrated": Noul(
                instructions=("The work hit a real technological uncertainty — "
                              "a problem the team did not already know how to "
                              "solve — not routine application of known method."),
            ),
            "baseline_measurable": Noul(
                instructions=("There is a measurable before/after: a baseline "
                              "state and a after-state that can be compared. "
                              "SR&ED needs the comparison to exist."),
            ),
            "hypothesis_tested": Noul(
                instructions=("The work followed a systematic method — "
                              "hypotheses, failures, resumptions — rather "
                              "than ad hoc tinkering. This is T661 lines "
                              "242/246 territory."),
            ),
            "docs_depth": Score(
                instructions=("How much of the evidence trail already exists in "
                              "a T661-narrative-ready form? Low = the artifacts "
                              "are commits only. High = ADRs, failure records, "
                              "hypothesis records, baseline measures all present."),
                criteria=["commits only", "some records",
                          "most records", "narrative-ready"],
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["disposition"].value,
            noul=a["uncertainty_demonstrated"].value,
            baseline=a["baseline_measurable"].value,
            hypothesis=a["hypothesis_tested"].value,
            score=a["docs_depth"].value,
            confidence=getattr(a["disposition"], "confidence", None),
            probabilities=dict(a["disposition"].probabilities or {}),
            label=a["disposition"].value,
        )

    # -- OP-15 actions (7th contract member, optional) -------------------
    def actions_for(self, d) -> list[Any]:
        """Disposition → domain verbs. Confidence-band routing stays in the
        ActionResolver default; this adds the claim-specific flavor."""
        from action_iteration import Action
        verb_by_disposition = {
            "claimable": "dispatch",   # draft-t661 material to the generator
            "partial": "recheck",      # enrich evidence, then re-ask
            "not_claimable": "flag",   # skip-thread, keep the clean-cut record
        }
        acts = []
        if d.choice in verb_by_disposition:
            target = {
                "claimable": "draft-t661",
                "partial": f"enrich:{d.subject_key}",
                "not_claimable": "skip-thread",
            }[d.choice]
            acts.append(Action(
                verb=verb_by_disposition[d.choice],
                target=target, owner="hermes",
                authority="delegated", urgency=0.85,
                source_decision=d.subject_key + "\x1f" + d.candidate_key,
                reason=(f"SR&ED disposition '{d.choice}' "
                        f"(conf={d.confidence:.3f})"),
            ))
        return acts


# ──────────────────────────────────────────────────────────────────────────
#  Offline demo
# ──────────────────────────────────────────────────────────────────────────

class _FakeOracle:
    """Answers every question the adapter can ask, offline, deterministically."""

    def __init__(self, disposition: str, probs: dict[str, float],
                 confidence: float, uncertainty: float, baseline: float,
                 hypothesis: float, depth: int):
        self._d = dict(disposition=disposition, probs=probs,
                       confidence=confidence, uncertainty=uncertainty,
                       baseline=baseline, hypothesis=hypothesis, depth=depth)

    def system_one(self, state, questions):
        q = self._d
        class A:
            def __init__(s, **kw): s.__dict__.update(kw)
        class R:
            model = "jev-standin"
            answers = {
                "disposition": A(value=q["disposition"],
                                 probabilities=q["probs"],
                                 confidence=q["confidence"]),
                "uncertainty_demonstrated": A(value=q["uncertainty"],
                                             confidence=q["confidence"]),
                "baseline_measurable": A(value=q["baseline"],
                                        confidence=q["confidence"]),
                "hypothesis_tested": A(value=q["hypothesis"],
                                      confidence=q["confidence"]),
                "docs_depth": A(value=q["depth"], confidence=q["confidence"]),
            }
        return R()

    def close(self): pass


def _thread(**over) -> dict:
    t = {
        "project": "miter", "branch": "exp/windowed-aggregates",
        "commit_range": "v0.4..v0.6", "prs": ["#41", "#47"], "issues": ["#38"],
        "window": "2026-09-15T00:00Z..2026-10-05T00:00Z",
        "thread": {
            "commits": 34, "failed_builds": 7, "hypothesis_records": 4,
            "adrs": 2, "baseline_measure": "byte-determinism probe",
            "after_measure": "windowed scan parity", "miter_verdicts": "16/16",
        },
    }
    t.update(over)
    return t


if __name__ == "__main__":
    # -- contract check --------------------------------------------------
    a = SredClaimAdapter()
    th = _thread()
    assert a.subject_key(th) == "miter|exp/windowed-aggregates"
    assert a.candidate_keys(th) == list(DISPOSITIONS)
    state = a.state_for(th)
    assert "AVAILABLE DISPOSITIONS" in state and "[claimable]" in state
    q = a.questions_for(th, None, "claimable")
    assert set(q) == {"disposition", "uncertainty_demonstrated",
                      "baseline_measurable", "hypothesis_tested", "docs_depth"}
    print("contract: 6 members + actions_for present ✓")

    # -- offline one_shot: one call → 3 rows ---------------------------
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        oracle = _FakeOracle(
            disposition="claimable",
            probs={"claimable": 0.72, "partial": 0.20, "not_claimable": 0.08},
            confidence=0.72, uncertainty=0.92, baseline=0.81,
            hypothesis=0.88, depth=3,
        )
        c = DecisionConnector(a, db_path=f"{td}/g.db", client=oracle)
        rows = c.evaluate(th, None)
        assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
        winner = max(rows, key=lambda r: r.noul or 0)
        assert winner.choice == "claimable"
        assert "claimable" in winner.label
        from decision_connector import _ans_value
        # canonical row shape carried on the decision
        routes = sorted({c.route(r) for r in rows})
        print(f"offline one_shot: 1 call → {len(rows)} rows, winner="
              f"{winner.choice}, routes={routes} ✓")
        c.close()

    # -- warm cache: second evaluate, zero API calls ---------------------
    import tempfile as _t
    with _t.TemporaryDirectory() as td:
        oracle = _FakeOracle(
            disposition="partial",
            probs={"claimable": 0.30, "partial": 0.55, "not_claimable": 0.15},
            confidence=0.55, uncertainty=0.62, baseline=0.40,
            hypothesis=0.58, depth=1,
        )
        c = DecisionConnector(a, db_path=f"{td}/g.db", client=oracle)
        c.evaluate(th, None)
        rows = c.evaluate(th, None)
        assert all(r.source == "cache" for r in rows)
        print(f"warm rerun: all {len(rows)} rows served from cache ✓")
        c.close()

    print("ALL CHECKS PASSED")