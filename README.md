# Decision Connector

A domain-agnostic substrate between **structured systems** and **Jev's typed decision primitives** (Choice / Noul / Score).

The connector owns the parts of decision automation that should be identical no matter what you're deciding:

- **Deterministic cache keys** — byte-stable keys built with unit separators, so the same state hits the same judgment every time
- **SQLite solution grid** — every judgment, every candidate, every score lands in one queryable table with provenance
- **Invalidation** — forget only what changed, never nuke the grid
- **Confidence routing** — low-confidence judgments route to a human, high-confidence ones flow straight through
- **Cost accounting** — every call is priced (`$42 / billion input tokens`, ~400 tokens per call)
- **Non-text input bridge** — structured state in, structured decisions out, no prose required

The judgment itself is delegated to **Jev**, a hosted decision engine exposing the typed primitive contract. This repo runs fully offline with stand-in primitives — you can develop, test, and red-team your adapters without an engine — and switches to live mode when a `typesafe_sdk` and `TYPESAFE_API_KEY` are present.

## Why

LLM-backed decisions drift; structured automation doesn't. The connector's job is to make the *substrate* boring and correct — cache, grid, routing — so the only moving part left is the judgment itself, and that judgment is measurable: byte-deterministic output, explicit confidence, auditable rows.

## Adapter contract

An adapter turns a domain into questions and answers:

| Side | What it does |
|---|---|
| `questions_for(state, candidates)` | Builds typed Choice / Noul / Score primitives for Jev |
| `canonical(response)` | Jev response → canonical `Decision` (value, probabilities, confidence, difficulty) |

Two evaluation modes:

- **ONE_SHOT** — one Jev call, grid rows for every candidate (six rows, one call)
- **PER_CANDIDATE** — one judgment per candidate, each row cached (warm re-runs cost zero calls)

## Domains

Four domains shipped — two reference adapters inside the substrate's self-test, two plug-in files that prove the pattern:

| Domain | subject | candidates | ships as |
|---|---|---|---|
| Compalog fitment | vehicle | parts | reference adapter |
| QC disposition | defect signature | dispositions | reference adapter |
| [Deal scoring](deal_scoring_adapter.py) | listing | grades (buy / hold / pass / scrap) | adapter file |
| [Catalog watchdog](catalog_watchdog_adapter.py) | page pair | verdicts (no_digest / digest / escalate) | adapter file |

`bucket_delineation.py` is the companion instrument: it perturbs state semantically to find where decisions flip and which input fields the decision is actually sensitive to — those are the fields that must be quantized.

### Adding a domain

One file, six members, zero substrate changes:

```python
class MyAdapter:
    name = "domain.decision-name"
    evaluation_mode = "one_shot" | "per_candidate"   # the cost lever
    def subject_key(self, subject) -> str                       # grid row
    def candidate_keys(self, subject, candidates) -> list[str]  # grid columns
    def state_for(self, subject, candidates) -> str             # → Jev state
    def questions_for(self, subject, candidates, candidate_key) -> dict
    def canonical(self, response, candidate_key) -> dict         # → Decision
```

## Run

```bash
uv run python3 decision_connector.py     # offline self-test, both domains
uv run python3 bucket_delineation.py     # stability probe, raw vs canonicalized
uv run python3 test_call_efficiency.py   # call-count assertions
uv run python3 deal_scoring_adapter.py   # plug-in domain demo
uv run python3 catalog_watchdog_adapter.py
uv run pytest                            # test suite
```

Live mode (requires the Jev SDK in your environment):

```bash
TYPESAFE_API_KEY=sk-... uv run python3 decision_connector.py --live
```

The SQLite grid path is configurable via `DECISION_GRID_DB` (default: `./grid.db`).

## Files

| File | Role |
|---|---|
| `decision_connector.py` | Connector substrate: primitives, grid, cache, routing, self-test |
| `bucket_delineation.py` | Stability prober + field ablation (quantization targeting) |
| `deal_scoring_adapter.py` | Plug-in domain: listing × grades |
| `catalog_watchdog_adapter.py` | Plug-in domain: page pair × verdicts |
| `test_call_efficiency.py` | Call-count and cache-hit assertions |
| `tests/` | Pytest suite |

## License

MIT — see [LICENSE](LICENSE).