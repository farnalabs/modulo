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
- Node.js 20+ (CI pins 22) with `pnpm` - the repo pins `pnpm@11.28.9` in
  `frontend/package.json` (`packageManager`), so use pnpm rather than npm
- Docker Desktop (for local Postgres/Redis)
- UV package manager, 0.11.13 to match the Docker image pin
  ([install](https://docs.astral.sh/uv/getting-started/installation/))

## PrimeVue licensing

PrimeVue is pinned to the MIT-licensed v4 line (`primevue@4.5.5`), so no licence
key, banner or build-arg is involved in local development, e2e or production.
An old PrimeVue licence entry in a workspace `.env` is inert and can be
removed.

## E2E visual/token slice on PRs (FAR-2017)

Most Playwright specs run only in the post-deploy staging `@regression` gate.
That left the specs asserting rendered CSS, design tokens and layout invisible
to PR CI: a token change passed every check, landed on `main`, and only failed
after the staging deploy, blocking the deploy train (FAR-2016's
`json-viewer` light-mode probe; FAR-1913/FAR-2028's mobile-layout axe
`color-contrast`).

The `frontend` CI job now runs a narrow, **unconditional** slice of those
specs against the local mock target on every frontend PR:

| Spec | Why it is in the slice |
|---|---|
| `json-viewer.spec.ts` | Design-token colours (incl. the light-mode probe) and long-value geometry |
| `staging-wcag.spec.ts` | WCAG AA axe audit (contrast/token class). Misleadingly named, but it runs against the local target |
| `filter-bar-overlay.spec.ts` | Dropdown overlay anchoring |
| `select-clipping-container.spec.ts` | Overlay escapes clipping ancestors; portalled to body |
| `pipeline-editor-select-overlay.spec.ts` | Editor Selects portal out of the clipped editor column |

Measured cost: **19 tests, ~2 min** locally (serial), versus ~313 tests / ~11
min for the full suite against a deployed target. It is *additive*: the
staging `@regression` gate is unchanged.

The selection lives in `frontend/scripts/e2e-visual-slice.mjs`. `--check`
scans every `@regression` spec for visual-assertion markers and fails if a
spec is neither in the slice nor in an explicit `STAGING_ONLY` list carrying a
reason. This prevents silent under-selection: adding a visual spec without
classifying it is a PR failure, not a quiet coverage gap. The slice is not
changed-path gated on purpose: a path filter can miss a spec whose subject
changed without a matching path, and an under-selecting selector reads as
coverage.

Deliberately staging-only (with reasons in the script): `mobile-layout.spec.ts`
(8.1 min local, and its axe findings can need seeded deployment data),
`pipeline-editor.spec.ts`, `run-detail-metadata.spec.ts`, `notifications.spec.ts`,
`dialog-select-overlay.spec.ts` (a pre-existing local-only focus failure),
and `wcag-contrast.spec.ts` (already runs as its own earlier step in the same
job).

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
