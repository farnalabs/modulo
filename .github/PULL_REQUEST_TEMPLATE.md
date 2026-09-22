## Description

<!-- Briefly describe the change and why it's needed. -->

## Changelog classification

<!-- Apply EXACTLY ONE `changelog:*` label to this PR (a CI check fails
     user-facing PRs that carry none):
  - `changelog: breaking` — breaking change
  - `changelog: feature`  — new feature
  - `changelog: fix`      — bug fix
  - `changelog: none`     — internal / refactor / test / docs / infra work;
                            a valid and honest answer, not a cop-out
Only `feature`, `fix`, and `breaking` produce a changelog entry. -->

## High-level summary

<!-- One or two sentences a product user would recognise — this is what ends
     up in the release notes. -->

## Checklist

- [ ] ruff, mypy, bandit, semgrep pass
- [ ] All tests pass (pytest + vitest)
- [ ] vue-tsc --noEmit passes (for frontend changes)
- [ ] No .env files or secrets committed
- [ ] Changelog updated (if user-facing)
