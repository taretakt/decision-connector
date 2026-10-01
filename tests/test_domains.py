"""Domain hydration tests — deal scoring + catalog watchdog.

Both adapters prove the substrate claim: a new domain is one file with six
members, zero substrate changes. These tests run fully offline against the
stand-in primitives.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from decision_connector import DecisionConnector
from deal_scoring_adapter import DealScoringAdapter, GRADES, _FakeOracle
from catalog_watchdog_adapter import CatalogWatchdogAdapter, VERDICTS, _FakeOracle


# ── shared fake oracle ----------------------------------------------------

class CountingOracle:
    """Deterministic answers + a call counter, as in the substrate's selftest."""

    def __init__(self):
        self.calls = []
        self._deal = None
        self._watch = None

    def use_deal(self, grade="buy", probs=None, confidence=0.71,
                 clears_floor=0.9, comps=3):
        self._deal = dict(grade=grade,
                          probs=probs or {"buy": 0.58, "hold": 0.27,
                                          "pass": 0.12, "scrap": 0.03},
                          confidence=confidence, clears_floor=clears_floor,
                          comps=comps)

    def use_watch(self, verdict="digest", probs=None, confidence=0.74,
                  intact=0.95, depth=2):
        self._watch = dict(verdict=verdict,
                           probs=probs or {"no_digest": 0.10, "digest": 0.72,
                                           "escalate": 0.18},
                           confidence=confidence, intact=intact, depth=depth)

    def system_one(self, state, questions):
        self.calls.append(questions)
        class A:
            def __init__(s, **kw): s.__dict__.update(kw)
        class R:
            model = "jev-standin"
            answers = {}
        if self._deal is not None:
            q = self._deal
            R.answers = {
                "grade": A(value=q["grade"], probabilities=q["probs"],
                           confidence=q["confidence"]),
                "clears_floor": A(value=q["clears_floor"],
                                  confidence=q["confidence"]),
                "comps_confidence": A(value=q["comps"],
                                      confidence=q["confidence"]),
            }
        elif self._watch is not None:
            q = self._watch
            R.answers = {
                "verdict": A(value=q["verdict"], probabilities=q["probs"],
                             confidence=q["confidence"]),
                "scraper_intact": A(value=q["intact"],
                                    confidence=q["confidence"]),
                "inspection_depth": A(value=q["depth"],
                                      confidence=q["confidence"]),
            }
        return R()

    def close(self): pass


# ── fixtures --------------------------------------------------------------

def _listing():
    return {
        "lot_id": "MS-LOT-8412",
        "title": "Pallet of cordless power tools, mixed brand, open box",
        "category": "power-tools", "condition": "open-box",
        "units": 42, "unit_cost_usd": 11.50, "lot_cost_usd": 483.00,
        "pallets": 1, "source_fee_pct": 0.13, "ship_usd": 140.00,
        "median_sold_usd": 42.00, "sell_through_pct": 0.71, "condition_adj": 0.90,
    }


def _pair(**over):
    pair = {
        "source": "megasavers", "path": "/clearance/power-tools",
        "window": "2026-10-01T03:00Z",
        "measurements": {
            "items_before": 880, "items_after": 884, "added": 6, "removed": 2,
            "modified": 14, "drift_fields": ["price_usd"], "price_churn_pct": 0.08,
            "structural_hash": "3f9a21c7", "structure_broke": False,
        },
    }
    pair.update(over)
    return pair


# ── deal scoring ──────────────────────────────────────────────────────────

def test_deal_adapter_contract():
    a = DealScoringAdapter()
    listing = _listing()
    assert a.subject_key(listing) == "MS-LOT-8412"
    assert a.candidate_keys(listing) == list(GRADES)
    state = a.state_for(listing)
    assert "AVAILABLE GRADES" in state and "[buy]" in state
    q = a.questions_for(listing, None, "buy")
    assert set(q) == {"grade", "clears_floor", "comps_confidence"}


def test_deal_one_shot_is_one_call_four_rows(tmp_path):
    oracle = CountingOracle(); oracle.use_deal()
    c = DecisionConnector(DealScoringAdapter(), db_path=str(tmp_path / "g.db"),
                          client=oracle)
    rows = c.evaluate(_listing(), None)
    assert len(rows) == 4
    assert len(oracle.calls) == 1
    assert all(r.subject_key == "MS-LOT-8412" for r in rows)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "buy"
    assert winner.noul == pytest.approx(0.58)
    c.close()


def test_deal_warm_rerun_serves_cache(tmp_path):
    oracle = CountingOracle(); oracle.use_deal()
    c = DecisionConnector(DealScoringAdapter(), db_path=str(tmp_path / "g.db"),
                          client=oracle)
    c.evaluate(_listing(), None)
    calls_after_first = len(oracle.calls)
    rows = c.evaluate(_listing(), None)
    assert len(oracle.calls) == calls_after_first  # no second API call
    assert all(r.source == "cache" for r in rows)
    c.close()


def test_deal_route_bands(tmp_path):
    oracle = CountingOracle(); oracle.use_deal(confidence=0.62)
    c = DecisionConnector(DealScoringAdapter(), db_path=str(tmp_path / "g.db"),
                          client=oracle)
    rows = c.evaluate(_listing(), None)
    assert sorted({c.route(r) for r in rows}) == ["act_and_flag"]  # 0.62 band
    c.close()


# ── catalog watchdog ──────────────────────────────────────────────────────

def test_watch_adapter_contract():
    a = CatalogWatchdogAdapter()
    pair = _pair()
    assert a.subject_key(pair) == "megasavers|/clearance/power-tools"
    assert a.candidate_keys(pair) == list(VERDICTS)
    assert "MEASURED DELTA" in a.state_for(pair)
    q = a.questions_for(pair, None, "digest")
    assert set(q) == {"verdict", "scraper_intact", "inspection_depth"}


def test_watch_one_shot_one_call_three_rows(tmp_path):
    oracle = CountingOracle(); oracle.use_watch()
    c = DecisionConnector(CatalogWatchdogAdapter(),
                          db_path=str(tmp_path / "g.db"), client=oracle)
    rows = c.evaluate(_pair(), None)
    assert len(rows) == 3
    assert len(oracle.calls) == 1
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "digest"
    assert winner.noul == pytest.approx(0.72)
    c.close()


def test_watch_structure_broken_wins_escalate(tmp_path):
    oracle = CountingOracle()
    oracle.use_watch(verdict="escalate",
                     probs={"no_digest": 0.02, "digest": 0.08, "escalate": 0.90},
                     confidence=0.88, intact=0.05, depth=4)
    c = DecisionConnector(CatalogWatchdogAdapter(),
                          db_path=str(tmp_path / "g.db"), client=oracle)
    pair = _pair(modified=0, added=0)
    pair["measurements"]["structure_broke"] = True
    rows = c.evaluate(pair, None)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "escalate"
    assert winner.noul == pytest.approx(0.90)
    c.close()


def test_full_distribution_rides_in_raw(tmp_path):
    oracle = CountingOracle(); oracle.use_deal()
    c = DecisionConnector(DealScoringAdapter(), db_path=str(tmp_path / "g.db"),
                          client=oracle)
    rows = c.evaluate(_listing(), None)
    for r in rows:
        assert set(r.raw["full_distribution"]) == set(GRADES)
    c.close()