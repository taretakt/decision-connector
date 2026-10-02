"""Substrate-loop API tests — is_terminal() and flush().

These two additions exist so scripts can loop safely: batch decisions until
terminal, flush the grid, and let the exit code carry the verdict.
"""

import sqlite3

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


def _words(conf):
    """One evaluation at a fixed confidence."""
    conn = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db", client=_Canned(conf))
    try:
        return conn.evaluate({"id": f"row-{conf}"}, ["only"])
    finally:
        conn.flush()


# ── is_terminal ────────────────────────────────────────────────────────

def test_terminal_when_all_rows_act():
    decisions = _words(0.95)
    conn = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db",
                             client=_Canned(0.95))
    try:
        assert conn.is_terminal(decisions) is True
    finally:
        conn.flush()


def test_not_terminal_when_low_confidence_escalates():
    decisions = _words(0.4)
    conn = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db",
                             client=_Canned(0.4))
    try:
        assert conn.is_terminal(decisions) is False
    finally:
        conn.flush()


def test_not_terminal_when_middle_flags():
    decisions = _words(0.75)
    conn = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db",
                             client=_Canned(0.75))
    try:
        assert conn.is_terminal(decisions) is False
    finally:
        conn.flush()


def test_mixed_batch_not_terminal():
    high = _words(0.95)
    low = _words(0.3)
    conn = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db",
                             client=_Canned(0.95))
    try:
        assert conn.is_terminal(high + low) is False
    finally:
        conn.flush()


# ── flush ──────────────────────────────────────────────────────────────

def test_flush_persists_and_closes():
    a = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db",
                          client=_Canned(0.9))
    a.evaluate({"id": "persist"}, ["only"])
    a.flush()  # commit + close

    # a fresh connector on the same grid must see the row as a cache hit
    b = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db",
                          client=_Canned(0.9))
    try:
        rows = b.evaluate({"id": "persist"}, ["only"])
        assert rows[0].source == "cache"
    finally:
        b.flush()


def test_flushed_connection_is_closed():
    conn = DecisionConnector(_FlagsAdapter(), db_path="substrate_loop.db",
                             client=_Canned(0.9))
    conn.evaluate({"id": "close-test"}, ["only"])
    conn.flush()
    try:
        conn.conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return  # closed — good
    raise AssertionError("connection still open after flush()")