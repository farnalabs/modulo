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
  docs/                      # architecture.md, core-principles.md, adr/, security/
  scripts/                   # dev helper scripts
  deploy/                    # Fly/Caddy/nginx/supervisor configs
  configs/                   # grafana dashboards, otel-collector config
  .semgrep/                  # custom lint rules (rls, credentials, jinja2, yaml, asyncdb)
  .github/workflows/         # CI, Deploy, merge-queue (autonomous PR lifecycle)
```

## Where to look first

- **Product requirements / behaviour:** `docs/architecture.md`, `docs/core-principles.md`
- **Architecture decisions:** `docs/adr/`
- **Product map:** `frontend/src/manifest.yaml`

## Working-directory rules (non-negotiable)

- **All Python tooling** (`uv run pytest` / `mypy` / `ruff`) runs from `backend/` - never the repo root.
- **All Node tooling** (`pnpm run lint` / `test:unit` / `vue-tsc`) runs from `frontend/` - never the repo root.
- Reproduce the exact command and working directory CI uses when diagnosing a failure - do not add or drop flags.
