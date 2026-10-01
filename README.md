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

## Reference adapters

Two shipped as worked examples — the pattern transfers, the adapter is the only custom part:

- `CompatalogFitmentAdapter` — automotive part fitment (does this part go on this vehicle?)
- `QCDispositionAdapter` — manufacturing QC disposition (rework / scrap / ship? Acceptable to ship?)

`bucket_delineation.py` is the companion instrument: it perturbs state semantically to find where decisions flip and which input fields the decision is actually sensitive to — those are the fields that must be quantized.

## Run

```bash
uv run python3 decision_connector.py     # offline self-test, both domains
uv run python3 bucket_delineation.py     # stability probe, raw vs canonicalized
uv run python3 test_call_efficiency.py   # call-count assertions
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
| `test_call_efficiency.py` | Call-count and cache-hit assertions |
| `tests/` | Pytest suite |

## License

MIT — see [LICENSE](LICENSE).