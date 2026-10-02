#!/usr/bin/env python3
# bucket_delineation.py
#
# Answers: "how far down do we go to get deterministic I/O with clear bucket
# edges?"
#
# Method: perturb the state semantically and watch what moves.
#   output stable   → that region is a bucket; you're deep enough
#   output moves    → that's an undelineated boundary; quantize it
#   confidence drop → you're standing near an edge (distance-to-boundary sensor)
#
# Also runs FIELD ABLATION to find which fields the decision is actually
# sensitive to — those are the ones that must be quantized.
#
# Usage:
#   uv run python3 bucket_delineation.py

from __future__ import annotations

import copy
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))
from decision_connector import (  # noqa: E402
    Decision, DecisionConnector, FitmentAdapter, QCDispositionAdapter,
)

# ── Perturbation primitives ───────────────────────────────────────────

SYNONYMS = {
    "caliper": ["brake caliper", "calliper", "cal", "brake-caliper"],
    "rotor": ["disc", "brake disc", "brake rotor", "rotor disc"],
    "piston": ["pot", "cylinder"],
    "front": ["fwd", "front axle", "forward"],
    "pad": ["brake pad", "friction pad", "lining"],
    "vehicle": ["car", "automobile", "unit", "platform"],
    "defect": ["flaw", "nonconformity", "anomaly", "deviation"],
    "scratch": ["abrasion", "score mark", "gouge"],
    "rework": ["repair", "recondition", "corrective operation"],
    "scrap": ["reject", "discard", "write-off"],
}

# Numeric representation variants for 302 → several textual forms
NUM_STYLES = [
    ("raw", lambda v: f"{v}"),
    ("mm_suffix", lambda v: f"{v}mm"),
    ("spelled_unit", lambda v: f"{v} millimeters"),
    ("cm", lambda v: f"{v/10:.1f}cm"),
    ("metres", lambda v: f"{v/1000:.3f}m"),
    ("inches", lambda v: f"{v/25.4:.2f}in"),
    ("approx", lambda v: f"approximately {v}"),
    ("range", lambda v: f"{v-1} to {v+1}"),
    ("thousandths", lambda v: f"{v*1000} thousandths of a metre"),
]

CASE_STYLES = {
    "snake": lambda s: s,
    "title": lambda s: s.replace("_", " ").title(),
    "upper": lambda s: s.replace("_", " ").upper(),
    "camel": lambda s: re.sub(r"_(.)", lambda m: m.group(1).upper(), s),
    "kebab": lambda s: s.replace("_", "-"),
}

VALUE_STYLES = {
    "raw": lambda s: s,
    "spaced": lambda s: str(s).replace("-", " ").replace("_", " "),
    "slashed": lambda s: str(s).replace("-", "/"),
    "quoted": lambda s: f'"{s}"',
}


@dataclass
class Perturbation:
    name: str
    family: str
    apply: Callable[[str], str]

    def __call__(self, state: str) -> str:
        return self.apply(state)


def build_perturbations() -> list[Perturbation]:
    ps: list[Perturbation] = []

    # ---- family: synonym substitution ---------------------------------
    def syn(word, repl):
        def f(s: str) -> str:
            return re.sub(rf"\b{re.escape(word)}s?\b", repl, s, flags=re.IGNORECASE)
        return f

    for word, repls in SYNONYMS.items():
        for i, r in enumerate(repls[:2]):
            ps.append(Perturbation(f"synonym:{word}→{r}", "synonym", syn(word, r)))

    # ---- family: numeric representation -------------------------------
    for label, fn in NUM_STYLES:
        def numf(s: str, fn=fn) -> str:
            def sub(m):
                v = float(m.group(1))
                return fn(v) if v >= 100 else m.group(0)  # only touch mm-scale values
            return re.sub(r"\b(\d{3}(?:\.\d+)?)\b", sub, s)
        ps.append(Perturbation(f"numeric:{label}", "numeric", numf))

    # ---- family: key casing -------------------------------------------
    for label, fn in CASE_STYLES.items():
        def casef(s: str, fn=fn) -> str:
            return re.sub(r"^\s*([a-z_]+)\s*:",
                          lambda m: f"  {fn(m.group(1))}:", s, flags=re.MULTILINE)
        ps.append(Perturbation(f"keycase:{label}", "keycase", casef))

    # ---- family: value formatting -------------------------------------
    for label, fn in VALUE_STYLES.items():
        def valf(s: str, fn=fn) -> str:
            return re.sub(r"^\s*([a-z_]+)\s*:\s*([a-z][a-z0-9_-]+)\s*$",
                          lambda m: f"  {m.group(1)}: {fn(m.group(2))}",
                          s, flags=re.MULTILINE)
        ps.append(Perturbation(f"value:{label}", "value", valf))

    # ---- family: structural noise -------------------------------------
    ps.append(Perturbation("struct:blank_lines", "struct",
                           lambda s: s.replace("\n", "\n\n")))
    ps.append(Perturbation("struct:leading_ws", "struct",
                           lambda s: "\n".join("  " + ln for ln in s.split("\n"))))
    ps.append(Perturbation("struct:trailing_ws", "struct",
                           lambda s: "\n".join(ln + " " for ln in s.split("\n"))))
    ps.append(Perturbation("struct:reorder_lines", "struct", _reorder_lines))
    ps.append(Perturbation("struct:json_wrap", "struct", _json_wrap))
    ps.append(Perturbation("struct:prose_wrap", "struct", _prose_wrap))
    ps.append(Perturbation("struct:preamble", "struct",
                           lambda s: "Please analyze the following carefully.\n\n" + s))
    ps.append(Perturbation("struct:epilogue", "struct",
                           lambda s: s + "\n\nGive your best judgment."))

    return ps


def _reorder_lines(s: str) -> str:
    """Duplicate-safe line shuffle (bucket edges shouldn't care about order)."""
    lines = s.split("\n")
    body = [ln for ln in lines if ln.strip()]
    head, rest = body[:1], body[1:]
    rng = random.Random(1234)          # deterministic seed
    rng.shuffle(rest)
    return "\n".join(head + rest)


def _json_wrap(s: str) -> str:
    d = {}
    for ln in s.split("\n"):
        if ":" in ln:
            k, _, v = ln.partition(":")
            d[k.strip()] = v.strip()
    return json.dumps(d, indent=2)


def _prose_wrap(s: str) -> str:
    out = []
    for ln in s.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if ":" in ln:
            k, _, v = ln.partition(":")
            out.append(f"The {k.strip().replace('_',' ')} is {v.strip()}.")
        else:
            out.append(ln + ".")
    return " ".join(out)


# ── Canonicalizer: the instrument for "how far down" ──────────────────
#
# You go down until a canonicalizer maps every acceptable variant of the input
# into ONE bucket. Each rule you add removes a class of perturbation-induced
# flips. When flips hit zero, you have found the depth. Not before.

# Multi-word entries first — order matters when substituting.
CANON_SYNONYMS = [
    ("brake caliper", "caliper"), ("brake-caliper", "caliper"),
    ("calliper", "caliper"), ("cal ", "caliper "),
    ("brake disc", "rotor"), ("brake rotor", "rotor"),
    ("rotor disc", "rotor"), ("disc", "rotor"),
    ("brake pad", "pad"), ("friction pad", "pad"), ("lining", "pad"),
    ("pot", "piston"), ("cylinder", "piston"),
    ("automobile", "vehicle"), ("car", "vehicle"), ("unit", "vehicle"),
    ("fwd", "front"), ("front axle", "front"), ("forward", "front"),
    ("flaw", "defect"), ("nonconformity", "defect"), ("anomaly", "defect"),
    ("abrasion", "scratch"), ("score mark", "scratch"), ("gouge", "scratch"),
    ("repair", "rework"), ("recondition", "rework"),
    ("reject", "scrap"), ("discard", "scrap"), ("write-off", "scrap"),
]

# Unit → mm conversion factor.
UNIT_MM = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0,
    "millimetres": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0,
    "m": 1000.0, "meter": 1000.0, "meters": 1000.0, "metre": 1000.0,
    "metres": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4,
    "thousandths of a metre": 1.0, "thousandth of a metre": 1.0,
}

_NUM_RE = re.compile(
    r"(?:approximately\s+)?(\d+(?:\.\d+)?)\s*"
    r"(mm|millimeters?|millimetres?|cm|centimeters?|m|meters?|metres?|"
    r"inches?|in|thousandths? of a met(?:re|er))",
    re.IGNORECASE,
)


def _numeric_to_mm(m: re.Match) -> str:
    v = float(m.group(1))
    unit = m.group(2).lower()
    factor = UNIT_MM.get(unit, 1.0)
    mm = v * factor
    # Thousandths-of-a-metre is a trap: "302 thousandths of a metre" = 0.302 m
    if "thousandth" in unit:
        mm = v / 1000.0 * 1000.0
    return f"{round(mm):d}mm"


def canonicalize(state: str, band_mm: int = 0) -> str:
    """Fold every acceptable variant into one canonical form.

    band_mm > 0 collapses numeric values into bands, e.g. 302 and 305 both
    become '300-305mm'. That is what makes bucket EDGES explicit: two inputs
    in the same band are, by definition, the same bucket.
    """
    s = state.lower()

    # 1. drop rhetorical padding (preamble / epilogue lines)
    drop = ("please analyze", "give your best judgment", "carefully")
    s = "\n".join(ln for ln in s.split("\n")
                  if not any(d in ln.lower() for d in drop))

    # 1b. strip hedges even when no unit follows ("approximately 302")
    #     Hedged numbers are a real bucket-boundary bug: the value is the same,
    #     but the model may treat the hedge as reduced certainty.
    s = re.sub(r"\b(?:approximately|approx\.?|about|roughly|circa)\s+", "", s,
               flags=re.IGNORECASE)

    # 2. normalize numbers to integer millimetres
    s = _NUM_RE.sub(_numeric_to_mm, s)

    # 3. optional banding → explicit bucket edges
    if band_mm > 0:
        def band(m):
            v = int(m.group(1))
            lo = (v // band_mm) * band_mm
            return f"{lo}-{lo + band_mm}mm"
        s = re.sub(r"\b(\d{2,4})mm\b", band, s)

    # 4. collapse synonyms
    for a, b in CANON_SYNONYMS:
        s = s.replace(a, b)

    # 5. reconstruct prose back into key/value  ("the make is buick" → "make buick")
    s = re.sub(r"the ([a-z0-9 ]{1,40}?) (?:is|are) ([^.\n]+)\.?", r"\1 \2", s)

    # 6. normalize keys, separators and structural syntax
    s = re.sub(r"[_\-/]+", " ", s)
    s = re.sub(r'[:",]', " ", s)            # colons, quotes, commas
    s = re.sub(r"[{}]", " ", s)             # json braces
    s = re.sub(r"[ \t]+", " ", s)           # collapse runs of spaces
    s = re.sub(r"\n\s*\n+", "\n", s)        # collapse blank lines
    return "\n".join(ln.strip() for ln in s.split("\n") if ln.strip())


# ── Report types ──────────────────────────────────────────────────────

@dataclass
class Observation:
    perturbation: str
    family: str
    choice: Optional[str]
    noul: Optional[float]
    confidence: Optional[float]
    source: str

    @property
    def top(self) -> float:
        c = self.confidence if self.confidence is not None else self.noul
        return c if c is not None else 0.0


@dataclass
class StabilityReport:
    subject_key: str
    baseline: Observation
    observations: list[Observation] = field(default_factory=list)

    @property
    def choice_flips(self) -> list[Observation]:
        return [o for o in self.observations if o.choice != self.baseline.choice]

    @property
    def noul_moves(self) -> list[Observation]:
        b = self.baseline.noul or 0.0
        return [o for o in self.observations
                if abs((o.noul or 0.0) - b) > 0.05 and o not in self.choice_flips]

    @property
    def unstable_families(self) -> dict[str, int]:
        fams: dict[str, int] = {}
        for o in self.choice_flips:
            fams[o.family] = fams.get(o.family, 0) + 1
        return fams

    def verdict(self) -> str:
        if self.choice_flips:
            return "NOT A BUCKET — output moves under perturbation"
        if self.noul_moves:
            return "FUZZY EDGE — choice holds, probability drifts"
        return "BUCKET — stable across every perturbation"

    def render(self) -> str:
        L = []
        L.append(f"  subject: {self.subject_key}")
        L.append(f"  baseline: choice={self.baseline.choice} "
                 f"noul={_r(self.baseline.noul)} conf={_r(self.baseline.confidence)}")
        L.append(f"  perturbations: {len(self.observations)}  "
                 f"choice flips: {len(self.choice_flips)}  "
                 f"prob drifts: {len(self.noul_moves)}")
        L.append(f"  VERDICT: {self.verdict()}")
        if self.choice_flips:
            L.append("  ↳ flips by family:")
            for fam, n in sorted(self.unstable_families.items()):
                L.append(f"      {fam:10s} {n} perturbation(s)")
            L.append("  ↳ worst offenders:")
            for o in self.choice_flips[:5]:
                L.append(f"      {o.perturbation:34s} → {o.choice} "
                         f"(noul {_r(o.noul)}, conf {_r(o.confidence)})")
        return "\n".join(L)


def _r(x) -> str:
    return "None" if x is None else f"{x:.3f}"


# ── Prober ────────────────────────────────────────────────────────────

class StabilityProber:
    """Drives perturbations through the connector and reads the result.

    Critical: the QUESTION stays fixed (same candidates) — only the wording of
    the STATE changes. If the candidate set changed too, every comparison would
    be meaningless.
    """

    def __init__(self, connector: DecisionConnector,
                 perturbations: Optional[list[Perturbation]] = None,
                 canonicalizer: Optional[Callable[[str], str]] = None):
        self.c = connector
        self.ps = perturbations or build_perturbations()
        self.canon = canonicalizer        # applied to BOTH baseline and variants

    def probe(self, subject, candidates) -> StabilityReport:
        raw = self.c.adapter.state_for(subject, candidates)
        base_state = self.canon(raw) if self.canon else raw
        skey = self.c.adapter.subject_key(subject)

        # Baseline is measured on the canonicalized state, so the comparison is
        # apples-to-apples: "canonicalized baseline" vs "canonicalized variant".
        ds = self.c.evaluate_state(base_state, subject, candidates)
        d0 = ds[0] if ds else Decision(subject_key=skey, candidate_key="?")
        baseline = Observation("baseline", "baseline", d0.choice, d0.noul,
                               d0.confidence, d0.source)
        report = StabilityReport(subject_key=skey, baseline=baseline)

        for p in self.ps:
            perturbed = p(base_state)
            if self.canon:
                perturbed = self.canon(perturbed)
            if perturbed == base_state:
                # The canonicalizer absorbed this perturbation entirely —
                # that is a WIN, not a skip. Count it as stable.
                report.observations.append(Observation(
                    p.name, p.family, baseline.choice, baseline.noul,
                    baseline.confidence, "absorbed"))
                continue
            obs = self._eval_state(perturbed, subject, candidates, p.name, p.family)
            report.observations.append(obs)
        return report

    def _eval_state(self, state: str, subject, candidates,
                    label: str, family: str) -> Observation:
        """Evaluate an altered state against the SAME candidate set."""
        ds = self.c.evaluate_state(state, subject, candidates)
        d = ds[0] if ds else Decision(subject_key="?", candidate_key="?")
        return Observation(perturbation=label, family=family, choice=d.choice,
                           noul=d.noul, confidence=d.confidence, source=d.source)

    # ---- field ablation: which fields does the answer actually rest on? ----

    def ablate_fields(self, subject: dict, candidates) -> dict[str, str]:
        """Drop one field at a time. If the choice moves, that field is
        decision-critical and MUST be quantized for a crisp bucket.

        Handles both nested ({"attributes": {...}}) and flat subjects.
        """
        results: dict[str, str] = {}
        base = self.c.evaluate(subject, candidates, force=True)
        base_choice = base[0].choice if base else None

        nested = "attributes" in subject
        container = subject["attributes"] if nested else subject
        skey = self.c.adapter.subject_key(subject)
        # Never ablate the grid identity fields.
        skip = {k for k in ("arku", "id", "piece_type")
                if k in container} | {skey}

        for a in list(container.keys()):
            if a in skip:
                continue
            s2 = copy.deepcopy(subject)
            (s2["attributes"] if nested else s2).pop(a, None)
            ds = self.c.evaluate(s2, candidates, force=True)
            choice = ds[0].choice if ds else None
            results[a] = ("CRITICAL" if choice != base_choice else "inert")
        return results


# ── Offline demo ──────────────────────────────────────────────────────

# The oracle is deliberately sensitive to *unquantized* forms. Because the same
# oracle is used for both cases, the only variable between Case A and Case B is
# the canonicalizer — which is exactly the thing we're trying to measure.
SENSITIVE_TO = {
    # unit / numeric representation
    "millimeters": "WRONG_UNIT",
    "inches": "WRONG_UNIT",
    "approximately": "HEDGED",
    "thousandths": "WRONG_UNIT",
    # serialization / lexeme variants
    "disc": "WRONG_LEXEME",
    "brake pad": "WRONG_LEXEME",
    "calliper": "WRONG_LEXEME",
    "please analyze": "PREAMBLE",
    "give your best judgment": "EPILOGUE",
    "{": "STRUCTURAL",
    # fires only when a field was ablated to None — makes ablation measurable
    "deviation: Nonemm": "MISSING_DIM",
}


def _fake_client():
    class A:
        def __init__(self, **kw): self.__dict__.update(kw)

    class R:
        model = "jev-1.13.0"

    class Client:
        def __init__(self): self.calls = 0

        def system_one(self, state, questions):
            self.calls += 1
            forced = next((v for k, v in SENSITIVE_TO.items() if k in state), None)
            opts = list(questions["disposition"].criteria.keys())
            pick = "WRONG" if forced else "rework"
            dist = {o: 0.02 for o in opts}
            dist[pick] = 0.71
            conf = 0.66 if forced else 0.88
            r = R()
            r.answers = {
                "disposition": A(value=pick, probabilities=dist, confidence=conf),
                "acceptable_to_ship": A(value=0.1, confidence=conf),
                "judgment_difficulty": A(value=3, confidence=conf),
            }
            return r

        def close(self): pass
    return Client()


if __name__ == "__main__":
    print("=" * 74)
    print("  BUCKET DELINEATION — how far down do we have to go?")
    print("=" * 74)

    db = Path(__file__).with_name("_bucket.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()

    defect = {
        "defect_class": "surface_scratch", "severity": "cosmetic_minor",
        "part_family": "bracket-A2", "detector_confidence": 0.87,
        "location": "visible face", "tolerance_class": "Class 2 surface",
        "customer_cosmetics": "high", "rework_cost_usd": 12,
        "scrap_cost_usd": 40, "units_short": 0, "lot_size": 500,
        "dimensional_deviation_mm": 302,
    }

    adapter = QCDispositionAdapter()

    # ── CASE A: raw state, no canonicalization ────────────────────────
    print("\n" + "─" * 74)
    print("  CASE A — RAW state (depth 0: freeform, nothing normalized)")
    print("─" * 74)
    cA = DecisionConnector(adapter, db_path=db, client=_fake_client())
    repA = StabilityProber(cA).probe(defect, None)
    print(repA.render())

    # ── CASE B: same oracle, state canonicalized first ────────────────
    print("\n" + "─" * 74)
    print("  CASE B — CANONICALIZED state (depth 3: typed + unit-normalized)")
    print("─" * 74)
    cB = DecisionConnector(adapter, db_path=db, client=_fake_client())
    repB = StabilityProber(cB, canonicalizer=canonicalize).probe(defect, None)
    print(repB.render())

    if repB.choice_flips:
        print("\n  ↳ residual flips — these input classes CANNOT be absorbed.")
        print("    They must be BLOCKED upstream, not normalized:")
        for o in repB.choice_flips[:6]:
            print(f"      {o.perturbation:36s} → {o.choice}")

    # ── CASE C: banded canonicalization — explicit bucket edges ───────
    print("\n" + "─" * 74)
    print("  CASE C — BANDED (band_mm=5) — bucket edges made explicit")
    print("─" * 74)
    banded = lambda s: canonicalize(s, band_mm=5)      # noqa: E731
    for mm in (300, 302, 304, 305, 309):
        d = {"defect_class": "x", "severity": "y", "part_family": "z",
             "dimensional_deviation_mm": mm}
        st = canonicalize(adapter.state_for(d, None), band_mm=5)
        line = next((ln for ln in st.split("\n") if "deviation" in ln), "?")
        print(f"    dimensional_deviation_mm={mm:>3d}  →  {line.strip()}")

    # ── Ladder summary ────────────────────────────────────────────────
    print("\n" + "─" * 74)
    print("  THE LADDER — same oracle, same perturbation set, one variable")
    print("─" * 74)
    n = len(repA.observations)
    fa, fb = len(repA.choice_flips), len(repB.choice_flips)
    print(f"    depth 0 (raw)            : {fa}/{n} perturbations flip the answer")
    print(f"    depth 3 (canonicalized)  : {fb}/{n} perturbations flip the answer")
    print(f"    depth 3+ (banded)        : numeric variants collapse to one cell")
    print(f"\n    → determinism bought: {fa - fb} flip classes eliminated by")
    print(f"      {len(CANON_SYNONYMS)} synonym rules + unit normalization")
    print(f"      + serialization rules.")

    # ── Field ablation ────────────────────────────────────────────────
    print("\n" + "─" * 74)
    print("  FIELD ABLATION — which fields must be quantized?")
    print("─" * 74)
    cC = DecisionConnector(adapter, db_path=db, client=_fake_client())
    abl = StabilityProber(cC).ablate_fields(defect, None)
    for f_name, verdict in abl.items():
        mark = "⚠ CRITICAL — must be a closed set or band" if verdict == "CRITICAL" else "inert"
        print(f"    {f_name:24s} {verdict:10s} {mark}")

    print("\n" + "─" * 74)
    print("  STOPPING RULE")
    print("─" * 74)
    print("""
    choice flips > 0            → not a bucket. Add a canonicalizer rule OR
                                  block that input class upstream. Re-probe.
    probability drift only      → acceptable IF the drift stays inside your
                                  routing band (act / act_and_flag / escalate).
    zero flips, zero drift      → you are deep enough. STOP.

    Confidence is the early-warning sensor: a perturbation that drops
    confidence without flipping the choice means you are standing ON an edge,
    even though the answer hasn't moved yet.

    Everything here is measurable offline. Swap in a live client and the same
    harness measures Jev's real sensitivity, not the oracle's.
""")

    for cc in (cA, cB, cC):
        cc.close()
    db.unlink()
    print("  Done.")
