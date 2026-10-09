## What this does

One sentence. Reference the issue it closes with `Closes #NN`.

## Contract check

- [ ] Adapts to the 7-member adapter shape if it touches adapters
- [ ] Offline suite passes (`uv run pytest`) and CI is green
- [ ] No runtime deps added; no `sys.path` or environment hacks
- [ ] Determinism: same spec + same data → same output

## Anything a reviewer should watch

- Decisions/costs/confidence routing touched?
- New cache keys or question-hash behavior (breaking cached rows)?
- Private paths or credentials in examples?

## Proof

Output from the relevant offline run or test (short form).