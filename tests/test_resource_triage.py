"""Resource-triage pipeline tests — theme → content → consumption.

The pipeline is three connector evaluations chained over one shared grid.
These tests prove: one call per stage on a fresh resource, zero calls on a
warm rerun, verdict/priority plumbing, and the queue-ready emit() row.
All offline against the scripted oracle.
"""

import pytest

from resource_triage_adapter import (
    TriagePipeline,
    ScriptedClient,
    TriageResult,
    _PRIORITY_INT,
)


def _res(**kw):
    base = {
        "title": "Test resource",
        "url": "https://example.org/x",
        "content_type": "article",
        "source": "test",
        "duration_mins": 10,
        "summary": "A test resource with real material in it.",
    }
    base.update(kw)
    return base


def _script(theme=("ai_agents", 0.9), source=("known_channel", 0.8),
            depth=("solid", 0.8), freshness=("current", 0.7),
            verdict=("queue", 0.7), action=("deep_read", 0.6),
            conf=0.8, noul=0.8, prio="P3"):
    def one(choice, prob, misc, score=None):
        return (choice, {choice: prob, misc: 1 - prob}, conf,
                noul if score is None else noul, score)
    return {
        "theme": one(theme[0], theme[1], "misc"),
        "source": one(source[0], source[1], "unknown"),
        "content": one(depth[0], depth[1], "noise", "specific"),
        "freshness": one(freshness[0], freshness[1], "stale"),
        "consume": one(verdict[0], verdict[1], "skip", prio),
        "action": one(action[0], action[1], "reference", "medium"),
    }


def test_theme_stage_one_call_and_winner(tmp_path):
    client = ScriptedClient(_script(theme=("logistics", 0.85)))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client, )
    rows = pipe._theme.evaluate(_res(), None)
    assert len(client.calls) == 1
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "logistics"
    assert winner.noul == pytest.approx(0.85)
    pipe.close()


def test_content_stage_depth_winner(tmp_path):
    client = ScriptedClient(_script(depth=("deep", 0.9)))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    rows = pipe._content.evaluate(_res(), None)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "deep"
    pipe.close()


def test_consumption_verdict_and_priority(tmp_path):
    client = ScriptedClient(_script(verdict=("consume_now", 0.6), prio="P1"))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    rows = pipe._consume.evaluate(_res(), None)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "consume_now"
    assert winner.score == _PRIORITY_INT["P1"]  # 5
    pipe.close()


def test_pipeline_chains_and_emits_queue_row(tmp_path):
    client = ScriptedClient(_script())
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    t = pipe.triage(_res())
    assert isinstance(t, TriageResult)
    assert t.theme == "ai_agents"
    assert t.source_tier == "known_channel"
    assert t.depth == "solid"
    assert t.freshness == "current"
    assert t.verdict == "queue"
    assert t.priority == "P3"
    assert t.action == "deep_read"
    assert t.payoff == "medium"
    row = t.emit()
    assert row["tags"] == "ai_agents"
    assert row["priority"] == "P3"
    assert row["url"] == "https://example.org/x"
    assert "action=deep_read" in row["notes"]
    assert "source=known_channel" in row["notes"]
    assert "freshness=current" in row["notes"]
    pipe.close()


def test_pipeline_warm_rerun_costs_zero_calls(tmp_path):
    client = ScriptedClient(_script())
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    pipe.triage(_res())
    first_calls = len(client.calls)
    assert first_calls == 6  # one per stage
    t2 = pipe.triage(_res())
    assert len(client.calls) == first_calls  # all cache hits
    assert t2.theme == "ai_agents"
    pipe.close()


def test_source_stage_winner(tmp_path):
    client = ScriptedClient(_script(source=("aggregator", 0.55)))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    rows = pipe._source.evaluate(_res(), None)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "aggregator"
    pipe.close()


def test_freshness_stage_winner(tmp_path):
    client = ScriptedClient(_script(freshness=("evergreen", 0.85)))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    rows = pipe._fresh.evaluate(_res(), None)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "evergreen"
    pipe.close()


def test_action_stage_winner_and_payoff(tmp_path):
    client = ScriptedClient(_script(action=("apply", 0.65)))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    rows = pipe._action.evaluate(_res(), None)
    winner = max(rows, key=lambda r: r.noul or 0)
    assert winner.choice == "apply"
    assert winner.score == "medium"
    pipe.close()


def test_noisy_resource_skips(tmp_path):
    client = ScriptedClient(_script(
        theme=("misc", 0.9), depth=("noise", 0.95), verdict=("skip", 0.9)))
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    t = pipe.triage(_res())
    assert t.depth == "noise"
    assert t.verdict == "skip"
    pipe.close()


def test_title_fallback_key_when_no_url(tmp_path):
    client = ScriptedClient(_script())
    pipe = TriagePipeline(db_path=str(tmp_path / "t.db"), client=client)
    t = pipe.triage(_res(url=""))
    assert t.theme == "ai_agents"  # still resolves via title fallback
    pipe.close()