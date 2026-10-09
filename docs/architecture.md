# Architecture

The connector is small on purpose. It owns the parts of decision automation that should be
identical no matter what you are deciding, and it stays ignorant of the domain itself. Three
things are deliberately separated:

| Layer | Lives in | Knows about |
|---|---|---|
| **Substrate** | `decision_connector.py` | keys, cache, grid, routing, cost. Domain-blind. |
| **Judgment** | Jev (hosted) — or a stand-in oracle offline | the actual question |
| **Domain** | one adapter file | parts, listings, resources… |

Swap the domain, the substrate does not change. That is the whole thesis: make the substrate
boring and correct so the only moving part left is the judgment — and make the judgment
measurable.

---

## The layers

### 1. Primitives in, decisions out

An adapter turns a domain into typed `Choice` / `Noul` / `Score` primitives via
`questions_for(state, candidates, candidate_key)`, and collapses whatever comes back into one
domain-neutral record:

```python
@dataclass
class Decision:
    subject_key: str
    candidate_key: str
    choice: Optional[str] = None
    noul: Optional[float] = None       # binary read: .positive == noul > 0.5
    score: Optional[float] = None
    confidence: Optional[float] = None
    probabilities: dict[str, float] = field(default_factory=dict)
    label: Optional[str] = None        # human-readable outcome
    source: str = "jev"                # 'jev' | 'cache' | 'manual' | 'import'
    model: Optional[str] = None
    note: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
```

Every domain, every mode, every stage produces this. Nothing downstream needs to know what was
being decided.

### 2. Deterministic cache keys

```python
def cache_key(domain: str, subject_key: str, candidate_key: str, question_hash: str) -> str:
    h = hashlib.sha256()
    for part in (domain, subject_key, candidate_key, question_hash):
        h.update(part.encode())
        h.update(UNIT_SEP)          # \x1f — a separator that cannot occur in the parts
    return h.hexdigest()
```

Two properties matter:

- **Unit separators between parts.** Naive concatenation collides: `("ab", "c")` and `("a", "bc")`
  hash the same. A byte that cannot appear inside the parts removes the whole class of bug.
- **The state is a question, not a string blob.** Keys are built from the *canonical question*,
  not the raw prose:

```python
def canonical_question_hash(candidate_keys: Sequence[str]) -> str:
    """Order-stable: [A, B] and [B, A] are the same question."""
    blob = json.dumps(sorted(candidate_keys), separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
```

Because the candidate set is part of the key, **a changing candidate list changes the question**
and orphans the old rows instead of silently reusing an answer to a question nobody asked. That
is the failure mode this design exists to prevent.

### 3. The solution grid

Every judgment — every subject, every candidate, every score — lands in one SQLite table:

```
grid(cache_key PK, domain, subject_key, candidate_key, question_hash, source, model,
     choice, noul, score, confidence, probabilities, label, note, created_at, invalidated)
  idx: subject_key
grid_stats(id=1, hits, misses, jev_calls, saved_usd)     -- singleton counters
```

- `source` records provenance (`jev` vs `cache` vs `manual` vs `import`) — an auditable row
  says where its answer came from, years later.
- **Invalidation is soft**: `invalidate(subject_key, candidate_key=None, note="")` flips
  `invalidated=1` and stores the *reason*. Nothing is deleted; a re-judgment writes a new row.
  You forget only what changed, and you keep the record of why.

### 4. Evaluation modes — the cost lever

| Mode | Calls | Grid rows | Use when |
|---|---|---|---|
| `per_candidate` | 1 per candidate | 1 per candidate | candidates are independent; re-runs warm to zero calls |
| `one_shot` | 1 per subject | 1 per candidate | candidates compete; the distribution is the answer |

`one_shot` carries the full distribution in `raw` (`full_distribution`, `adapter_noul`) because
the per-row `Decision.noul` is overwritten with the per-row probability. Downstream stages that
need stage semantics read them from `raw` and coerce at a threshold — `bool(0.3)` is silently
`True`, so comparisons are explicit (`x > 0.5`).

### 5. Confidence routing — what makes unattended automation safe

```python
def route(self, d, act_above: float = 0.85, escalate_below: float = 0.60) -> str:
    c = d.confidence if d.confidence is not None else d.top_probability
    if c >= act_above:     return "act"
    if c <= escalate_below: return "escalate"
    return "act_and_flag"
```

Three outcomes, not two. High confidence flows straight through; low confidence goes to a human;
the middle band acts **but is flagged for review** — the zone where silent automation quietly
does damage if you pretend it is certainty.

`is_terminal(decisions)` is the loop contract: a batch is terminal only if *every* row routes to
`act`. Any escalate or flag means a human should at least see it before the loop moves on — that
is how a loop decides it can run unattended.

`flush()` commits and closes, so scripts and CI exit without leaking a connection.

### 6. Vision bridge

Jev is text-only. `evaluate_from_image(image_path, subject, candidates, extractor)` runs the
extractor, then feeds the text state through the normal path. The extractor output is cached by
**image hash**, so the expensive vision call happens once per unique image, never twice.

---

## The adapter contract — seven members, zero substrate changes

```python
class MyAdapter:
    name = "domain.decision-name"                                # 1
    evaluation_mode = "one_shot" | "per_candidate"               # 2
    def subject_key(self, subject) -> str: ...                   # 3  grid row
    def candidate_keys(self, subject, candidates) -> list[str]: ...  # 4  grid columns
    def state_for(self, subject, candidates) -> str | Mapping: ...   # 5  → Jev state
    def questions_for(self, subject, candidates, candidate_key) -> dict: ...  # 6
    def canonical(self, response, candidate_key) -> dict: ...    # 7  → Decision
```

A domain is one file. It ships its own offline oracle, its own `__main__` demo, and its own
tests. Full contract in [CONTRIBUTING.md](../CONTRIBUTING.md).

Multi-stage domains compose: each stage is its own adapter, and each stage writes its winner into
a *staged copy* of the subject (`staged["theme"]`, `staged["depth"]`) so the next stage's
`state_for` sees it. Keep each stage's candidate list a fixed enum — a changing set changes the
question hash and orphans cached rows.

---

## Offline vs live — the differential contract

The repo runs **fully offline** on stand-in primitives: scripted oracles that map a stage key to a
canned `(choice, dist, conf, noul, score)` tuple. You can develop, test, red-team and demo
adapters without an engine, and CI never touches the network.

Live mode is the same harness with a real client — a `typesafe_sdk` and `TYPESAFE_API_KEY` in the
environment. Because both paths run identical substrate code, the *same* test suite can be pointed
at each: offline-online differential runs catch nondeterminism before release.

---

## `bucket_delineation.py` — the stability instrument

Before you cache a judgment, you need to know which parts of the state it cannot tolerate
changing. This module answers that by violence:

- **Perturbation families** — synonym swaps, line reordering, JSON wrapping, prose wrapping,
  numeric normalization (`canonicalize(state, band_mm)`).
- **`probe(subject, candidates)` → `StabilityReport`** — `choice_flips`, `noul_moves`,
  `unstable_families`, `verdict`. Semantically identical states should land on the same decision;
  where they do not, you have found a key you must normalize before hashing.
- **`ablate_fields(subject, candidates)`** — drops one input field at a time and reports which
  fields the decision is actually sensitive to. Those are the fields that must be quantized.

This is the half of the system that keeps the cache *honest*: determinism is easy to claim and
hard to verify, so it is verified by perturbation instead of by assertion.

---

## Cost model

Every call is priced (`$42 / billion input tokens`, ~400 tokens per call ≈ $0.0000168). Cache
hits cost zero. Counters live both in the process session dict (`hits`, `misses`, `jev_calls`,
`saved_usd`) and in the `grid_stats` row, so a run can report what it avoided spending.
`test_call_efficiency.py` asserts call counts and cache-hit ratios — the cost claim is tested, not
asserted.

---

## Boundaries (non-goals)

- **Not a model and not a prompt library.** The judgment is delegated; the connector never
  pretends to reason.
- **Not domain-aware.** It does not know what a part, a listing, or a resource is, and it should
  never learn.
- **Not an orchestrator.** It gives a loop the facts it needs (`is_terminal`) and stops.
- **No network in the offline path.** CI proves it.

---

## Testing

55 tests, offline-only, no network, no engine. CI runs the suite plus **every module's offline
mode** — each script doubles as a demo and a smoke test, so the documented examples cannot rot.
