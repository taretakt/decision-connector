#!/usr/bin/env python3
# resource_triage_adapter.py
#
# Domain: resource funnel triage — the layer between "a wide net of links"
# and "what should I actually consume".
#
#   subject    — a resource (title, url, content_type, source, duration, ...)
#   candidates — per stage:
#                 stage 1 THEMES      — which theme does this belong to?
#                 stage 2 DEPTH       — how much content is actually in it?
#                 stage 3 CONSUMPTION — what happens to it (skip/queue/consume-now)?
#
# Three separate Jev judgments, chained by TriagePipeline. Each stage is its
# own adapter with its own cache rows, so re-running a stage on a known
# resource costs zero calls. Everything offline-safe via the stand-in
# primitives; swap in a live client + TYPESAFE_API_KEY for the real engine.
#
# Usage:
#   uv run python3 resource_triage_adapter.py
#
# Like every domain, this is one file + six members per stage, zero substrate
# changes. The theme list below is a starter set — it is the one thing that is
# genuinely yours to name.

from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
from typing import Any, Mapping, Optional

from decision_connector import (
    Choice,
    Noul,
    Score,
    DecisionConnector,
    Decision,
)

# ── the one config that is Ross's to own ────────────────────────────────────
DEFAULT_THEMES = {
    "ai_agents": "agents, LLMs, tooling, model behavior",
    "industrial_ops": "plants, mills, processes, operators, P.Eng material",
    "logistics": "freight, routing, linehaul, vehicle operations",
    "making_cad": "CAD, fabrication, machining, build projects",
    "decision_systems": "decision theory, scoring, determinism, red-teaming",
    "real_estate": "halifax, housing, property, renos",
    "markets_arbitrage": "liquidation, flipping, auctions, pricing",
    "creative_music": "music, rap, video, production",
    "health_fitness": "training, diet, biometrics",
    "misc": "everything else",
}

DEPTH_LADDER = ["noise", "fluff", "solid", "deep"]

VERDICTS = ["skip", "queue", "consume_now"]

PRIORITY_LADDER = ["P5", "P4", "P3", "P2", "P1"]

# Score levels → grid-safe int (1..5) that round-trips: level suffix == int.
# The prompt presents the ladder; Jev returns a level label; we store the
# suffix as an int so the REAL grid column stays honest AND f"P{n}" is exact.
_PRIORITY_INT = {"P5": 5, "P4": 4, "P3": 3, "P2": 2, "P1": 1}

# Layers 4-6 of the tree — source trust, freshness, next action.
# The tree is now six deep: theme -> source -> content -> freshness
# -> consumption -> action. Theme list is yours to own; these sets are
# starter ladders like DEPTH_LADDER.
SOURCE_TIERS = ["first_party", "known_channel", "aggregator", "unknown"]
FRESHNESS_LADDER = ["evergreen", "current", "dated", "stale"]
ACTIONS = ["watch_now", "deep_read", "skim", "apply", "reference"]


# ══════════════════════════════════════════════════════════════════════
#  Stage 1 — theme
# ══════════════════════════════════════════════════════════════════════

def _facts_state(resource: dict) -> str:
    """Render scrobbler enrichment facts for the prompt, when present."""
    parts = []
    if resource.get("already_consumed"):
        parts.append("ALREADY CONSUMED — you've scrobbled this exact URL before")
    elif resource.get("already_queued"):
        parts.append("ALREADY QUEUED — this exact URL is in the hopper")
    if resource.get("channel_track") is not None:
        parts.append(f"channel track record: {resource['channel_track']} prior scrobbles")
    if resource.get("channel_freq"):
        parts.append(f"channel appears {resource['channel_freq']}x in the corpus")
    if "backlog_P1" in resource:
        parts.append(f"hopper backlog: P1={resource.get('backlog_P1', 0)} "
                     f"P2={resource.get('backlog_P2', 0)} "
                     f"P3={resource.get('backlog_P3', 0)}")
    return "\n".join(parts)


class ResourceThemeAdapter:
    """Which theme does this resource belong to? one_shot over the theme set."""

    name = "resource.theme"
    evaluation_mode = "one_shot"

    def __init__(self, themes: Mapping[str, str] = DEFAULT_THEMES):
        self._themes = dict(themes)

    def subject_key(self, resource: dict) -> str:
        # Cache key = the resource identity. URL wins; fall back to title hash.
        return resource.get("url") or f"title::{resource.get('title', '?')}"

    def candidate_keys(self, resource: dict, candidates: Any = None) -> list[str]:
        return sorted(self._themes)

    def state_for(self, resource: dict, candidates: Any = None) -> str:
        return resource.get("url", "") + "\n" + resource.get("title", "")

    def questions_for(self, resource, candidates, candidate_key):
        return {
            "theme": Choice(
                instructions=(
                    "Pick the single theme this resource belongs to. "
                    "Use the criteria as the working definition of each theme."
                ),
                criteria=dict(self._themes),
            ),
            "theme_confidence": Noul(
                instructions="Is the primary theme unambiguous?",
                criteria=None,
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["theme"].value,
            noul=getattr(a["theme_confidence"], "value", None),
            confidence=getattr(a["theme"], "confidence", None),
            probabilities=dict(a["theme"].probabilities or {}),
            label=a["theme"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  Stage 2 — content
# ══════════════════════════════════════════════════════════════════════

class ResourceContentAdapter:
    """How much content is actually in it? Depth ladder + substance Noul."""

    name = "resource.content"
    evaluation_mode = "one_shot"

    def subject_key(self, resource: dict) -> str:
        return resource.get("url") or f"title::{resource.get('title', '?')}"

    def candidate_keys(self, resource: dict, candidates: Any = None) -> list[str]:
        return list(DEPTH_LADDER)

    def state_for(self, resource: dict, candidates: Any = None) -> str:
        parts = [resource.get("title", ""), resource.get("source", "")]
        if resource.get("summary"):
            parts.append(resource["summary"])
        return "\n".join(parts)

    def questions_for(self, resource, candidates, candidate_key):
        return {
            "depth": Choice(
                instructions=(
                    "Rate how much original content this resource actually carries. "
                    "'noise' = recycled/clickbait, 'fluff' = thin but real, "
                    "'solid' = substantive, 'deep' = dense, original, worth study."
                ),
                criteria={d: d for d in DEPTH_LADDER},
            ),
            "substance": Noul(
                instructions=(
                    "Does this resource contain real, keepable material "
                    "(facts, methods, examples) rather than filler?"
                ),
                criteria=None,
            ),
            "specificity": Score(
                instructions="How specific is the material to a real problem?",
                criteria=["generic", "topical", "specific", "actionable"],
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["depth"].value,
            noul=getattr(a["substance"], "value", None),
            score=getattr(a["specificity"], "value", None),
            confidence=getattr(a["depth"], "confidence", None),
            probabilities=dict(a["depth"].probabilities or {}),
            label=a["depth"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  Stage 3 — consumption
# ══════════════════════════════════════════════════════════════════════

class ResourceConsumptionAdapter:
    """What happens to this resource? Verdict + attention Noul + priority."""

    name = "resource.consumption"
    evaluation_mode = "one_shot"

    def subject_key(self, resource: dict) -> str:
        return resource.get("url") or f"title::{resource.get('title', '?')}"

    def candidate_keys(self, resource: dict, candidates: Any = None) -> list[str]:
        return list(VERDICTS)

    def state_for(self, resource: dict, candidates: Any = None) -> str:
        parts = [
            f"title: {resource.get('title', '')}",
            f"type: {resource.get('content_type', '')}",
            f"source: {resource.get('source', '')}",
        ]
        if resource.get("duration_mins"):
            parts.append(f"duration: {resource['duration_mins']} minutes")
        if resource.get("theme"):
            parts.append(f"theme: {resource['theme']}")
        if resource.get("depth"):
            parts.append(f"content depth: {resource['depth']}")
        f = _facts_state(resource)
        if f:
            parts.append(f)
        return "\n".join(parts)

    def questions_for(self, resource, candidates, candidate_key):
        return {
            "verdict": Choice(
                instructions=(
                    "Decide what should happen to this resource. "
                    "'skip' = not worth the attention slot, "
                    "'queue' = save it for later, "
                    "'consume_now' = it should go on soon."
                ),
                criteria={v: v for v in VERDICTS},
            ),
            "attention": Noul(
                instructions=(
                    "Is this worth a real attention slot this week, "
                    "or is it background/thin material?"
                ),
                criteria=None,
            ),
            "priority": Score(
                instructions=(
                    "How urgent is this relative to everything else waiting? "
                    "P5 = whenever, P1 = as soon as possible."
                ),
                criteria=list(PRIORITY_LADDER),
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["verdict"].value,
            noul=getattr(a["attention"], "value", None),
            score=_PRIORITY_INT.get(getattr(a["priority"], "value", None),
                                    getattr(a["priority"], "value", None)),
            confidence=getattr(a["verdict"], "confidence", None),
            probabilities=dict(a["verdict"].probabilities or {}),
            label=a["verdict"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  Stage 4 — source credibility (layer 4)
# ══════════════════════════════════════════════════════════════════════

class ResourceSourceAdapter:
    """Who stands behind this resource? Tier + credibility Noul.

    Kills the noise-machine channels early: an 'aggregator' that rehosts
    recycled clips scores low on credibility and is demoted before the
    expensive content stage ever sees it.
    """

    name = "resource.source"
    evaluation_mode = "one_shot"

    def subject_key(self, resource: dict) -> str:
        return resource.get("url") or f"title::{resource.get('title', '?')}"

    def candidate_keys(self, resource: dict, candidates: Any = None) -> list[str]:
        return list(SOURCE_TIERS)

    def state_for(self, resource: dict, candidates: Any = None) -> str:
        parts = [f"title: {resource.get('title', '')}",
                 f"source: {resource.get('source', '')}",
                 f"channel: {resource.get('channel', '')}"]
        if resource.get("duration_mins"):
            parts.append(f"duration: {resource['duration_mins']} minutes")
        f = _facts_state(resource)
        if f:
            parts.append(f)
        return "\n".join(parts)

    def questions_for(self, resource, candidates, candidate_key):
        return {
            "source_tier": Choice(
                instructions=(
                    "Rate who stands behind this resource. 'first_party' "
                    "= the original author/creator; 'known_channel' = "
                    "established publisher with a track record; "
                    "'aggregator' = rehosts/recycles other people's "
                    "content; 'unknown' = can't tell."
                ),
                criteria={t: t for t in SOURCE_TIERS},
            ),
            "credibility": Noul(
                instructions=(
                    "Does this source have a real track record of "
                    "substance, or is it a clip-mill/affiliate funnel?"
                ),
                criteria=None,
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["source_tier"].value,
            noul=getattr(a["credibility"], "value", None),
            confidence=getattr(a["source_tier"], "confidence", None),
            probabilities=dict(a["source_tier"].probabilities or {}),
            label=a["source_tier"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  Stage 5 — freshness (layer 5)
# ══════════════════════════════════════════════════════════════════════

class ResourceFreshnessAdapter:
    """How does this resource age? Timeliness tier + decay Noul.

    A 2014 hip-hop video and a 2026 Halifax rental-market post do not
    decay alike. This layer feeds the priority ladder so dated material
    can't win the queue just because it's deep.
    """

    name = "resource.freshness"
    evaluation_mode = "one_shot"

    def subject_key(self, resource: dict) -> str:
        return resource.get("url") or f"title::{resource.get('title', '?')}"

    def candidate_keys(self, resource: dict, candidates: Any = None) -> list[str]:
        return list(FRESHNESS_LADDER)

    def state_for(self, resource: dict, candidates: Any = None) -> str:
        parts = [f"title: {resource.get('title', '')}"]
        if resource.get("captured_at"):
            parts.append(f"captured: {resource['captured_at']}")
        if resource.get("content_type"):
            parts.append(f"type: {resource['content_type']}")
        if resource.get("theme"):
            parts.append(f"theme: {resource['theme']}")
        f = _facts_state(resource)
        if f:
            parts.append(f)
        return "\n".join(parts)

    def questions_for(self, resource, candidates, candidate_key):
        return {
            "freshness": Choice(
                instructions=(
                    "How does this resource age? 'evergreen' = stays true "
                    "(technique, theory); 'current' = valid now, some "
                    "shelf life; 'dated' = mostly stale, maybe one useful "
                    "idea; 'stale' = overtaken."
                ),
                criteria={f: f for f in FRESHNESS_LADDER},
            ),
            "decay": Noul(
                instructions=(
                    "How fast does this lose value? High decay = prices, "
                    "news, tool versions. Low decay = fundamentals."
                ),
                criteria=None,
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["freshness"].value,
            noul=getattr(a["decay"], "value", None),
            confidence=getattr(a["freshness"], "confidence", None),
            probabilities=dict(a["freshness"].probabilities or {}),
            label=a["freshness"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  Stage 6 — next action (layer 6)
# ══════════════════════════════════════════════════════════════════════

class ResourceActionAdapter:
    """What do we DO with this resource? Action + commitment + payoff.

    Terminal layer: turns a queue verdict into an executable next step.
    'queue P2' becomes 'watch_now P2' vs 'reference P2' — the row now
    says what happens when it reaches the top of the stack.
    """

    name = "resource.action"
    evaluation_mode = "one_shot"

    def subject_key(self, resource: dict) -> str:
        return resource.get("url") or f"title::{resource.get('title', '?')}"

    def candidate_keys(self, resource: dict, candidates: Any = None) -> list[str]:
        return list(ACTIONS)

    def state_for(self, resource: dict, candidates: Any = None) -> str:
        parts = [f"title: {resource.get('title', '')}"]
        for k in ("theme", "depth", "freshness", "source_tier"):
            if resource.get(k):
                parts.append(f"{k}: {resource[k]}")
        if resource.get("verdict"):
            parts.append(f"verdict: {resource['verdict']}")
        if resource.get("priority"):
            parts.append(f"priority: {resource['priority']}")
        f = _facts_state(resource)
        if f:
            parts.append(f)
        return "\n".join(parts)

    def questions_for(self, resource, candidates, candidate_key):
        return {
            "action": Choice(
                instructions=(
                    "Pick the next concrete step for this resource. "
                    "'watch_now' = consume it today; 'deep_read' = "
                    "study it closely; 'skim' = 5-minute scan; 'apply' = "
                    "do something with it (build/implement/write); "
                    "'reference' = keep as lookup material."
                ),
                criteria={a: a for a in ACTIONS},
            ),
            "commitment": Noul(
                instructions=(
                    "Does this deserve a real attention slot, or is it a "
                    "skim/reference item?"
                ),
                criteria=None,
            ),
            "payoff": Score(
                instructions=(
                    "How much is likely to come back from doing this — "
                    "knowledge, money, or craft?"
                ),
                criteria=["low", "medium", "high", "transformative"],
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["action"].value,
            noul=getattr(a["commitment"], "value", None),
            score=getattr(a["payoff"], "value", None),
            confidence=getattr(a["action"], "confidence", None),
            probabilities=dict(a["action"].probabilities or {}),
            label=a["action"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  Pipeline
# ══════════════════════════════════════════════════════════════════════

@dataclass
class TriageResult:
    resource: dict
    theme: Optional[str] = None
    theme_confidence: Optional[float] = None
    source_tier: Optional[str] = None
    source_cred: Optional[float] = None
    depth: Optional[str] = None
    substance: Optional[bool] = None
    freshness: Optional[str] = None
    decay: Optional[float] = None
    verdict: Optional[str] = None
    attention: Optional[bool] = None
    priority: Optional[str] = None
    action: Optional[str] = None
    commitment: Optional[float] = None
    payoff: Optional[str] = None
    route: Optional[str] = None
    notes: Optional[str] = None

    def emit(self) -> dict:
        """Queue-ready row, shaped for the scrobbler queue schema."""
        return {
            "title": self.resource.get("title"),
            "url": self.resource.get("url"),
            "content_type": self.resource.get("content_type"),
            "source": self.resource.get("source"),
            "tags": self.theme,
            "priority": self.priority,
            "verdict": self.verdict,
            "depth": self.depth,
                        "notes": (f"theme={self.theme}; source={self.source_tier}; "
                      f"freshness={self.freshness}; action={self.action}; "
                      f"{self.notes or ''}").strip("; ")        }


@dataclass
class ScrobblerContext:
    """What the scrobbler already knows — injected into every stage's state.

    Each stage's prompt then carries facts beyond the bare resource: whether
    it's already queued/consumed, the channel's consumption track record, and
    current backlog pressure. Judgment is made against what you've already
    done, not just what this one link is.
    """
    queued_urls: set = field(default_factory=set)
    consumed_urls: set = field(default_factory=set)
    channel_scrobbles: Counter = field(default_factory=Counter)
    backlog_by_priority: Counter = field(default_factory=Counter)
    channel_freq: Counter = field(default_factory=Counter)

    def facts_for(self, resource: dict, stage: str) -> dict:
        url = resource.get("url", "")
        facts = {
            "already_queued": url in self.queued_urls,
            "already_consumed": url in self.consumed_urls,
            "backlog_P1": self.backlog_by_priority.get("P1", 0),
            "backlog_P2": self.backlog_by_priority.get("P2", 0),
            "backlog_P3": self.backlog_by_priority.get("P3", 0),
        }
        chan = resource.get("channel") or resource.get("author")
        if chan:
            facts["channel_track"] = self.channel_scrobbles.get(chan, 0)
        if resource.get("channel_freq") or self.channel_freq.get(chan or ""):
            facts["channel_freq"] = resource.get("channel_freq") or \
                self.channel_freq.get(chan or "", 0)
        return facts

    @classmethod
    def load(cls, db_path, channel_freq: Counter | None = None):
        """Read the scrobbler DB (queue + scrobbles tables) defensively.

        Missing tables / unreadable DB / foreign schema all degrade to an
        empty context — enrichment is a bonus, never a hard dependency.
        """
        import sqlite3
        queued, consumed = set(), set()
        chan_sc, backlog = Counter(), Counter()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "queue" in tables:
                for r in conn.execute("SELECT url, priority, status FROM queue"):
                    if r["url"]:
                        queued.add(r["url"])
                    if r["status"] != "done" and r["priority"]:
                        backlog[f"P{int(r['priority'])}"] += 1
            if "scrobbles" in tables:
                for r in conn.execute("SELECT url, creator FROM scrobbles"):
                    if r["url"]:
                        consumed.add(r["url"])
                    if r["creator"]:
                        chan_sc[r["creator"]] += 1
            conn.close()
        except Exception:
            pass
        return cls(queued_urls=queued, consumed_urls=consumed,
                  channel_scrobbles=chan_sc, backlog_by_priority=backlog,
                  channel_freq=channel_freq or Counter())


class TriagePipeline:
    """Chain the six stages over one connector grid (shared db).

    theme -> source -> content -> freshness -> consumption -> action
    """

    def __init__(self, db_path="triage.db", client=None,
                 themes: Mapping[str, str] = DEFAULT_THEMES,
                 context: ScrobblerContext | None = None):
        self.themes = dict(themes)
        self.context = context
        self._theme = DecisionConnector(ResourceThemeAdapter(themes),
                                        db_path=db_path, client=client)
        self._source = DecisionConnector(ResourceSourceAdapter(),
                                         db_path=db_path, client=client)
        self._content = DecisionConnector(ResourceContentAdapter(),
                                          db_path=db_path, client=client)
        self._fresh = DecisionConnector(ResourceFreshnessAdapter(),
                                        db_path=db_path, client=client)
        self._consume = DecisionConnector(ResourceConsumptionAdapter(),
                                          db_path=db_path, client=client)
        self._action = DecisionConnector(ResourceActionAdapter(),
                                         db_path=db_path, client=client)

    def triage(self, resource: dict, force: bool = False) -> TriageResult:
        # Dedupe gate — deterministic, no judgment involved. If the scrobbler
        # already knows this URL (queued or consumed), it does not re-enter
        # the hopper. Returns a skip result without spending a single call.
        if self.context and resource.get("url"):
            if resource["url"] in self.context.consumed_urls:
                return TriageResult(resource=resource, verdict="skip",
                                    route="act", priority=None, action=None,
                                    notes="already consumed (scrobbler)")
            if resource["url"] in self.context.queued_urls:
                return TriageResult(resource=resource, verdict="skip",
                                    route="act", priority=None, action=None,
                                    notes="already in queue (hopper)")

        # Stage 1 — theme
        rows = self._theme.evaluate(resource, None, force=force)
        winner = max(rows, key=lambda r: r.noul or 0)
        theme, theme_conf = winner.choice, winner.noul

        staged = dict(resource)
        staged["theme"], staged["depth"] = theme, None

        # Enrichment — every later stage sees the scrobbler facts + theme.
        if self.context:
            staged.update(self.context.facts_for(staged, "source"))

        # Stage 2 — source
        rows = self._source.evaluate(staged, None, force=force)
        swin = max(rows, key=lambda r: r.noul or 0)
        source_tier = swin.choice
        _cred = swin.raw.get("adapter_noul")
        source_cred = _cred if _cred is not None else swin.noul

        staged["source_tier"] = source_tier
        if self.context:
            staged.update(self.context.facts_for(staged, "content"))

        # Stage 3 — content
        rows = self._content.evaluate(staged, None, force=force)
        cwin = max(rows, key=lambda r: r.noul or 0)
        _sub = cwin.raw.get("adapter_noul")
        depth = cwin.choice
        substance = _sub > 0.5 if _sub is not None else None

        staged["depth"] = depth
        if self.context:
            staged.update(self.context.facts_for(staged, "freshness"))

        # Stage 4 — freshness
        rows = self._fresh.evaluate(staged, None, force=force)
        fwin = max(rows, key=lambda r: r.noul or 0)
        freshness = fwin.choice
        _dec = fwin.raw.get("adapter_noul")
        decay = _dec if _dec is not None else fwin.noul

        staged["freshness"] = freshness
        if self.context:
            staged.update(self.context.facts_for(staged, "consumption"))

        # Stage 5 — consumption
        rows = self._consume.evaluate(staged, None, force=force)
        vwin = max(rows, key=lambda r: r.noul or 0)
        verdict = vwin.choice
        _att = vwin.raw.get("adapter_noul")
        attention = None if _att is None else _att > 0.5
        pri = vwin.score  # int 1..5 from the grid, or a level label from live
        priority = pri if isinstance(pri, str) else f"P{int(pri)}" if pri else None

        staged["verdict"], staged["priority"] = verdict, priority
        if self.context:
            staged.update(self.context.facts_for(staged, "action"))

        # Stage 6 — action
        rows = self._action.evaluate(staged, None, force=force)
        awin = max(rows, key=lambda r: r.noul or 0)
        action = awin.choice
        _com = awin.raw.get("adapter_noul")
        commitment = _com if _com is not None else awin.noul
        payoff = awin.score

        return TriageResult(
            resource=resource,
            theme=theme, theme_confidence=theme_conf,
            source_tier=source_tier, source_cred=source_cred,
            depth=depth, substance=bool(substance) if substance is not None else None,
            freshness=freshness, decay=decay,
            verdict=verdict, attention=bool(attention) if attention is not None else None,
            priority=priority, route=self._consume.route(vwin),
            action=action, commitment=commitment, payoff=payoff,
        )

    def close(self):
        for c in (self._theme, self._source, self._content,
                  self._fresh, self._consume, self._action):
            c.close()


# ══════════════════════════════════════════════════════════════════════
#  Offline demo oracle
# ══════════════════════════════════════════════════════════════════════

class _A:
    def __init__(s, **kw):
        s.__dict__.update(kw)


class ScriptedClient:
    """Deterministic offline oracle: stage/question → canned answer tuple.

    `script` maps stage → tuple (choice, probs, conf, noul, score) OR
    stage → list of such tuples (consumed in order, one per call) — the
    demo uses lists so each resource gets a different verdict.
    """

    def __init__(self, script: Mapping[str, object]):
        self.script = {k: list(v) if isinstance(v, (list, tuple)) and
                       v and isinstance(v[0], tuple) else v
                       for k, v in script.items()}
        self.calls = []

    def _take(self, key: str):
        v = self.script[key]
        if isinstance(v, list):
            return v.pop(0) if v else v
        return v

    def system_one(self, state, questions):
        if "theme" in questions:
            key = "theme"
        elif "source_tier" in questions:
            key = "source"
        elif "depth" in questions:
            key = "content"
        elif "freshness" in questions:
            key = "freshness"
        elif "verdict" in questions:
            key = "consume"
        else:
            key = "action"
        choice, probs, conf, noul, score = self._take(key)
        self.calls.append(key)
        class R:
            model = "stand-in-1.0"
        r = R()
        probs_map = {k: v for k, v in probs.items()}
        r.answers = {
            # stage 1 — theme
            "theme": _A(value=choice, probabilities=probs_map, confidence=conf),
            "theme_confidence": _A(value=noul),
            # stage 2 — source
            "source_tier": _A(value=choice, probabilities=probs_map, confidence=conf),
            "credibility": _A(value=noul),
            # stage 3 — content
            "depth": _A(value=choice, probabilities=probs_map, confidence=conf),
            "substance": _A(value=noul),
            "specificity": _A(value=score),
            # stage 4 — freshness
            "freshness": _A(value=choice, probabilities=probs_map, confidence=conf),
            "decay": _A(value=noul),
            # stage 5 — consumption
            "verdict": _A(value=choice, probabilities=probs_map, confidence=conf),
            "attention": _A(value=noul),
            "priority": _A(value=score),
            # stage 6 — action
            "action": _A(value=choice, probabilities=probs_map, confidence=conf),
            "commitment": _A(value=noul),
            "payoff": _A(value=score),
        }
        return r

    def close(self):
        pass


# ══════════════════════════════════════════════════════════════════════
#  Demo
# ══════════════════════════════════════════════════════════════════════

DEMO_RESOURCES = [
    {
        "title": "How vLLM batches continuous batching under the hood",
        "url": "https://youtube.com/watch?v=abc123",
        "content_type": "youtube",
        "source": "youtube-likes",
        "duration_mins": 34,
        "summary": "Deep dive into paged attention and scheduler internals.",
    },
    {
        "title": "Engine teardown: a 1970s planer mill restoration",
        "url": "https://youtube.com/watch?v=def456",
        "content_type": "youtube",
        "source": "youtube-likes",
        "duration_mins": 48,
        "summary": "Full teardown, machining work, and reassembly with tolerances.",
    },
    {
        "title": "13 signs AI is eating the economy",
        "url": "https://news.ycombinator.com/item?id=9999",
        "content_type": "article",
        "source": "hn",
        "duration_mins": 8,
        "summary": "Listicle, mostly recycled talking points.",
    },
    {
        "title": "Halifax rental market: October numbers",
        "url": "https://example.org/hfx-oct",
        "content_type": "article",
        "source": "rss",
        "duration_mins": 12,
        "summary": "New supply, rent growth, and vacancy by neighbourhood.",
    },
]


def _default_script():
    # Per-resource scripts, consumed in DEMO_RESOURCES order.
    # (choice, distribution, confidence, noul, score)
    def pack(choice, prob, conf, noul, score):
        rest = 1 - prob
        dist = {choice: prob, "misc" if choice != "misc" else "ai_agents": rest}
        return (choice, dist, conf, noul, score)

    themes = [
        pack("ai_agents", 0.9, 0.9, 0.9, None),      # vLLM deep dive
        pack("making_cad", 0.9, 0.9, 0.9, None),     # planer mill
        pack("ai_agents", 0.55, 0.55, 0.5, None),    # listicle, shaky theme
        pack("real_estate", 0.92, 0.92, 0.95, None), # Halifax numbers
    ]
    depths = [
        pack("deep", 0.68, 0.68, 0.75, "actionable"),
        pack("deep", 0.7, 0.7, 0.8, "specific"),
        pack("fluff", 0.62, 0.62, 0.3, "generic"),
        pack("solid", 0.6, 0.6, 0.65, "topical"),
    ]
    consumptions = [
        pack("consume_now", 0.65, 0.65, 0.8, "P1"),
        pack("queue", 0.6, 0.6, 0.7, "P3"),
        pack("skip", 0.8, 0.8, 0.25, "P5"),
        pack("queue", 0.55, 0.55, 0.6, "P2"),
    ]
    sources = [
        pack("known_channel", 0.7, 0.7, 0.6, None),     # vLLM lab
        pack("first_party", 0.8, 0.8, 0.7, None),       # workshop rebuild
        pack("aggregator", 0.6, 0.6, 0.3, None),        # listicle mill
        pack("known_channel", 0.75, 0.75, 0.65, None),  # real-estate data
    ]
    freshness = [
        pack("current", 0.6, 0.6, 0.55, None),   # LLM tooling, decays
        pack("evergreen", 0.8, 0.8, 0.75, None), # machine restoration
        pack("dated", 0.7, 0.7, 0.4, None),      # listicle already stale
        pack("current", 0.65, 0.65, 0.6, None),  # market numbers move
    ]
    actions = [
        pack("deep_read", 0.5, 0.5, 0.7, "high"),
        pack("apply", 0.5, 0.5, 0.75, "transformative"),
        pack("skim", 0.6, 0.6, 0.3, "low"),
        pack("reference", 0.55, 0.55, 0.45, "medium"),
    ]
    return {"theme": themes, "content": depths, "consume": consumptions,
            "source": sources, "freshness": freshness, "action": actions}


if __name__ == "__main__":
    print("=" * 68)
    print("  RESOURCE TRIAGE — 6-layer decision tree")
    print("  theme → source → content → freshness → consumption → action")
    print("  mode: OFFLINE (scripted oracle)")
    print("=" * 68)

    client = ScriptedClient(_default_script())
    pipe = TriagePipeline(db_path="_triage_demo.db", client=client)

    for res in DEMO_RESOURCES:
        t = pipe.triage(res)
        print(f"\n▶ {res['title'][:58]}")
        print(f"  theme      : {t.theme}  (conf={t.theme_confidence})")
        print(f"  source     : {t.source_tier}  credibility={t.source_cred:.2f}")
        print(f"  content    : {t.depth}  substance={t.substance}")
        print(f"  freshness  : {t.freshness}  decay={t.decay:.2f}")
        print(f"  consumption: {t.verdict}  priority={t.priority}  "
              f"attention={t.attention}  route={t.route}")
        print(f"  action     : {t.action}  commitment={t.commitment:.2f}  "
              f"payoff={t.payoff}")

    print(f"\nJEV CALLS: {len(client.calls)}  (6 per resource × {len(DEMO_RESOURCES)})")
    pipe.close()
    import os
    for f in ("_triage_demo.db",):
        if os.path.exists(f):
            os.remove(f)
    print("\nALL GREEN — substrate untouched, funnel carried by one file.")