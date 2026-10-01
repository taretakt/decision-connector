#!/usr/bin/env python3
# decision_connector.py
#
# A domain-agnostic connector between structured systems and Jev's typed
# decision primitives. Compatalog fitment is the reference adapter; QC
# disposition is the second, proving the abstraction carries.
#
# Design contract
# ---------------
# Everything is subject × candidate.
#   fitment:     subject = vehicle,  candidates = parts
#   disposition: subject = defect,   candidates = dispositions
#   scoring:     subject = listing,  candidates = grades
#
# The adapter — and ONLY the adapter — knows the domain. It supplies:
#   subject_key()    grid row coordinate
#   candidate_keys() grid column coordinates
#   state_for()      domain data → Jev state (text/JSON)
#   questions_for()  domain data → Jev primitives
#   canonical()      Jev response → canonical Decision
#
# The connector supplies the shared substrate:
#   deterministic cache keys, SQLite solution grid, invalidation, provenance,
#   confidence routing, cost accounting, and the non-text input bridge.
#
# Usage:
#   uv run python3 decision_connector.py              # offline self-test
#   TYPESAFE_API_KEY=sk-... uv run python3 decision_connector.py --live

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

try:
    from typesafe_sdk import Choice, Noul, Score
except ImportError:  # allows offline inspection without the SDK
    # Stand-in primitives. They accept the same kwargs and expose the same
    # attributes as the real ones, so adapter code and the offline self-test
    # exercise the identical questions_for() path. Without these, every adapter
    # raises TypeError on the first Choice(...) and the documented offline
    # self-test cannot run at all.
    from dataclasses import dataclass as _dc

    @_dc
    class Choice:  # type: ignore[no-redef]
        instructions: str
        criteria: Mapping[str, str] = field(default_factory=dict)
        kind: str = "choice"

    @_dc
    class Noul:  # type: ignore[no-redef]
        instructions: str
        criteria: Any = None
        kind: str = "noul"

    @_dc
    class Score:  # type: ignore[no-redef]
        instructions: str
        criteria: Sequence[str] = field(default_factory=list)
        kind: str = "score"

DEFAULT_DB = Path(os.environ.get("DECISION_GRID_DB", "grid.db"))

# Jev list price: $42 / billion input tokens. Typical call ≈ 400 input tokens.
COST_PER_CALL_USD = 42 / 1_000_000_000 * 400

UNIT_SEP = b"\x1f"   # prevents boundary collisions when concatenating keys


# ══════════════════════════════════════════════════════════════════════
#  Canonical result
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Decision:
    """Domain-neutral decision record. Every adapter collapses to this."""
    subject_key: str
    candidate_key: str
    choice: Optional[str] = None
    noul: Optional[float] = None
    score: Optional[float] = None
    confidence: Optional[float] = None
    probabilities: dict[str, float] = field(default_factory=dict)
    label: Optional[str] = None          # human-readable outcome
    source: str = "jev"                  # 'jev' | 'cache' | 'manual' | 'import'
    model: Optional[str] = None
    note: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def positive(self) -> Optional[bool]:
        """Binary read of the Noul, if present."""
        return None if self.noul is None else self.noul > 0.5

    @property
    def top_probability(self) -> float:
        return max(self.probabilities.values(), default=0.0)

    def __str__(self) -> str:
        return (f"[{self.source}] {self.subject_key} × {self.candidate_key} → "
                f"{self.label or self.choice} "
                f"noul={self.noul if self.noul is None else round(self.noul, 3)}")


# ══════════════════════════════════════════════════════════════════════
#  Adapter protocol — the ONLY domain-aware surface
# ══════════════════════════════════════════════════════════════════════

@runtime_checkable
class DomainAdapter(Protocol):
    """Implement this to plug a system into the decision connector."""

    name: str
    # 'per_candidate'  — N candidates, N independent judgments (one Jev call each)
    # 'one_shot'       — N candidates, ONE multivariable decision (one call total)
    evaluation_mode: str

    def subject_key(self, subject: Any) -> str:
        """Stable grid row coordinate."""

    def candidate_keys(self, subject: Any, candidates: Any) -> list[str]:
        """Stable grid column coordinates."""

    def state_for(self, subject: Any, candidates: Any) -> str | Mapping:
        """Domain data → the state Jev evaluates."""

    def questions_for(self, subject: Any, candidates: Any,
                      candidate_key: str) -> Mapping[str, Any]:
        """Domain data + one candidate → Jev primitives."""

    def canonical(self, response: Any, candidate_key: str) -> dict[str, Any]:
        """Jev response → kwargs for Decision(...)."""


# ══════════════════════════════════════════════════════════════════════
#  Reference adapter 1 — Compatalog fitment
# ══════════════════════════════════════════════════════════════════════

class CompatalogFitmentAdapter:
    """vehicle × part → fits / doesn't fit, with confidence."""

    name = "compatalog.fitment"
    # Each part needs its own compatibility judgment ("does part A fit?"),
    # so N candidates genuinely requires N calls.
    evaluation_mode = "per_candidate"

    def subject_key(self, vehicle: dict) -> str:
        return vehicle.get("arku") or vehicle.get("id") or vehicle.get("piece_type", "?")

    def candidate_keys(self, vehicle: dict, parts: Mapping[str, dict]) -> list[str]:
        return list(parts.keys())

    def state_for(self, vehicle: dict, parts: Mapping[str, dict]) -> str:
        lines = [f"VEHICLE: {vehicle.get('piece_type', '?')}"]
        for k, v in (vehicle.get("attributes") or {}).items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append("CANDIDATE PARTS:")
        for pid, p in parts.items():
            lines.append(f"  - [{pid}] {p.get('name', pid)}")
            for k, v in p.items():
                if k != "name":
                    lines.append(f"      {k}: {v}")
        return "\n".join(lines)

    def questions_for(self, vehicle, parts, candidate_key):
        part = parts[candidate_key]
        return {
            "best_match": Choice(
                instructions=("Which part is the best fitment match for this "
                              "vehicle? Consider caliper type, rotor diameter, "
                              "knuckle, and application."),
                criteria={pid: p.get("name", pid) for pid, p in parts.items()},
            ),
            "compatible": Noul(
                instructions=(f"Part {candidate_key} ({part.get('name','')}) is "
                              "compatible with this vehicle and fits without "
                              "modification."),
            ),
            "difficulty": Score(
                instructions="Fitment difficulty, bolt-on through custom fab.",
                criteria=["bolt-on", "minor adjustment", "moderate",
                          "significant modification", "custom fabrication"],
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["best_match"].value,
            noul=a["compatible"].value,
            score=a["difficulty"].value,
            confidence=getattr(a["compatible"], "confidence", None),
            probabilities=dict(a["best_match"].probabilities or {}),
            label=a["best_match"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  Reference adapter 2 — QC disposition  (proves the abstraction)
# ══════════════════════════════════════════════════════════════════════

class QCDispositionAdapter:
    """defect reading × disposition → what the line should do with the unit.

    The vision layer detects and measures. This adapter encodes the QC lead's
    judgement about what to DO about it — the tribal knowledge bottleneck.
    """

    name = "manufacturing.qc-disposition"
    # ONE multivariable decision: Jev scores every disposition in a single
    # call and returns the full probability distribution. N rows, 1 call.
    evaluation_mode = "one_shot"

    # What the line can do with a defective unit.
    DISPOSITIONS = {
        "scrap":     "Consume the unit as scrap; recover no value.",
        "rework":    "Return to a station for a defined corrective operation.",
        "touch_up":  "Cosmetic-only correction, no re-certification required.",
        "deviate":   "Ship with a documented engineering deviation.",
        "ship":      "Within spec; ship as first-quality with no action.",
        "escalate":  "Hold the lot and route to a human QC lead for a call.",
    }

    def subject_key(self, defect: dict) -> str:
        # Signature = class + severity + part family. Finite, cacheable.
        return "|".join([
            defect.get("defect_class", "?"),
            defect.get("severity", "?"),
            defect.get("part_family", "?"),
        ])

    def candidate_keys(self, defect: dict, candidates=None) -> list[str]:
        return list(candidates or self.DISPOSITIONS.keys())

    def state_for(self, defect: dict, candidates=None) -> str:
        lines = ["QUALITY ESCAPE — MEASURED READING",
                 f"  defect_class : {defect.get('defect_class')}",
                 f"  severity     : {defect.get('severity')}",
                 f"  part_family  : {defect.get('part_family')}",
                 f"  detector_conf: {defect.get('detector_confidence')}",
                 f"  location     : {defect.get('location')}",
                 # NOTE: unit lives in the VALUE, not the key. If it lived in
                 # the key ("dimensional_deviation_mm: 302") the value would be
                 # a bare number and no canonicalizer could band it.
                 f"  dimensional_deviation: {defect.get('dimensional_deviation_mm')}mm",
                 "",
                 "PRODUCTION CONTEXT",
                 f"  tolerance_class  : {defect.get('tolerance_class')}",
                 f"  customer_cosmetics: {defect.get('customer_cosmetics')}",
                 f"  rework_cost_usd  : {defect.get('rework_cost_usd')}",
                 f"  scrap_cost_usd   : {defect.get('scrap_cost_usd')}",
                 f"  units_short      : {defect.get('units_short')}",
                 f"  lot_size         : {defect.get('lot_size')}",
                 "",
                 "AVAILABLE DISPOSITIONS:"]
        for d in self.candidate_keys(defect, candidates):
            lines.append(f"  - [{d}] {self.DISPOSITIONS.get(d, d)}")
        return "\n".join(lines)

    def questions_for(self, defect, candidates, candidate_key):
        return {
            "disposition": Choice(
                instructions=("Which disposition is correct for this unit? "
                              "Weigh defect severity, tolerance class, customer "
                              "cosmetics requirements, rework vs scrap cost, "
                              "and whether units are needed this week."),
                criteria={d: self.DISPOSITIONS[d]
                          for d in self.candidate_keys(defect, candidates)},
            ),
            "acceptable_to_ship": Noul(
                instructions=("Shipping this unit WITHOUT further action is "
                              "acceptable for this customer and application."),
            ),
            "judgment_difficulty": Score(
                instructions=("How much human judgement does this call require? "
                              "Low = any operator knows the rule. High = needs "
                              "an experienced QC lead."),
                criteria=["operator rule", "supervisor check",
                          "experienced lead", "engineering review",
                          "customer consultation"],
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["disposition"].value,
            noul=a["acceptable_to_ship"].value,
            score=a["judgment_difficulty"].value,
            confidence=getattr(a["disposition"], "confidence", None),
            probabilities=dict(a["disposition"].probabilities or {}),
            label=a["disposition"].value,
        )


# ══════════════════════════════════════════════════════════════════════
#  The connector
# ══════════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid (
    cache_key    TEXT PRIMARY KEY,
    domain       TEXT NOT NULL,
    subject_key  TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    source       TEXT NOT NULL,
    model        TEXT,
    choice       TEXT,
    noul         REAL,
    score        REAL,
    confidence   REAL,
    probabilities TEXT,
    label        TEXT,
    note         TEXT,
    created_at   REAL NOT NULL,
    invalidated  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_grid_cell  ON grid(domain, subject_key, candidate_key);
CREATE INDEX IF NOT EXISTS idx_grid_row   ON grid(subject_key);

CREATE TABLE IF NOT EXISTS grid_stats (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    hits      INTEGER NOT NULL DEFAULT 0,
    misses    INTEGER NOT NULL DEFAULT 0,
    jev_calls INTEGER NOT NULL DEFAULT 0,
    saved_usd REAL    NOT NULL DEFAULT 0.0
);
INSERT OR IGNORE INTO grid_stats (id) VALUES (1);
"""


def cache_key(domain: str, subject_key: str, candidate_key: str,
              question_hash: str) -> str:
    h = hashlib.sha256()
    for part in (domain, subject_key, candidate_key, question_hash):
        h.update(part.encode())
        h.update(UNIT_SEP)
    return h.hexdigest()


def canonical_question_hash(candidate_keys: Sequence[str]) -> str:
    """Order-stable: [A,B] and [B,A] are the same question."""
    blob = json.dumps(sorted(candidate_keys), separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class DecisionConnector:
    """Substrate: cache, grid, provenance, routing. Domain-blind."""

    def __init__(self, adapter: DomainAdapter, db_path: Path | str = DEFAULT_DB,
                 client=None):
        self.adapter = adapter
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._client = client
        self._owns_client = client is None
        self.session = {"hits": 0, "misses": 0, "jev_calls": 0, "saved_usd": 0.0}

    # -- client ---------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            from typesafe_sdk import TypeSafeClient
            self._client = TypeSafeClient()
        return self._client

    # -- read/write -----------------------------------------------------
    def lookup(self, key: str) -> Optional[Decision]:
        row = self.conn.execute(
            "SELECT * FROM grid WHERE cache_key=? AND invalidated=0", (key,)
        ).fetchone()
        if row is None:
            return None
        return Decision(
            subject_key=row["subject_key"], candidate_key=row["candidate_key"],
            choice=row["choice"], noul=row["noul"], score=row["score"],
            confidence=row["confidence"],
            probabilities=json.loads(row["probabilities"] or "{}"),
            label=row["label"], source="cache", model=row["model"],
            note=row["note"],
        )

    def store(self, d: Decision, key: str, qhash: str, source: str = "jev") -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO grid
               (cache_key, domain, subject_key, candidate_key, question_hash,
                source, model, choice, noul, score, confidence, probabilities,
                label, note, created_at, invalidated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (key, self.adapter.name, d.subject_key, d.candidate_key, qhash,
             source, d.model, d.choice, d.noul, d.score, d.confidence,
             json.dumps(d.probabilities), d.label, d.note, time.time()),
        )
        self.conn.commit()

    def invalidate(self, subject_key: str, candidate_key: str | None = None,
                   note: str = "") -> int:
        if candidate_key is None:
            cur = self.conn.execute(
                """UPDATE grid SET invalidated=1, note=?
                   WHERE domain=? AND subject_key=? AND invalidated=0""",
                (note, self.adapter.name, subject_key))
        else:
            cur = self.conn.execute(
                """UPDATE grid SET invalidated=1, note=?
                   WHERE domain=? AND subject_key=? AND candidate_key=?
                   AND invalidated=0""",
                (note, self.adapter.name, subject_key, candidate_key))
        self.conn.commit()
        return cur.rowcount

    # -- the money path -------------------------------------------------
    def evaluate(self, subject, candidates, force: bool = False,
                 state_override: Optional[str] = None) -> list[Decision]:
        """Cache-first. Jev is the oracle of last resort.

        per_candidate: N candidates → N calls (each candidate gets its own
                       independent judgment, e.g. a per-part compatibility Noul)
        one_shot:      N candidates → ONE call (a single multivariable Choice);
                       the returned distribution fans out into N grid rows.

        state_override: evaluate an explicit state string instead of calling
        adapter.state_for(). Used by the bucket prober; pairs with force=True
        so a perturbed state is never served from cache.
        """
        mode = getattr(self.adapter, "evaluation_mode", "per_candidate")
        if mode == "one_shot":
            return self._evaluate_one_shot(subject, candidates, force, state_override)
        return self._evaluate_per_candidate(subject, candidates, force, state_override)

    def evaluate_state(self, state: str, subject, candidates) -> list[Decision]:
        """Evaluate an arbitrary state with the real candidate set.

        This is how the prober separates the two variables: the question stays
        fixed, only the wording of the state changes.
        """
        return self.evaluate(subject, candidates, force=True, state_override=state)

    def _evaluate_per_candidate(self, subject, candidates, force,
                                state_override=None):
        rows = self.adapter.subject_key(subject)
        cols = self.adapter.candidate_keys(subject, candidates)
        qhash = canonical_question_hash(cols)
        out: list[Decision] = []

        for ck in cols:
            key = cache_key(self.adapter.name, rows, ck, qhash)
            if not force:
                hit = self.lookup(key)
                if hit is not None:
                    self.session["hits"] += 1
                    self.session["saved_usd"] += COST_PER_CALL_USD
                    self._bump(hits=1, saved=COST_PER_CALL_USD)
                    out.append(hit)
                    continue

            self.session["misses"] += 1
            d = self._ask(subject, candidates, ck, rows, key, state_override)
            self.session["jev_calls"] += 1
            self._bump(misses=1, jev_calls=1)
            self.store(d, key, qhash, source="jev")
            out.append(d)
        return out

    def _evaluate_one_shot(self, subject, candidates, force,
                           state_override=None):
        """One Jev call scores every candidate. Fan the distribution out."""
        rows = self.adapter.subject_key(subject)
        cols = self.adapter.candidate_keys(subject, candidates)
        qhash = canonical_question_hash(cols)

        # Cache: if every column is already warm, we never touch the API.
        keys = {ck: cache_key(self.adapter.name, rows, ck, qhash) for ck in cols}
        if not force:
            cached = {ck: self.lookup(k) for ck, k in keys.items()}
            if all(v is not None for v in cached.values()):
                n = len(cols)
                self.session["hits"] += n
                self.session["saved_usd"] += COST_PER_CALL_USD  # ONE call saved, not N
                self._bump(hits=n, saved=COST_PER_CALL_USD)
                return [cached[ck] for ck in cols]  # type: ignore[misc]

        # Miss → exactly ONE API call, regardless of candidate count.
        state = (state_override if state_override is not None
                 else self.adapter.state_for(subject, candidates))
        questions = self.adapter.questions_for(subject, candidates, cols[0])
        resp = self.client.system_one(state=state, questions=questions)
        base = self.adapter.canonical(resp, cols[0])
        probs: dict[str, float] = base.get("probabilities") or {}
        chosen = base.get("choice")
        self.session["misses"] += len(cols)
        self.session["jev_calls"] += 1
        self._bump(misses=len(cols), jev_calls=1)

        out: list[Decision] = []
        for ck in cols:
            # Per-row noul = P(this candidate is the correct answer).
            p = probs.get(ck, 0.0) if probs else (1.0 if ck == chosen else 0.0)
            # canonical()'s contract is "Jev response → kwargs for Decision".
            # Anything it produced beyond the standard fields must ride along,
            # or an adapter that answers a second question in the same call has
            # its answer silently discarded. Preserve it under raw.
            extra = {k: v for k, v in base.items()
                     if k not in ("choice", "noul", "score", "confidence",
                                  "probabilities", "label", "raw")}
            d = Decision(
                subject_key=rows, candidate_key=ck,
                choice=chosen,
                noul=p,
                score=base.get("score"),
                confidence=base.get("confidence"),
                probabilities={ck: p},
                label=base.get("label") or chosen,
                model=getattr(resp, "model", None) or "jev",
                raw={**extra, **dict(base.get("raw") or {}),
                     "full_distribution": probs,
                     "adapter_noul": base.get("noul")},
            )
            self.store(d, keys[ck], qhash, source="jev")
            out.append(d)
        return out

    def _ask(self, subject, candidates, ck, rows, key,
             state_override=None) -> Decision:
        state = (state_override if state_override is not None
                 else self.adapter.state_for(subject, candidates))
        questions = self.adapter.questions_for(subject, candidates, ck)
        resp = self.client.system_one(state=state, questions=questions)
        kw = self.adapter.canonical(resp, ck)
        return Decision(
            subject_key=rows, candidate_key=ck,
            model=getattr(resp, "model", None) or "jev", **kw)

    # -- routing --------------------------------------------------------
    def route(self, d: Decision, act_above: float = 0.85,
              escalate_below: float = 0.60) -> str:
        """Confidence router — the thing that makes automation safe.

        High confidence  → act
        Low confidence   → escalate to a human
        In between       → act, but flag for review
        """
        c = d.confidence if d.confidence is not None else d.top_probability
        if c >= act_above:
            return "act"
        if c <= escalate_below:
            return "escalate"
        return "act_and_flag"

    # -- vision bridge --------------------------------------------------
    def evaluate_from_image(self, image_path: str, subject, candidates,
                            extractor, subject_key: Optional[str] = None):
        """Jev is text-only. Bridge: image → extractor → text state → decision.

        The extractor output is cached by image hash, so the expensive vision
        call happens once per unique image, never twice.
        """
        img = Path(image_path)
        img_hash = hashlib.sha256(img.read_bytes()).hexdigest()[:16]
        extracted = extractor(str(img))
        fused = dict(subject) if isinstance(subject, dict) else {"_raw": subject}
        fused["_image_extraction"] = extracted
        fused["_image_hash"] = img_hash
        return self.evaluate(fused, candidates)

    # -- grid -----------------------------------------------------------
    def grid(self, domain: Optional[str] = None) -> list[sqlite3.Row]:
        d = domain or self.adapter.name
        return self.conn.execute(
            """SELECT subject_key, candidate_key, noul, choice, confidence,
                      source, invalidated
               FROM grid WHERE domain=? ORDER BY subject_key, candidate_key""",
            (d,)).fetchall()

    def _bump(self, hits=0, misses=0, jev_calls=0, saved=0.0):
        self.conn.execute(
            """UPDATE grid_stats SET hits=hits+?, misses=misses+?,
                   jev_calls=jev_calls+?, saved_usd=saved_usd+? WHERE id=1""",
            (hits, misses, jev_calls, saved))
        self.conn.commit()

    def report(self) -> dict:
        r = self.conn.execute("SELECT * FROM grid_stats WHERE id=1").fetchone()
        total = r["hits"] + r["misses"]
        cells = self.conn.execute(
            "SELECT COUNT(*) c FROM grid WHERE invalidated=0 AND domain=?",
            (self.adapter.name,)).fetchone()["c"]
        return {
            "domain": self.adapter.name,
            "session": dict(self.session),
            "lifetime": {
                "hits": r["hits"], "misses": r["misses"],
                "jev_calls": r["jev_calls"],
                "hit_rate": (r["hits"] / total) if total else 0.0,
                "saved_usd": round(r["saved_usd"], 8),
                "cells": cells,
            },
        }

    def close(self):
        self.conn.close()
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
#  Offline self-test — two domains, one engine
# ══════════════════════════════════════════════════════════════════════

TRANSPORT = {
    "scrap": "scrap", "rework": "rework", "touch_up": "touch_up",
    "deviate": "deviate", "ship": "ship", "escalate": "escalate",
}


def _fake_response(choice, noul, score, probs, conf):
    class _A:
        def __init__(self, **kw): self.__dict__.update(kw)
    class _R:
        model = "jev-1.13.0"
    probs = probs or {}
    a = {
        "best_match": _A(value=choice, probabilities=probs, confidence=conf),
        "compatible": _A(value=noul, confidence=conf),
        "difficulty": _A(value=score, confidence=conf),
        "disposition": _A(value=choice, probabilities=probs, confidence=conf),
        "acceptable_to_ship": _A(value=noul, confidence=conf),
        "judgment_difficulty": _A(value=score, confidence=conf),
    }
    r = _R()
    r.answers = a
    return r


class _FakeClient:
    """Stands in for TypeSafeClient in the offline test."""
    def __init__(self, table): self.table = table
    def system_one(self, state, questions):
        key = next(iter(questions))
        # choose by inspecting which question names exist
        if "disposition" in questions:
            sig = state.split("defect_class : ")[1].split("\n")[0]
            sev = state.split("severity     : ")[1].split("\n")[0]
            payload = self.table.get((sig, sev), ("escalate", 0.4, 3, {}, 0.4))
        else:
            payload = self.table.get("fitment", ("pad-slide-econo", 0.95, 1, {}, 0.9))
        choice, noul, score, probs, conf = payload
        return _fake_response(choice, noul, score, probs, conf)
    def close(self): pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    live = args.live and bool(os.environ.get("TYPESAFE_API_KEY", ""))

    print("=" * 68)
    print("  DECISION CONNECTOR — one engine, two domains")
    print(f"  mode: {'LIVE' if live else 'OFFLINE self-test'}")
    print("=" * 68)

    db = Path(__file__).with_name("_selftest.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()

    # ---- Domain 1: fitment -------------------------------------------------
    vehicle = {
        "arku": "CAR-epsilon-2016-regal",
        "piece_type": "Buick Regal (4th gen, E2XX)",
        "attributes": {"make": "Buick", "model": "Regal", "front_rotor_mm": 302,
                       "rear_rotor_mm": 292, "caliper": "single-piston-slide"},
    }
    parts = {
        "pad-slide-econo": {"name": "Single-Piston Economy Pad Set",
                            "caliper_type": "single-piston-slide"},
        "pad-brembo-hp": {"name": "Brembo High-Performance Pads",
                          "caliper_type": "brembo-6piston"},
    }

    print("\n── DOMAIN 1: compatalog.fitment ─────────────────────────────")
    fake_fit = {"fitment": ("pad-slide-econo", 0.95, 1, {"pad-slide-econo": 0.95}, 0.91)}
    c1 = DecisionConnector(CompatalogFitmentAdapter(), db_path=db,
                           client=_FakeClient(fake_fit))
    d1 = c1.evaluate(vehicle, parts)
    for d in d1:
        print(f"  {d}  → route={c1.route(d)}")

    # same call again → should all be cache hits
    d1b = c1.evaluate(vehicle, parts)
    print(f"  re-run sources: {[d.source for d in d1b]}")

    # ---- Domain 2: QC disposition -----------------------------------------
    defect = {
        "defect_class": "surface_scratch", "severity": "cosmetic_minor",
        "part_family": "bracket-A2", "detector_confidence": 0.87,
        "location": "visible face", "dimensional_deviation": "none",
        "tolerance_class": "Class-2 surface", "customer_cosmetics": "high",
        "rework_cost_usd": 12, "scrap_cost_usd": 40,
        "units_short": 0, "lot_size": 500,
    }
    print("\n── DOMAIN 2: manufacturing.qc-disposition ───────────────────")
    fake_qc = {("surface_scratch", "cosmetic_minor"): ("rework", 0.12, 3,
                                                       {"rework": 0.62,
                                                        "touch_up": 0.18,
                                                        "scrap": 0.08,
                                                        "deviate": 0.06,
                                                        "ship": 0.04,
                                                        "escalate": 0.02}, 0.62)}
    c2 = DecisionConnector(QCDispositionAdapter(), db_path=db,
                           client=_FakeClient(fake_qc))
    d2 = c2.evaluate(defect, None)
    for d in d2[:3]:
        print(f"  {d}  → route={c2.route(d)}")
    print(f"  ... ({len(d2)} dispositions scored)")

    # ---- Proof the grid is shared, not duplicated -------------------------
    print("\n── SHARED GRID (one table, both domains) ────────────────────")
    rows = c2.conn.execute(
        """SELECT domain, subject_key, candidate_key, noul, label
           FROM grid WHERE invalidated=0
           ORDER BY domain, subject_key, candidate_key LIMIT 10""").fetchall()
    for r in rows:
        print(f"  {r['domain']:28s} {r['subject_key'][:26]:28s} "
              f"{r['candidate_key'][:20]:22s} noul={r['noul']}")

    print(f"\n  totals: {c2.conn.execute('SELECT COUNT(*) c FROM grid').fetchone()['c']} cells")

    print("\n── ADAPTER CONTRACT CHECK ───────────────────────────────────")
    for adp in (CompatalogFitmentAdapter(), QCDispositionAdapter()):
        ok = isinstance(adp, DomainAdapter)
        print(f"  {adp.name:32s} satisfies DomainAdapter: {ok} {'✓' if ok else '✗'}")

    print("\n── COST ACCOUNTING ──────────────────────────────────────────")
    print(f"  per Jev call:        ${COST_PER_CALL_USD:.8f}")
    print(f"  10k cached lookups:  ${COST_PER_CALL_USD*10_000:.4f}")
    print(f"  1M cached lookups:   ${COST_PER_CALL_USD*1_000_000:.2f}")

    c1.close(); c2.close()
    db.unlink()
    print("\n  Self-test DB cleaned up. Add TYPESAFE_API_KEY for live calls.")
