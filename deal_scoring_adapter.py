#!/usr/bin/env python3
# deal_scoring_adapter.py
#
# Domain: marketplace / liquidation deal scoring.
#   subject    — a listing (lot, pallet, or bulk deal)
#   candidates — grades the deal can earn
#
# Plug-in adapter for the decision-connector substrate: the ONLY file a new
# domain needs. Nothing in the substrate changes when a domain joins.
#
# Usage:
#   uv run python3 deal_scoring_adapter.py        # offline demo + contract check

from __future__ import annotations

from typing import Any, Mapping, Optional

from decision_connector import (
    Choice,
    DecisionConnector,
    Noul,
    Score,
)

# ──────────────────────────────────────────────────────────────────────────
#  The deal ledger — grades the line can earn
# ──────────────────────────────────────────────────────────────────────────

GRADES = {
    "buy":   "Expected value clears cost plus carrying; buy and move.",
    "hold":  "Not yet justified; watch price, comps, or condition.",
    "pass":  "Does not clear the floor; do not bid.",
    "scrap": "Negative value after fees; only recovery is parts or disposal.",
}


class DealScoringAdapter:
    """listing × grade → a defendable call on the lot."""

    name = "marketplace.deal-scoring"
    # ONE multivariable decision: Jev scores every grade in a single call and
    # returns the full distribution. N rows, 1 call.
    evaluation_mode = "one_shot"

    def subject_key(self, listing: dict) -> str:
        # The listing id is the stable grid row. If the same lot reappears
        # with a new id, canonicalize the signature instead so the cache
        # dedupes re-lists.
        return listing.get("lot_id") or listing.get("id") or "?"

    def candidate_keys(self, listing: dict, candidates=None) -> list[str]:
        return list(candidates or GRADES.keys())

    def state_for(self, listing: dict, candidates=None) -> str:
        lines = ["LIQUIDATION LOT — LISTED AS-IS",
                 f"  title           : {listing.get('title')}",
                 f"  category        : {listing.get('category')}",
                 f"  condition       : {listing.get('condition')}",
                 f"  units           : {listing.get('units')}",
                 f"  unit_cost_usd   : {listing.get('unit_cost_usd')}",
                 f"  lot_cost_usd    : {listing.get('lot_cost_usd')}",
                 f"  pallets         : {listing.get('pallets')}",
                 f"  source_fee_pct  : {listing.get('source_fee_pct')}",
                 f"  ship_usd        : {listing.get('ship_usd')}",
                 "",
                 "COMPS (same or adjacent category, last 90d)",
                 f"  median_sold_usd : {listing.get('median_sold_usd')}",
                 f"  sell_through_pct: {listing.get('sell_through_pct')}",
                 f"  condition_adj   : {listing.get('condition_adj')}",
                 "",
                 "AVAILABLE GRADES:"]
        for g in self.candidate_keys(listing, candidates):
            lines.append(f"  - [{g}] {GRADES.get(g, g)}")
        return "\n".join(lines)

    def questions_for(self, listing, candidates, candidate_key):
        return {
            "grade": Choice(
                instructions=("Which grade does this lot earn? Weigh unit cost "
                              "against median sold, sell-through, condition "
                              "adjustment, fees, freight, and how clean the "
                              "comps are."),
                criteria={g: GRADES[g] for g in self.candidate_keys(listing, candidates)},
            ),
            "clears_floor": Noul(
                instructions=("Expected value after all costs and carrying "
                              "clears your minimum return floor."),
            ),
            "comps_confidence": Score(
                instructions=("How trustworthy are the comps behind this call? "
                              "Low = sparse or mismatched. High = dense, recent, "
                              "same condition."),
                criteria=["anecdotal", "thin", "usable", "dense recent",
                          "same-sku verified"],
            ),
        }

    def canonical(self, response, candidate_key):
        a = response.answers
        return dict(
            choice=a["grade"].value,
            noul=a["clears_floor"].value,
            score=a["comps_confidence"].value,
            confidence=getattr(a["grade"], "confidence", None),
            probabilities=dict(a["grade"].probabilities or {}),
            label=a["grade"].value,
        )


# ──────────────────────────────────────────────────────────────────────────
#  Offline demo — a fake oracle, then the connector does its job
# ──────────────────────────────────────────────────────────────────────────

class _FakeOracle:
    """Answers every question the adapter can ask, offline, deterministically."""

    def __init__(self, grade: str, probs: dict[str, float],
                 confidence: float, clears_floor: float, comps: int):
        self._q = dict(grade=grade, probs=probs, confidence=confidence,
                       clears_floor=clears_floor, comps=comps)

    def system_one(self, state, questions):
        class A:
            def __init__(s, **kw): s.__dict__.update(kw)
        class R:
            model = "jev-standin"
            answers = {
                "grade": A(value=self._q["grade"],
                           probabilities=self._q["probs"],
                           confidence=self._q["confidence"]),
                "clears_floor": A(value=self._q["clears_floor"],
                                  confidence=self._q["confidence"]),
                "comps_confidence": A(value=self._q["comps"],
                                      confidence=self._q["confidence"]),
            }
        return R()

    def close(self): pass


def _demo_listing() -> dict:
    return {
        "lot_id": "MS-LOT-8412",
        "title": "Pallet of cordless power tools, mixed brand, open box",
        "category": "power-tools",
        "condition": "open-box",
        "units": 42,
        "unit_cost_usd": 11.50,
        "lot_cost_usd": 483.00,
        "pallets": 1,
        "source_fee_pct": 0.13,
        "ship_usd": 140.00,
        "median_sold_usd": 42.00,
        "sell_through_pct": 0.71,
        "condition_adj": 0.90,
    }


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    print("=" * 68)
    print("  DEAL SCORING — one listing, four grades, one call")
    print("=" * 68)

    db = Path(tempfile.gettempdir()) / "_deal_scoring_demo.db"
    if db.exists():
        db.unlink()

    oracle = _FakeOracle(
        grade="buy",
        probs={"buy": 0.58, "hold": 0.27, "pass": 0.12, "scrap": 0.03},
        confidence=0.71,
        clears_floor=0.9,
        comps=3,
    )
    c = DecisionConnector(DealScoringAdapter(), db_path=db, client=oracle)
    rows = c.evaluate(_demo_listing(), None)
    print(f"  grid rows written : {len(rows)}  (4 grades)")
    print(f"  JEV API CALLS     : {c.session['jev_calls']}  <-- one call, four rows")
    for r in rows:
        print(f"    {r}  → route={c.route(r)}")
    winner = max(rows, key=lambda r: r.noul or 0)
    print(f"  winner            : {winner.choice}  (noul={winner.noul})")
    c.close()
    db.unlink()
    print("\nALL GREEN — substrate untouched, domain carried by one file.")