# Modulo

Product-specific agent guidance for the `farnalabs/modulo` repository.

## What this repo is

Self-hosted agent governance for agentic SDLC pipelines. Backend: Python 3.12
FastAPI. Frontend: Vue 3 + Vite SPA. Runs on Postgres + Redis. Product
requirements and behaviour spec live in `docs/` and the product map
`frontend/src/manifest.yaml` (ADR 008). The PRD is retired.

## Repository structure

```
modulo/
  backend/                   # Python 3.12, uv, FastAPI
    src/modulo/
      api/                   # FastAPI routes, WebSocket, MCP server
      core/                  # pipeline_engine, schema_registry, trigger_engine, hitl_manager, ...
      db/                    # SQLAlchemy models, Alembic migrations, rls.py, models/
      connectors/            # ConnectorType ABC + connectors/{filesystem,github}
      model_backends/        # BaseChatModel ABC + {anthropic,openai,stub}
      auth/                  # JWT, Basic Auth, API key validation
      otel_bridge/           # LangGraph -> OTel callback handler
    tests/unit/              # No DB, StubModelBackend, fast
    tests/integration/       # Testcontainers Postgres, real migrations
    tests/bdd/               # pytest-bdd steps + Gherkin features
  frontend/                  # Vue 3 (Composition API), Vite, Pinia, pnpm
    src/{stores,components,views,composables}
    src/manifest.yaml        # product map (ADR 008)
    tests/e2e/               # Playwright
  docs/                      # architecture.md, core-principles.md, security/ (ADRs moved out — see below)
  scripts/                   # dev helper scripts
  deploy/                    # Fly/Caddy/nginx/supervisor configs; deploy/compose/ + deploy/docker/ hold non-default compose files and Dockerfiles
  configs/                   # grafana dashboards, otel-collector config
  .semgrep/                  # custom lint rules (rls, credentials, jinja2, yaml, asyncdb)
  .github/workflows/         # CI, Deploy, merge-queue (autonomous PR lifecycle)
```

## Where to look first

- **Product requirements / behaviour:** `docs/architecture.md`, `docs/core-principles.md`
- **Architecture decisions:** ADRs (migrated out of this repo 2026-09-02, FAR-434; previously in `docs/adr/`)
- **Product map:** `frontend/src/manifest.yaml`

## Working-directory rules (non-negotiable)

- **All Python tooling** (`uv run pytest` / `mypy` / `ruff`) runs from `backend/` - never the repo root.
- **All Node tooling** (`pnpm run lint` / `test:unit` / `vue-tsc`) runs from `frontend/` - never the repo root.
- Reproduce the exact command and working directory CI uses when diagnosing a failure - do not add or drop flags.

## Lessons Learned

> **Scope: codebase only.** Lessons recorded here are about the product codebase
> itself: backend, frontend, database schema and migrations, tests, and the
> tooling that runs against the code. Lessons about **deployments**, the
> **dogfood / autonomous SDLC pipeline**, or **developer environments** (machine
> setup, OS quirks, worktrees, CI infrastructure) do **not** belong here; those
> live in the workspace-level `AGENTS.md`.
>
> Relocated out of this file to the workspace-level `AGENTS.md` on 2026-09-17
> ([PR #686](https://github.com/farnalabs/modulo/pull/686)):
>
> - Postgres NUL-byte json-to-jsonb migration gotcha (2026-08-26)
> - E2B sandbox `timeout_seconds` 1-hour cap (2026-08-31)
> - GitHub Actions API 404 means token scope, not 'not found' (2026-09-01)
> - Windows semgrep fail-open wrapper (`run_semgrep.py`) (2026-09-16)
> - Bot PR closures must leave a loud, auditable reason (2026-08-31)

### Repo-wide pnpm overrides silently clobber transitive exact pins (2026-09-06)

A frontend-workspace-wide override (`js-yaml: ^5.0.0` in `frontend/pnpm-workspace.yaml`) broke `pnpm run generate:api` (FAR-646): `@redocly/openapi-core@1.34.19` pins `js-yaml@4.3.1` exactly and reads the `types` export (`js_yaml_1.types.merge`) that js-yaml 5.x removed, so the override hoisted 5.4.1 into it and openapi-typescript crashed at require time — the fix is a scoped override (`@redocly/openapi-core>js-yaml: 4.3.1`, delete it if openapi-typescript ever upgrades to @redocly/openapi-core 2.x, which wants js-yaml ^5.2.2), and note pnpm v10+ reads overrides ONLY from pnpm-workspace.yaml (an `overrides` edit in frontend/package.json is silently ignored, so "fix it in package.json" cannot work).

### Pre-auth routes need an explicit unauthenticated test (2026-09-16)

Every SSO pre-auth route (`/api/v1/auth/oidc/{provider}/login`, `/api/v1/auth/oidc/{provider}/callback`, `/api/v1/auth/saml/login`, `/api/v1/auth/saml/acs`, `/api/v1/auth/saml/metadata`) was guarded with `require_feature("sso")`. `require_feature` resolves the plan via `get_plan_context`, which depends on `get_current_user`, which depends on `HTTPBearer(auto_error=True)`. A request with no `Authorization` header — exactly what a browser sends when navigating from the logged-out login page — raised `401 "Not authenticated"` before the route handler ran. The feature was marked covered in the product map and had passing unit and BDD tests, because every test called those routes with an authenticated client. The real flow (an unauthenticated user clicking the sign-in button) was never exercised, so the feature was unreachable end-to-end while CI stayed green.

Rules:

1. Any route reachable before authentication needs at least one test that calls it with NO credentials and asserts the intended pre-auth behaviour (200/302/400/402 as applicable), and explicitly asserts it is NOT a 401.
2. A dependency that composes an authenticated dependency (`require_feature`, `get_plan_context`, `get_current_user`) must never guard a pre-auth route; use an anonymous resolver instead.
3. "Covered" is a claim about user-reachable behaviour, not about the existence of tests. A feature whose primary entry point is unreachable is not covered.
4. A mocked or authenticated test client does not exercise the unauthenticated path — add the unauthenticated case explicitly.
