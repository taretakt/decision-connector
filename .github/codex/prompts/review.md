# Codex review brief — decision-connector

Review this pull request for correctness, determinism, and security.

This repository is a decision-automation substrate between structured systems
and Jev's typed decision primitives. The invariants that matter:

1. Cache keys stay byte-deterministic (`cache_key`, `canonical_question_hash`).
   A change that alters a key for the same logical input is a breaking change.
2. The SQLite `grid` schema must not change silently — stored decisions are
   long-lived and read by the confidence router.
3. Confidence routing thresholds (`route`: act ≥ 0.85, escalate ≤ 0.60) must
   keep explicit tests when touched.
4. The offline stand-in primitives (Choice/Noul/Score on ImportError) must keep
   the same kwargs and attributes as the real SDK, or the offline test suite
   stops exercising the real `questions_for()` path.
5. No new hardcoded local paths, credentials, or personal-environment strings.

Be specific: cite files and lines. If the PR is sound, say so plainly in a few
lines. If anything above is violated, explain the fix before approving style.