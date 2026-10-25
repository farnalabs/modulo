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
- Node.js 20+ (CI pins 22) with `pnpm` - the repo pins `pnpm@11.28.7` in
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
the process environment when it is not already set; Vite then inlines it into
`import.meta.env` exactly as it does for the production build. This covers
`pnpm dev` and the Playwright e2e `webServer` with one mechanism.

If you see the banner locally, add `VITE_PRIMEUI_LICENSE=<key>` to your
workspace root `.env` (gitignored, so never commit the key; the repo is public
and `VITE_*` values are inlined into the client bundle). A shell-exported
`VITE_PRIMEUI_LICENSE` takes precedence over the `.env` lookup.

## Docs validity sweep

`scripts/check_doc_validity.py` checks the product docs (`README.md` and
`docs/**/*.md`) for three classes of defect:

- retired tokens: organisation names, paths and environment flags the repo no
  longer recognises, such as a retired top-level AGENTS.md path, or a `ghcr.io`
  image path whose first path segment is not `farnalabs` (a placeholder such as
  `<org>`, `{org}` or `$ORG`, or a third-party organisation). A bare
  `ghcr.io/farnalabs` is not flagged;
- backticked paths that begin with a known top-level prefix (`docs/`,
  `backend/`, `scripts/` and the other entries in `PATH_PREFIXES`) and contain
  no space or placeholder character, but do not exist in the checkout.
  References to a sibling workspace repo (`Repos/...`) are listed as skipped,
  not checked;
- advertised URLs that do not resolve (a bounded HTTP status probe).

Run it from the repository root:

```bash
uv run --project backend python scripts/check_doc_validity.py
uv run --project backend python scripts/check_doc_validity.py --no-urls
uv run --project backend python scripts/check_doc_validity.py --self-test
```

Exit code 0 is clean; 1 means a stale reference, a missing path or an
unreachable URL was found, or that the `--max-urls` cap (default 100) left URLs
unprobed - a capped run is not a clean result. URLs over the cap are reported as
not checked; raise the cap to cover them. `--self-test` exercises the checker's
own detection rules without scanning the repository.

The check does not verify prose accuracy, markdown anchors, backticked paths that
do not begin with one of those prefixes, or the docs in `Repos/modulo-website`;
review those by hand.
