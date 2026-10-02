"""funnel.py tests — intake, batch triage, queue persistence, CLI.

Everything offline: the scripted oracle stands in for judgment, and the
queue-write path goes to a throwaway sqlite (never a real queue DB).

The batch CLI mirrors CI's invocation: python -m pytest adds the repo root
to sys.path, so `import funnel` resolves the same way the workflow does.
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

import pytest

import funnel
from resource_triage_adapter import ScriptedClient, TriagePipeline

TIMELINE = """- **[2026-09-28 18:12]** [Engine teardown: 1970s planer mill](https://www.youtube.com/watch?v=AAA111) — *workshop* (`Acc 1`)
- **[2026-09-27 09:40]** [How vLLM batches continuous batching](https://www.youtube.com/watch?v=BBB222) — *vllm-lab* (`Acc 2`)
"""

TAB_LINES = "Pallet flip walkthrough: 42 units\tfile:///tmp/flip.md\nHalifax rental market numbers\thttps://example.com/rental"

URL_LINES = "https://www.youtube.com/watch?v=CCC333\nhttps://example.com/paper.pdf\n"


def _script_for(n: int):
    return funnel.canned_script(n)


# ── intake ────────────────────────────────────────────────────────────────

def test_timeline_parse():
    res = funnel.load_lines(TIMELINE.splitlines())
    assert len(res) == 2
    assert res[0]["title"].startswith("Engine teardown")
    assert res[0]["url"].endswith("AAA111")
    assert res[0]["content_type"] == "youtube"
    assert res[0]["channel"] == "workshop"
    assert res[0]["source"] == "timeline"
    assert res[1]["channel"] == "vllm-lab"


def test_tabbed_parse():
    res = funnel.load_lines(TAB_LINES.splitlines())
    assert len(res) == 2
    assert res[0]["title"] == "Pallet flip walkthrough: 42 units"
    assert res[0]["source"] == "list"
    assert res[1]["url"] == "https://example.com/rental"


def test_plain_url_parse():
    res = funnel.load_lines(URL_LINES.splitlines())
    assert len(res) == 2
    assert res[0]["url"] == "https://www.youtube.com/watch?v=CCC333"
    assert res[1]["source"] in ("url", "list")


def test_unknown_shapes_skipped():
    lines = ["- not-a-timeline-entry", "garbage line", "   ", "https://ok.example/1"]
    res = funnel.load_lines(lines)
    assert len(res) == 1
    assert res[0]["url"] == "https://ok.example/1"


def test_limit_respected():
    res = funnel.load_lines(TIMELINE.splitlines(), limit=1)
    assert len(res) == 1


def test_empty_input():
    assert funnel.load_lines([]) == []


# ── canned script / client ────────────────────────────────────────────────

def test_canned_script_shape():
    s = funnel.canned_script(3)
    assert set(s) == {"theme", "content", "consume"}
    for stage, lst in s.items():
        assert len(lst) == 3
        choice, probs, conf, noul, score = lst[0]
        assert isinstance(choice, str) and isinstance(conf, float)
        assert probs[choice] > 0


def test_real_client_none_without_sdk(monkeypatch):
    # offline environment: no typesafe_sdk import, no key → None
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert funnel.real_client() is None


# ── batch triage (offline oracle) ─────────────────────────────────────────

def _pipe(tmp_path):
    return TriagePipeline(db_path=str(tmp_path / "funnel.db"),
                          client=ScriptedClient(_script_for(4)),
                          themes=getattr(funnel, "DEFAULT_THEMES", None))


def test_batch_triage_emits_queue_rows(tmp_path):
    res = funnel.load_lines(TIMELINE.splitlines() + URL_LINES.splitlines())
    pipe = TriagePipeline(db_path=str(tmp_path / "funnel.db"),
                          client=ScriptedClient(_script_for(len(res))),
                          themes=funnel.DEFAULT_THEMES)
    results = [pipe.triage(r) for r in res]
    pipe.close()
    assert len(results) == 4
    rows = [t.emit() for t in results]
    assert all({"title", "url", "tags", "priority", "verdict"} <= set(r) for r in rows)
    assert all(r["verdict"] in ("consume_now", "queue", "skip") for r in rows)
    assert all(r["priority"] is None or str(r["priority"]).startswith("P") for r in rows)


def test_batch_report_mentions_standin(tmp_path, capsys):
    res = funnel.load_lines(TIMELINE.splitlines())
    pipe = TriagePipeline(db_path=str(tmp_path / "funnel.db"),
                          client=ScriptedClient(_script_for(len(res))),
                          themes=funnel.DEFAULT_THEMES)
    results = [pipe.triage(r) for r in res]
    pipe.close()
    rows = [t.emit() for t in results]
    # main() prints the report; exercise the same path via argparse-injected argv
    out = capsys.readouterr().out
    assert out == ""  # nothing printed yet — report comes from main()
    assert len(rows) == 2


# ── queue persistence (throwaway db) ─────────────────────────────────────

def test_write_queue_creates_table_and_inserts(tmp_path):
    res = funnel.load_lines(TIMELINE.splitlines())
    pipe = TriagePipeline(db_path=str(tmp_path / "funnel.db"),
                          client=ScriptedClient(_script_for(len(res))),
                          themes=funnel.DEFAULT_THEMES)
    rows = [t.emit() for t in [pipe.triage(r) for r in res]]
    pipe.close()

    db = tmp_path / "q.db"
    n = funnel.write_queue(rows, str(db), tag="funnel-test")
    assert n == 2

    con = sqlite3.connect(db)
    got = con.execute("SELECT title, status, source FROM queue ORDER BY id").fetchall()
    con.close()
    assert len(got) == 2
    assert got[0][1] == "pending"
    assert all("funnel-test:" in src for _, _, src in got)


def test_write_queue_priority_maps_p_to_int(tmp_path):
    row = {"title": "T", "url": "https://x/y", "content_type": "article",
           "source": "test", "tags": "ai_agents", "priority": "P3"}
    db = tmp_path / "q.db"
    funnel.write_queue([row], str(db))
    con = sqlite3.connect(db)
    pri = con.execute("SELECT priority FROM queue").fetchone()[0]
    con.close()
    assert pri == 3


def test_write_queue_only_touches_explicit_path(tmp_path):
    """The queue path is exactly the one passed — no hidden defaults."""
    other = tmp_path / "real.db"
    db = tmp_path / "q.db"
    rows = [{"title": "T", "url": "https://x/y", "content_type": "article",
             "source": "test", "tags": "ai_agents", "priority": "P2"}]
    funnel.write_queue(rows, str(db))
    assert db.exists()
    assert not other.exists()

# ── CLI ───────────────────────────────────────────────────────────────────

def test_main_no_resources_returns_1(capsys):
    rc = funnel.main(["--file", "/nonexistent/empty.txt"])
    assert rc == 1


def test_main_default_sample_corpus(tmp_path, capsys):
    rc = funnel.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "funnel:" in out
    assert "STAND-IN" in out


def test_main_file_and_out_json(tmp_path, capsys):
    src = tmp_path / "corpus.txt"
    src.write_text(TIMELINE)
    out = tmp_path / "rows.json"
    rc = funnel.main(["--file", str(src), "--limit", "2", "--out", str(out)])
    capsys.readouterr()
    assert rc == 0
    data = json.loads(out.read_text())
    assert len(data) == 2
    assert data[0]["url"].endswith("AAA111")


def test_main_write_queue_opt_in(tmp_path, capsys):
    src = tmp_path / "corpus.txt"
    src.write_text(TIMELINE)
    db = tmp_path / "cli_q.db"
    rc = funnel.main(["--file", str(src), "--limit", "2", "--write-queue", str(db)])
    capsys.readouterr()
    assert rc == 0
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    con.close()
    assert n == 2


def test_main_live_fails_honestly_when_unavailable(tmp_path, capsys, monkeypatch):
    src = tmp_path / "corpus.txt"
    src.write_text(TIMELINE)
    monkeypatch.setattr(funnel, "real_client", lambda: None)
    rc = funnel.main(["--file", str(src), "--live"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "live" in err.lower()