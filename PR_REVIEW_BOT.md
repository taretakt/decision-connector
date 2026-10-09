# Codex PR review bot

`.github/workflows/codex-review.yml` runs Codex on every pull request and
posts its review as a comment on the PR.

## What it does

1. Checks out the PR merge ref.
2. Runs `openai/codex-action@v1` with the review brief in
   `.github/codex/prompts/review.md`.
3. Posts the final message back on the PR via `github-script`.

The brief is repo-specific: determinism, cache correctness, routing safety,
secret hygiene. Costs one Codex run per push to a PR — cheap, and exactly the
work OpenAI's Codex Open Source Fund grants are meant to pay for.

## Requirements

- An OpenAI API key stored as the **`OPENAI_API_KEY`** repository secret
  (Settings → Secrets and variables → Actions). Until it's set, the Codex step
  **skips cleanly — empty result, no failed run** — so PRs stay green before
  credits land. Set the secret to turn the review on.

## Toggle

- **Disable**: comment out or delete the `codex` / `post_feedback` jobs, or
  delete the workflow file.
- **Re-enable**: restore the file from git history.
- **Per-PR opt-out**: a PR author can't silence it (Actions don't negotiate);
  for private experiments, open the PR as a *draft* — the workflow triggers on
  `opened` regardless, so instead push to a branch and open the PR when ready.

## Cost guard

The job runs only on `pull_request` events — never on every push, never on
schedules. The prompt caps the review to one pass. If you want stricter
budgets, pin `model` / `effort` inputs in the workflow (see the [action
docs](https://developers.openai.com/codex/github-action)).