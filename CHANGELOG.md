# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-10-09

First public release. The substrate, two reference adapters, three plug-in domains, a batch
funnel with a scrobbler hopper, a stability instrument, and a 55-test offline suite.

### Added — substrate (`decision_connector.py`)

- Typed decision primitives (`Choice` / `Noul` / `Score`) collapsed into one domain-neutral
  `Decision` record with provenance (`source`), probabilities, confidence and label.
- Deterministic cache keys: sha256 over `domain · subject · candidate · question_hash` with
  unit separators between parts (`\x1f`), so part boundaries cannot be forged.
- Order-stable canonical question hash — `[A, B]` and `[B, A]` are the same question, so a
  changing candidate set orphans rows instead of silently reusing an answer.
- SQLite **solution grid** — one queryable row per judgment (subject × candidate) with
  provenance, timestamps and an `invalidated` flag; indexes on subject.
- **Soft invalidation** with a stored reason: forget only what changed, never delete history.
- **Confidence routing** — `act` / `act_and_flag` / `escalate` around configurable thresholds
  (default `0.85` / `0.60`), so uncertain judgments go to a human instead of through.
- **Loop contract** — `is_terminal(decisions)` reports when a whole batch can run unattended;
  `flush()` commits and closes for safe script and CI exit.
- **Cost accounting** — per-call pricing (`$42 / billion input tokens`, ~400 tokens per call),
  session counters and a `grid_stats` row tracking hits, misses, calls and dollars saved.
- **Vision bridge** — `evaluate_from_image()` extracts text from an image once per unique image
  hash and feeds it through the normal path (Jev itself is text-only).

### Added — domains

- Reference adapters inside the substrate self-test: **Fitment** (vehicle × parts) and
  **QC disposition** (defect signature × dispositions).
- Plug-in domain `deal_scoring` — listing × grade (buy / hold / pass / scrap).
- Plug-in domain `catalog_watchdog` — page pair × verdict (no_digest / digest / escalate).
- Plug-in domain `resource_triage` — a six-layer decision tree:
  theme → source → content → freshness → consumption → action, each layer its own adapter with
  staged state threading.
- **Adapter contract**: one file, seven members, zero substrate changes.

### Added — tooling

- `funnel.py` — batch CLI: corpus intake, staged pipeline, human-flag report, opt-in queue
  write. `--hopper DB` enriches every stage with what the scrobbler already knows (already
  queued, already consumed, channel track record, backlog by priority) and applies a dedupe
  gate before writing queue rows.
- `bucket_delineation.py` — perturbation-based stability prober plus per-field ablation, to find
  which input fields a decision is actually sensitive to and must therefore be quantized.
- `test_call_efficiency.py` — call-count and cache-hit assertions (the cost claim, tested).
- `examples/resource_triage.py` — real-data funnel demo: a timeline of resources → queue rows,
  runnable against the inline sample corpus or `YT_TIMELINE`.

### Added — docs and infra

- `README.md`, `CONTRIBUTING.md` (the adapter contract written down), `PR_REVIEW_BOT.md`
  (how the Codex review bot works and how to toggle it), and `docs/architecture.md`.
- CI on every push and pull request: the 55-test suite plus every module's offline mode, so the
  documented examples cannot rot.
- Codex pull-request review workflow (`openai/codex-action@v1`) committed; it activates when an
  `OPENAI_API_KEY` secret is configured.

### Notes

- The repo runs **fully offline** on stand-in primitives; live mode requires the Jev SDK
  (`typesafe_sdk` + `TYPESAFE_API_KEY`) and uses the same code path.
- All adapter weights shipped here are reference examples, not settled truth.

[0.1.0]: https://github.com/taretakt/decision-connector/releases/tag/v0.1.0
