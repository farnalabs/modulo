## Summary
The repository currently fails `ruff check .` and `ruff format --check .` at the repo root, which blocks CI.

### Changes
- **ruff.toml**: Added `.github/scripts/*` to `[lint.per-file-ignores]` for rule `T20`. The `pr-review-dedup.py` CI helper is a standalone CLI that legitimately writes to stdout/stderr, consistent with the existing `scripts/*` and `backend/scripts/*` rules.
- **.github/scripts/pr-review-dedup.py**: Removed the now-redundant `# noqa: T201` directives (the per-file ignore covers them; they were also triggering RUF100).
- **docs/connector-authoring.md**, **docs/model-backend-authoring.md**: Formatted the embedded Python code blocks so `ruff format --check .` passes.

### Verification
- `ruff check .` -> All checks passed
- `ruff format --check .` -> all files already formatted

These are mechanical, low-risk config/formatting fixes that make the repo's own lint/format gates green again.
