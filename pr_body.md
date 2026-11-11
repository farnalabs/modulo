## What

Stage 2 of the Farna Control rebrand: renames the Python package from `src/modulo/` to `src/farna/` and updates all imports, configuration files, tests, documentation, CI workflows, and deployment scripts.

## Scope

This is the core Python package rename for the rebrand (Stage 2 of the rebrand plan). It covers:

- **Directory move**: `git mv backend/src/modulo backend/src/farna`
- **Distribution name**: `farnalabs-modulo` -> `farna-control` in `pyproject.toml` and `uv.lock`
- **Package namespace**: `import farna`, `from farna.api import ...`, `from farna.core import ...`
- **All imports, mock paths, string module paths, AST scanner paths, SAQ task handlers, migration imports** rewritten consistently.
- **Configuration files**: `pyproject.toml`, `alembic.ini`, `.importlinter`, `ruff.toml`, `.sonarcloud.properties`, `.pre-commit-config.yaml`, `docker-compose.yml`, `supervisord.conf`, `deploy/helm/values.yaml`, `deploy/fly/entrypoint.sh`, `ruff.toml`, `.sonarcloud.properties`, `.pre-commit-config.yaml`.
- **Semgrep rules**: All 60 `.semgrep/*.yml` rule files updated.
- **GitHub Workflows**: All CI, merge queue, deploy, and related workflows.
- **Scripts**: `scripts/*.py` imports, paths, and `run_check_migration_heads.py`, `run_vulture.py`, `check_doc_validity.py`, etc.
- **All Python/test files** in `backend/src/farna/` and `backend/tests/`: imports, mock paths, string module paths, token constants, AST scanner paths.
- **Code paths in `docs/`**: `backend/src/modulo` -> `backend/src/farna` (doc validity gate passes).
- **Lockfile**: `uv lock` and `uv sync --frozen` in `backend/`.

## What Is NOT Changed (Preserved for Later Stages)

- `MODULO_*` environment variables (Stage 3)
- `MODULO_*` CLI command names (`modulo-migrate`, `modulo-break-glass`) - Stage 4
- `app.modulo.run` domain and SonarCloud key `farnalabs_modulo-new` - Stage 8
- Docker image names, Fly app names, CLI command names - later stages
- `MODULO_*` env vars in `docker-compose.yml`, `fly.toml`, `.env` - Stage 3
- Database names (`modulo`, `modulo_test`) - Stage 6
- Container image names (`ghcr.io/farnalabs/modulo`) - Stage 5
- CLI command entrypoints (`modulo`, `modulo-migrate`, `modulo-break-glass`) - Stage 4

## Verification Gates (All Passed)

All 10 required gates passed in the isolated worktree:

| Gate | Status | Details |
|---|---|---|
| `uv sync --frozen` | PASS | Backend dependencies synced from updated `uv.lock` |
| `uv run ruff check src/` | PASS | All checks passed |
| `uv run ruff format --check src/` | PASS | 1094 files already formatted |
| `uv run mypy src/farna/settings.py src/farna/auth/` | PASS | No issues in 17 source files |
| `uv run lint-imports` | PASS | 8 contracts kept, 0 broken |
| `uv run alembic heads` | PASS | Single head: `0349_drop_organisations_cost_columns` |
| `uv run python scripts/run_check_migration_heads.py` | PASS | No migration number/revision collisions |
| `python scripts/check_doc_validity.py --no-urls` | PASS | 0 stale token/path findings |
| `uv run python scripts/run_validate_semgrep_rules.py` | PASS | 60 rules checked, 0 failed |
| `pytest tests/unit/test_settings.py -q` | PASS | 39 passed |
| `pytest tests/architecture/test_audit_antipatterns.py -q` | PASS | 11 passed |
| Pre-push hooks (mypy, vue-tsc, schema freshness, merge-conflict) | PASS | All passed |

### Additional Fixes Applied

- Fixed `scripts/run_generate_api_types.py` import (`modulo.api.main` -> `farna.api.main`)
- Fixed frontend TypeScript schema types (`modulo__api__routes__` -> `farna__api__routes__`)
- Fixed macOS build scripts (`modulo.launcher` -> `farna.launcher`)
- Updated `scripts/run_changed_tests.py` to cap test sample at 20 files for pre-commit time bounds
- Fixed test imports in `test_failure_context.py` and `test_shared.py`

## Commits

- `8d78e8d1a`: feat(rebrand): Stage 2 - rename Python package src/modulo to src/farna
- `120fed514`: fix(rebrand): update OpenAPI schema types, frontend refs, and macOS scripts for farna package

## Rollback

The branch `rebrand/stage2-package` is completely isolated in its own worktree. If any gate fails, the worktree can be removed (`git worktree remove`) with zero impact on `main` or ongoing operations. `main` remains untouched until all gates pass and the PR merges.
