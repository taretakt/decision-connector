"""Domain hydration tests — SR&ED claim disposition adapter.

Proves the substrate claim once more: a new domain is one file with six
members (+ optional actions_for), zero substrate changes. Fully offline
against the stand-in primitives.

Design: vault/Ventures/sred-claim-adapter-design-2026-10-11.md
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from decision_connector import DecisionConnector
from action_iteration import ActionResolver
from sred_adapter import SredClaimAdapter, DISPOSITIONS, _FakeOracle, _thread


class CountingOracle:
    """Deterministic answers + a call counter."""

    def __init__(self):
        self.calls = []
        self._d = None

    def use(self, disposition="claimable", probs=None, confidence=0.72,
            uncertainty=0.92, baseline=0.81, hypothesis=0.88, depth=3):
        self._d = dict(disposition=disposition,
                       probs=probs or {"claimable": 0.72, "partial": 0.20,
                                       "not_claimable": 0.08},
                       confidence=confidence, uncertainty=uncertainty,
                       baseline=baseline, hypothesis=hypothesis, depth=depth)

    def system_one(self, state, questions):
        self.calls.append(questions)
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


# ── adapters share the fake-oracle shape as the shipped ones ────────

def test_sred_adapter_contract():
    a = SredClaimAdapter()
    th = _thread()
    assert a.subject_key(th) == "miter|exp/windowed-aggregates"
    assert a.candidate_keys(th) == list(DISPOSITIONS)
    state = a.state_for(th)
    assert "AVAILABLE DISPOSITIONS" in state and "[claimable]" in state
    q = a.questions_for(th, None, "claimable")
    assert set(q) == {"disposition", "uncertainty_demonstrated",
                      "baseline_measurable", "hypothesis_tested", "docs_depth"}
    # the honest-cut dispositions are in the criteria
    assert "not_claimable" in q["disposition"].criteria


def test_sred_one_shot_is_one_call_three_rows(tmp_path):
    oracle = CountingOracle(); oracle.use()
    c = DecisionConnector(SredClaimAdapter(),
                          db_path=str(tmp_path / "g.db"), client=oracle)
    rows = c.evaluate(_thread(), None)
    assert len(rows) == 3
    assert len(oracle.calls) == 1
    assert all(r.subject_key == "miter|exp/windowed-aggregates" for r in rows)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "claimable"
    c.close()


def test_sred_warm_rerun_serves_cache(tmp_path):
    oracle = CountingOracle(); oracle.use()
    c = DecisionConnector(SredClaimAdapter(),
                          db_path=str(tmp_path / "g.db"), client=oracle)
    c.evaluate(_thread(), None)
    n1 = len(oracle.calls)
    rows = c.evaluate(_thread(), None)
    assert len(oracle.calls) == n1          # no second API call
    assert all(r.source == "cache" for r in rows)
    c.close()


def test_sred_actions_for_domain_verbs(tmp_path):
    a = SredClaimAdapter()
    oracle = CountingOracle(); oracle.use(disposition="partial",
                                          confidence=0.55,
                                          probs={"claimable": 0.30,
                                                 "partial": 0.55,
                                                 "not_claimable": 0.15})
    c = DecisionConnector(a, db_path=str(tmp_path / "g.db"), client=oracle)
    rows = c.evaluate(_thread(), None)
    winner = max(rows, key=lambda r: r.noul or 0)
    res = ActionResolver(adapter=a, act_above=0.85, escalate_below=0.60)
    acts = res.reduce(winner)
    # partial → the adapter's recheck/enrich verb rides the resolution
    assert any(x.target.startswith("enrich:") for x in acts)
    c.close()


def test_sred_clean_cut_is_a_real_outcome(tmp_path):
    """The system can say no — not_claimable dispositions exist and route."""
    a = SredClaimAdapter()
    oracle = CountingOracle(); oracle.use(disposition="not_claimable",
                                          confidence=0.88,
                                          probs={"claimable": 0.04,
                                                 "partial": 0.08,
                                                 "not_claimable": 0.88})
    c = DecisionConnector(a, db_path=str(tmp_path / "g.db"), client=oracle)
    rows = c.evaluate(_thread(), None)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "not_claimable"
    res = ActionResolver(adapter=a, act_above=0.85, escalate_below=0.60)
    acts = res.reduce(winner)
    assert any(x.verb == "flag" and x.target == "skip-thread" for x in acts)
    c.close()