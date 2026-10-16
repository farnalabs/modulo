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

## PrimeUI licence for local dev and e2e (FAR-1858)

PrimeVue 5 verifies a licence key at runtime and shows an "Invalid PrimeUI
License" banner when none is configured. Staging and production receive the key
as a Docker build-arg (`VITE_PRIMEUI_LICENSE`, from the deploy workflow's
GitHub secret). For local development there is no build step, so
`frontend/vite.config.ts` lifts `VITE_PRIMEUI_LICENSE` from the nearest
ancestor `.env` file (the workspace root `.env` in the standard layout) into
the process environment when it is not already set — Vite then inlines it into
`import.meta.env` exactly as it does for the production build. This covers
`pnpm dev` and the Playwright e2e `webServer` with one mechanism.

If you see the banner locally, add `VITE_PRIMEUI_LICENSE=<key>` to your
workspace root `.env` (gitignored — never commit the key; the repo is public
and `VITE_*` values are inlined into the client bundle). A shell-exported
`VITE_PRIMEUI_LICENSE` takes precedence over the `.env` lookup.
