"""Substrate-loop API tests — OP-15 reduction semantics.

The v0.7 carve replaced the old is_terminal()/flush() surface with the OP-15
action layer: a Decision's __iter__ reduces to deterministic Actions
(dispatch / escalate / flag / recheck), and the loop is closed when no
recheck or escalate action remains. flush() became close().

These tests assert the new terminal semantics: a batch is terminal iff every
decision reduces without recheck/escalate verbs. Same loop-safety property,
expressed through the reduction instead of a bespoke method.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from decision_connector import Choice, DecisionConnector, Noul, Score


class _FlagsAdapter:
    """Minimal per_candidate adapter: one candidate, one judgment."""

    name = "test.loop-flags"
    evaluation_mode = "per_candidate"

    def subject_key(self, subject):
        return subject.get("id", "anon")

    def candidate_keys(self, subject, candidates):
        return list(candidates)

    def state_for(self, subject, candidates):
        return f"state:{subject.get('id')}"

    def questions_for(self, subject, candidates, candidate_key):
        return {
            "ok": Choice(instructions="is it ok?", criteria={"yes": "", "no": ""}),
            "room": Noul(instructions="enough room?"),
            "grade": Score(instructions="how good?", criteria=["bad", "ok", "great"]),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["ok"].value,
            noul=a["room"].value,
            score=a["grade"].value,
            confidence=a["ok"].confidence,
            probabilities=dict(a["ok"].probabilities or {}),
            label=a["ok"].value,
        )


class _Canned:
    """Answers any question with a fixed confidence. Domain-blind."""

    def __init__(self, conf):
        self.conf = conf

    def system_one(self, state, questions):
        class A:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        a = {
            "ok": A(value="yes", probabilities={"yes": self.conf, "no": 1 - self.conf},
                    confidence=self.conf),
            "room": A(value=0.8, confidence=self.conf),
            "grade": A(value=2, confidence=self.conf),
        }

        class R:
            model = "test-1.0"

        r = R()
        r.answers = a
        return r

    def close(self):
        pass


def _words(conf, tmp_path):
    conn = DecisionConnector(_FlagsAdapter(),
                             db_path=str(tmp_path / f"loop-{conf}.db"),
                             client=_Canned(conf))
    try:
        return conn.evaluate({"id": f"row-{conf}"}, ["only"])
    finally:
        conn.close()


def _verbs(decision):
    """The deterministic Action verbs a decision reduces to."""
    return sorted(a.verb for a in decision)


# ── terminal: reduction carries no recheck/escalate ───────────────────

def test_terminal_when_high_confidence_dispatches(tmp_path):
    rows = _words(0.95, tmp_path)
    assert _verbs(rows[0]) == ["dispatch"]  # act; nothing pending


def test_not_terminal_when_low_confidence_escalates(tmp_path):
    rows = _words(0.4, tmp_path)
    assert "escalate" in _verbs(rows[0])  # human takes it; loop stays open


def test_not_terminal_when_middle_flags(tmp_path):
    rows = _words(0.75, tmp_path)
    assert _verbs(rows[0]) == ["dispatch", "flag"]  # acted, but reviewed


def test_mixed_batch_not_terminal(tmp_path):
    high = _words(0.95, tmp_path)
    low = _words(0.3, tmp_path)
    verbs = _verbs(high[0]) + _verbs(low[0])
    assert "escalate" in verbs  # one pending verb keeps the loop open


# ── persistence: close() commits; a fresh connector sees cache ────────

def test_close_persists_and_reopens_cached(tmp_path):
    db = str(tmp_path / "persist.db")
    a = DecisionConnector(_FlagsAdapter(), db_path=db, client=_Canned(0.9))
    a.evaluate({"id": "persist"}, ["only"])
    a.close()  # commit + close

    b = DecisionConnector(_FlagsAdapter(), db_path=db, client=_Canned(0.9))
    try:
        rows = b.evaluate({"id": "persist"}, ["only"])
        assert rows[0].source == "cache"
    finally:
        b.close()


def test_close_idempotent_on_fresh_connector(tmp_path):
    conn = DecisionConnector(_FlagsAdapter(),
                             db_path=str(tmp_path / "fresh.db"),
                             client=_Canned(0.9))
    conn.close()  # no rows yet — must not raise