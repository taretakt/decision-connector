#!/usr/bin/env python3
# catalog_watchdog_adapter.py
#
# Domain: catalog / directory delta watching.
#   subject    — a page pair (before/after snapshot of a monitored catalog)
#   candidates — verdicts the watchdog can hand back to the digest loop
#
# The mechanical per-item delta (ADDED / MODIFIED / REMOVED per row) is hash
# diffing and belongs in code. THIS adapter carries the judgment layer: given
# the measured delta, is the change worth waking the agent for, and is the
# scraper itself still healthy?
#
# Usage:
#   uv run python3 catalog_watchdog_adapter.py     # offline demo + contract check

from __future__ import annotations

from typing import Any, Mapping, Optional

from decision_connector import (
    Choice,
    DecisionConnector,
    Noul,
    Score,
)

# ──────────────────────────────────────────────────────────────────────────
#  Verdicts the watchdog can hand to the digest loop
# ──────────────────────────────────────────────────────────────────────────

VERDICTS = {
    "no_digest": "Nothing material moved; update last_seen only.",
    "digest":    "Material additions or modifications; run the agent digest.",
    "escalate":  "Structure broke or counts imploded; the scraper may be broken.",
}


class CatalogWatchdogAdapter:
    """page pair × verdict → what the digest loop should do."""

    name = "catalog.watchdog-verdict"
    # ONE multivariable decision over the verdicts; the distribution fans out.
    evaluation_mode = "one_shot"

    def subject_key(self, pair: dict) -> str:
        # A stable page identity: source + path, NOT the content hash (the
        # content is exactly what changes between snapshots).
        return f"{pair.get('source', '?')}|{pair.get('path', '?')}"

    def candidate_keys(self, pair: dict, candidates=None) -> list[str]:
        return list(candidates or VERDICTS.keys())

    def state_for(self, pair: dict, candidates=None) -> str:
        m = pair.get("measurements") or {}
        lines = ["CATALOG PAGE PAIR — MEASURED DELTA",
                 f"  source          : {pair.get('source')}",
                 f"  path            : {pair.get('path')}",
                 f"  window          : {pair.get('window')}",
                 "",
                 "MEASUREMENTS (computed mechanically before this call)",
                 f"  items_before    : {m.get('items_before')}",
                 f"  items_after     : {m.get('items_after')}",
                 f"  added           : {m.get('added')}",
                 f"  removed         : {m.get('removed')}",
                 f"  modified        : {m.get('modified')}",
                 f"  drift_fields    : {m.get('drift_fields')}",
                 f"  price_churn_pct : {m.get('price_churn_pct')}",
                 "",
                 "STRUCTURE (layout/tag fingerprint of the page)",
                 f"  structural_hash : {m.get('structural_hash')}",
                 f"  structure_broke : {m.get('structure_broke')}",
                 "",
                 "AVAILABLE VERDICTS:"]
        for v in self.candidate_keys(pair, candidates):
            lines.append(f"  - [{v}] {VERDICTS.get(v, v)}")
        return "\n".join(lines)

    def questions_for(self, pair, candidates, candidate_key):
        return {
            "verdict": Choice(
                instructions=("Which verdict fits this page pair? Weigh how "
                              "much moved, whether the move is material to "
                              "subscribers, and above all whether the page "
                              "structure itself survived. A structural break "
                              "with zero items is a scraper failure, not a "
                              "quiet catalog."),
                criteria={v: VERDICTS[v] for v in self.candidate_keys(pair, candidates)},
            ),
            "scraper_intact": Noul(
                instructions=("The scraper parsed this snapshot correctly; the "
                              "page structure is intact and the delta is real."),
            ),
            "inspection_depth": Score(
                instructions=("How much human or agent inspection does this "
                              "delta deserve? Low = skip. High = full review."),
                criteria=["skip", "digest skim", "agent digest",
                          "deep review", "operator call"],
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["verdict"].value,
            noul=a["scraper_intact"].value,
            score=a["inspection_depth"].value,
            confidence=getattr(a["verdict"], "confidence", None),
            probabilities=dict(a["verdict"].probabilities or {}),
            label=a["verdict"].value,
        )


# ──────────────────────────────────────────────────────────────────────────
#  Offline demo
# ──────────────────────────────────────────────────────────────────────────

class _FakeOracle:
    """Answers every question the adapter can ask, offline, deterministically."""

    def __init__(self, verdict: str, probs: dict[str, float],
                 confidence: float, intact: float, depth: int):
        self._q = dict(verdict=verdict, probs=probs, confidence=confidence,
                       intact=intact, depth=depth)

    def system_one(self, state, questions):
        class A:
            def __init__(s, **kw): s.__dict__.update(kw)
        class R:
            model = "jev-standin"
            answers = {
                "verdict": A(value=self._q["verdict"],
                             probabilities=self._q["probs"],
                             confidence=self._q["confidence"]),
                "scraper_intact": A(value=self._q["intact"],
                                    confidence=self._q["confidence"]),
                "inspection_depth": A(value=self._q["depth"],
                                      confidence=self._q["confidence"]),
            }
        return R()

    def close(self): pass


def _demo_pair(modified: int = 14, added: int = 6, removed: int = 2) -> dict:
    return {
        "source": "megasavers",
        "path": "/clearance/power-tools",
        "window": "2026-10-01T03:00Z",
        "measurements": {
            "items_before": 880,
            "items_after": 884,
            "added": added,
            "removed": removed,
            "modified": modified,
            "drift_fields": ["price_usd"],
            "price_churn_pct": 0.08,
            "structural_hash": "3f9a21c7",
            "structure_broke": False,
        },
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    print("=" * 68)
    print("  CATALOG WATCHDOG — one page pair, three verdicts, one call")
    print("=" * 68)

    db = Path(tempfile.gettempdir()) / "_watchdog_demo.db"
    if db.exists():
        db.unlink()

    oracle = _FakeOracle(
        verdict="digest",
        probs={"no_digest": 0.10, "digest": 0.72, "escalate": 0.18},
        confidence=0.74,
        intact=0.95,
        depth=2,
    )
    c = DecisionConnector(CatalogWatchdogAdapter(), db_path=db, client=oracle)
    rows = c.evaluate(_demo_pair(), None)
    print(f"  grid rows written : {len(rows)}  (3 verdicts)")
    print(f"  JEV API CALLS     : {c.session['jev_calls']}  <-- one call, three rows")
    for r in rows:
        print(f"    {r}  → route={c.route(r)}")
    winner = max(rows, key=lambda r: r.noul or 0)
    print(f"  winner            : {winner.choice}  (noul={winner.noul})")
    c.close()
    db.unlink()

    # A broken structure with zero items must escalate, not read as 'quiet'.
    print("\n── edge: structure broke, zero items ──")
    broke = _demo_pair(modified=0, added=0)
    broke["measurements"]["structure_broke"] = True
    c2 = DecisionConnector(CatalogWatchdogAdapter(), db_path=db,
                           client=_FakeOracle(
                               verdict="escalate",
                               probs={"no_digest": 0.02, "digest": 0.08,
                                      "escalate": 0.90},
                               confidence=0.88, intact=0.05, depth=4))
    rows2 = c2.evaluate(broke, None)
    w2 = max(rows2, key=lambda r: r.noul or 0)
    print(f"  winner            : {w2.choice}  (noul={w2.noul})")
    c2.close()
    db.unlink()
    print("\nALL GREEN — substrate untouched, domain carried by one file.")