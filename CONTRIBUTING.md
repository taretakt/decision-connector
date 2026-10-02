# Contributing

The substrate is deliberately boring. The interesting part is the domain you
hang off it. This file is the contract for both.

## The one claim to respect

> One file, six members, zero substrate changes.

Every domain ships as a single adapter file. The substrate
(`decision_connector.py`) is shared, tested, and should *never* need to change
to accommodate a new domain. If you find yourself editing the substrate for a
domain, stop — the six-member contract below is missing something.

## The adapter contract (six members)

| Member | Signature | What it means |
|---|---|---|
| `name` | `str` | domain route, e.g. `"deal.scoring"` |
| `evaluation_mode` | `"one_shot" \| "per_candidate"` | the cost lever: one call for all candidates, or one per candidate |
| `subject_key(subject)` | `str` | grid row — what makes this subject itself |
| `candidate_keys(subject, candidates)` | `list[str]` | grid columns — the space of possible answers |
| `state_for(subject, candidates)` | `str` | the state text handed to Jev |
| `questions_for(subject, candidates, candidate_key)` | `dict` of Choice / Noul / Score | the typed primitives |
| `canonical(response, candidate_key)` | `dict` → `Decision` | normalize the Jev response (choice, noul, score, confidence, probabilities, label) |

Ship a `__main__` offline demo and a test file per domain. Demos use the
stand-in primitives — they must run with **zero** network and **zero** keys:
`uv run python3 <your_adapter>.py` must exit 0.

## Verification before you push

```bash
uv run pytest                          # the gate
uv run python3 decision_connector.py   # substrate self-test
uv run python3 bucket_delineation.py   # stability probe
uv run python3 test_call_efficiency.py # call-count assertions
uv run python3 deal_scoring_adapter.py
uv run python3 catalog_watchdog_adapter.py
uv run python3 resource_triage_adapter.py
```

Everything must exit 0. Determinism is a feature, not a fix: the same state
must hit the same judgment — that's what the cache keys guarantee, and tests
assert it.

## Coding rules

- **Byte-deterministic cache keys** — never put units or formatting in a key;
  units belong in the *value*.
- **Canonicalize before you quantize** — if a decision flips on an input,
  that input must be banded (see `bucket_delineation.py`).
- **Grid is the source of truth** for a decision. Cache hits must be
  indistinguishable from fresh judgments in every downstream consumer.
- **No secrets, ever.** No API keys, no internal paths, no environment
  usernames in code or fixtures. GREP before you push:
  `grep -rnE '/opt/|/home/|sk-[A-Za-z0-9]{20}|BEGIN (RSA|OPENSSH|PRIVATE)' .`
- **No `_*.db` files committed.** Grid artifacts are machine state; the
  `.gitignore` already refuses them.

## PR etiquette

- CI must be green *before* a review is requested.
- One concern per PR; reference the domain file, not the substrate.
- The Codex review bot (`openai/codex-action`) comments on every PR when
  `OPENAI_API_KEY` is configured — treat its notes as a second opinion, not a
  blocker. See [PR_REVIEW_BOT.md](PR_REVIEW_BOT.md).

## License

MIT. Code you contribute is yours; the repository stays MIT forever.