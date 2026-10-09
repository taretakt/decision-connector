"""Scrobbler-context enrichment tests — facts, dedupe gate, hopper wiring.

Everything offline: a throwaway sqlite stands in for personal_life.db.
The real scrobbler DB is never opened by this suite.
"""

import os
import sqlite3

import pytest

import funnel
from resource_triage_adapter import ScrobblerContext, TriagePipeline

TIMELINE = """- **[2026-09-28 18:12]** [Engine teardown: 1970s planer mill](https://www.youtube.com/watch?v=AAA111) — *workshop* (`Acc 1`)
- **[2026-09-27 09:40]** [How vLLM batches continuous batching](https://www.youtube.com/watch?v=BBB222) — *vllm-lab* (`Acc 2`)
"""


def _make_scrobbler_db(path):
    """Scrobbler-shaped DB: queue (hopper) + scrobbles (consumption)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, content_type TEXT, url TEXT, author TEXT,
            priority INTEGER, tags TEXT, notes TEXT, status TEXT DEFAULT 'pending',
            source TEXT, created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE scrobbles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, media_type TEXT, title TEXT, creator TEXT,
            url TEXT, external_id TEXT, started_at INTEGER,
            duration_s INTEGER, progress_s INTEGER, status TEXT,
            meta TEXT, created_at INTEGER
        );
        INSERT INTO queue (title, url, priority, status, source)
            VALUES ('Already queued video', 'https://www.youtube.com/watch?v=AAA111', 2, 'pending', 'scanner');
        INSERT INTO scrobbles (title, creator, url, source)
            VALUES ('Already watched video', 'workshop', 'https://www.youtube.com/watch?v=CCC333', 'youtube');
        INSERT INTO scrobbles (title, creator, url, source)
            VALUES ('Past watch', 'workshop', 'https://www.youtube.com/watch?v=DDD444', 'youtube');
        INSERT INTO scrobbles (title, creator, url, source)
            VALUES ('Past watch 2', 'workshop', 'https://www.youtube.com/watch?v=EEE555', 'youtube');
    """)
    conn.commit()
    conn.close()


# ── context loading ────────────────────────────────────────────────────

def test_load_reads_queue_and_scrobbles(tmp_path):
    db = tmp_path / "life.db"
    _make_scrobbler_db(db)
    ctx = ScrobblerContext.load(str(db))
    assert "https://www.youtube.com/watch?v=AAA111" in ctx.queued_urls
    assert "https://www.youtube.com/watch?v=CCC333" in ctx.consumed_urls
    # pending P2 queued row → backlog {P2: 1}
    assert ctx.backlog_by_priority.get("P2") == 1
    # three scrobbles from 'workshop' → channel track record
    assert ctx.channel_scrobbles.get("workshop") == 3


def test_load_missing_db_degrades_empty():
    ctx = ScrobblerContext.load("/nonexistent/no.db")
    assert ctx.queued_urls == set()
    assert ctx.consumed_urls == set()
    assert ctx.channel_scrobbles == {}


def test_load_foreign_schema_degrades_empty(tmp_path):
    db = tmp_path / "foreign.db"
    sqlite3.connect(str(db)).close()  # zero tables
    ctx = ScrobblerContext.load(str(db))
    assert ctx.queued_urls == set()
    assert ctx.consumed_urls == set()


# ── facts_for enrichment ───────────────────────────────────────────────

def test_facts_for_flags_state(tmp_path):
    db = tmp_path / "life.db"
    _make_scrobbler_db(db)
    ctx = ScrobblerContext.load(str(db))

    queued = ctx.facts_for({"url": "https://www.youtube.com/watch?v=AAA111"}, "source")
    assert queued["already_queued"] is True
    assert queued["already_consumed"] is False

    consumed = ctx.facts_for({"url": "https://www.youtube.com/watch?v=CCC333"}, "content")
    assert consumed["already_consumed"] is True
    assert consumed["backlog_P1"] == 0

    chan = ctx.facts_for({"url": "https://x.example", "channel": "workshop"}, "consumption")
    assert chan["channel_track"] == 3


# ── dedupe gate ────────────────────────────────────────────────────────

def test_pipeline_dedupe_gate_queued(tmp_path):
    db = tmp_path / "life.db"
    _make_scrobbler_db(db)
    ctx = ScrobblerContext.load(str(db))
    client = funnel.ScriptedClient(funnel.canned_script(2))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client, context=ctx)
    t = pipe.triage({"title": "Dup", "url": "https://www.youtube.com/watch?v=AAA111"})
    assert t.verdict == "skip"
    assert t.notes == "already in queue (hopper)"
    assert client.calls == []  # no Jev calls spent on a dedupe
    pipe.close()


def test_pipeline_dedupe_gate_consumed(tmp_path):
    db = tmp_path / "life.db"
    _make_scrobbler_db(db)
    ctx = ScrobblerContext.load(str(db))
    client = funnel.ScriptedClient(funnel.canned_script(2))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client, context=ctx)
    t = pipe.triage({"title": "Dup", "url": "https://www.youtube.com/watch?v=CCC333"})
    assert t.verdict == "skip"
    assert t.notes.startswith("already consumed")
    assert client.calls == []
    pipe.close()


def test_pipeline_no_gate_without_context(tmp_path):
    client = funnel.ScriptedClient(funnel.canned_script(2))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    t = pipe.triage({"title": "Fresh", "url": "https://www.youtube.com/watch?v=AAA111"})
    assert t.verdict != "skip"
    pipe.close()


# ── hopper write path ──────────────────────────────────────────────────

def test_write_queue_dedupes_existing_urls(tmp_path):
    db = tmp_path / "q.db"
    funnel.write_queue(
        [{"title": "A", "url": "https://a.example", "content_type": "video"}],
        str(db), tag="funnel")
    # same URL again → blocked by dedupe
    n = funnel.write_queue(
        [{"title": "A", "url": "https://a.example", "content_type": "video"}],
        str(db), tag="funnel")
    assert n == 0
    rows = sqlite3.connect(str(db)).execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    assert rows == 1


def test_hopper_flag_writes_to_context_db(tmp_path):
    db = tmp_path / "life.db"
    _make_scrobbler_db(db)
    corpus = tmp_path / "corpus.md"
    corpus.write_text(TIMELINE)
    rc = funnel.main(["--file", str(corpus), "--hopper", str(db), "--limit", "2"])
    assert rc == 0
    # the queued AAA111 URL is excluded from a fresh write (dedupe gate + write dedupe)
    rows = sqlite3.connect(str(db)).execute(
        "SELECT url, source FROM queue ORDER BY id").fetchall()
    assert any(str(r[1]).startswith("funnel") for r in rows)
    # no duplicate of the pre-existing queued row
    n_aaa = sum(1 for r in rows if r[0] == "https://www.youtube.com/watch?v=AAA111")
    assert n_aaa == 1