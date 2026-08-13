## What

<!-- Briefly describe the change and what it does. Link the issue/task it resolves, e.g. "Fixes #123" or "Part of FAR-16". -->

## Why

<!-- Why is this change needed? What problem does it solve? -->

## How

<!-- How does the change work? Key implementation decisions, trade-offs, or alternative approaches considered. -->

## Tests

<!-- What did you run to verify this change? Name the actual suites/files, e.g. "uv run pytest tests/unit/... -q" or "pnpm run test:unit". -->

- [ ] Backend tests run
- [ ] Frontend tests run

## Screenshots / Logs

<!-- If user-facing, add screenshots. For backend changes, include relevant logs or output. Delete this section if not applicable. -->

## Checklist

- [ ] ruff, mypy, bandit, semgrep pass
- [ ] All tests pass (pytest + vitest)
- [ ] vue-tsc --noEmit passes (for frontend changes)
- [ ] No .env files or secrets committed
- [ ] Changelog updated (if user-facing)
