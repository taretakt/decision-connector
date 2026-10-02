"""Smoke tests for the decision-connector substrate.

These run fully offline against the stand-in primitives: the adapter
contract, the cache-key determinism, the solution grid, and the two
evaluation modes (ONE_SHOT vs PER_CANDIDATE).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decision_connector import (
    DecisionConnector,
    FitmentAdapter,
    QCDispositionAdapter,
    cache_key,
    canonical_question_hash,
)


class _Fake:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeResponse:
    model = "jev-stand-in"

    def __init__(self):
        probs = {d: 0.0 for d in QCDispositionAdapter.DISPOSITIONS}
        probs["rework"] = 0.62
        self.answers = {
            "disposition": _Fake(value="rework", probabilities=probs, confidence=0.62),
            "acceptable_to_ship": _Fake(value=0.12, confidence=0.62),
            "judgment_difficulty": _Fake(value=3, confidence=0.62),
            "best_match": _Fake(value="rework", probabilities=probs, confidence=0.62),
            "compatible": _Fake(value=0.12, confidence=0.62),
            "difficulty": _Fake(value=3, confidence=0.62),
        }


class CountingClient:
    def __init__(self):
        self.calls = []

    def system_one(self, state, questions):
        self.calls.append(next(iter(questions)))
        return _FakeResponse()

    def close(self):
        pass


def _defect():
    return {
        "defect_class": "surface_scratch",
        "severity": "cosmetic_minor",
        "part_family": "bracket-A2",
        "detector_confidence": 0.87,
        "tolerance_class": "Class-2 surface",
        "customer_cosmetics": "high",
        "rework_cost_usd": 12,
        "scrap_cost_usd": 40,
        "units_short": 0,
        "lot_size": 500,
    }


def _vehicle():
    return {
        "arku": "CAR-x",
        "piece_type": "Buick Regal",
        "attributes": {"front_rotor_mm": 302},
    }


def _parts():
    return {"pad-a": {"name": "A"}, "pad-b": {"name": "B"}}


def test_one_shot_qc_six_rows_one_call(tmp_path):
    client = CountingClient()
    c = DecisionConnector(
        QCDispositionAdapter(),
        db_path=str(tmp_path / "grid.db"),
        client=client,
    )
    rows = c.evaluate(_defect(), None)
    assert len(rows) == 6
    assert len(client.calls) == 1
    assert sorted({c.route(r) for r in rows}) == ["act_and_flag"]  # uniform 0.62 confidence
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "rework"
    c.close()


def test_per_candidate_fitment_caches_warm_reruns(tmp_path):
    client = CountingClient()
    c = DecisionConnector(
        FitmentAdapter(),
        db_path=str(tmp_path / "grid.db"),
        client=client,
    )
    rows = c.evaluate(_vehicle(), _parts())
    assert len(rows) == 2
    assert len(client.calls) == 2  # two candidates, two judgments

    client.calls.clear()
    rows2 = c.evaluate(_vehicle(), _parts())
    assert len(client.calls) == 0  # warm: pure cache hits
    assert {r.source for r in rows2} == {"cache"}
    c.close()


def test_cache_keys_are_byte_deterministic():
    a = cache_key("fitment", "CAR-1", "PAD-A", "qhash")
    b = cache_key("fitment", "CAR-1", "PAD-A", "qhash")
    assert a == b
    c = cache_key("fitment", "CAR-1", "PAD-B", "qhash")
    assert a != c
    # unit separators prevent boundary collisions
    assert cache_key("fit", "ment", "PAD-A", "qhash") != cache_key(
        "fitmen", "t", "PAD-A", "qhash"
    )


def test_question_hash_is_order_stable():
    assert canonical_question_hash(["A", "B"]) == canonical_question_hash(["B", "A"])
    assert canonical_question_hash(["A", "B"]) != canonical_question_hash(["A", "B", "C"])