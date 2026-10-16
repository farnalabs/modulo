# Development Setup

See [Quickstart](quickstart.md) for local development setup instructions.
See [System Requirements](system-requirements.md) for prerequisites.
See [Deployment](deployment.md) for production deployment.

## Working directories (non-negotiable)

- All Python tooling (`uv run pytest` / `mypy` / `ruff` / `alembic`) runs
  from `backend/` - never the repo root.
- All Node tooling (`pnpm run lint` / `test:unit` / `vue-tsc`) runs from
  `frontend/` - never the repo root.

## Required Tools

- Python 3.12+
- Node.js 20+ (CI pins 22) with `pnpm` - the repo pins `pnpm@11.28.5` in
  `frontend/package.json` (`packageManager`), so use pnpm rather than npm
- Docker Desktop (for local Postgres/Redis)
- UV package manager, 0.11.13 to match the Docker image pin
  ([install](https://docs.astral.sh/uv/getting-started/installation/))
