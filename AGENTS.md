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

## Lessons Learned

### Postgres NUL-byte SQL gotcha: json-to-jsonb migrations are not lossless on populated databases

Postgres forbids NUL bytes in any SQL string literal, and this makes every "obvious" pure-SQL approach to finding or stripping NUL-containing rows silently wrong. Discovered 2026-08-26 when migration `0129_runs_json_to_jsonb` (`ALTER ... TYPE jsonb USING col::jsonb`) failed on 5 NUL-byte rows in `runs.node_telemetry_json`, blocking the whole 0127-0150 migration chain on prod (DB stuck at `0126_human_set_eval_type`) and keeping the SAQ worker down (fleet-wide `fire_due_triggers` heartbeat alert storm). Three cleanup attempts failed before the root cause was understood.

Rules:

1. `chr(0)` and `E'\x00'` raise `null character not permitted` — you cannot express the NUL byte in a SQL literal at all. A plain `'\x00'` is FOUR literal characters (`\`, `x`, `0`, `0`) that match nothing, so `regexp_replace`, `LIKE '%\x00%'`, and `position('\x00' in col)` silently do nothing.
2. `col::text IS JSON` returns TRUE for NUL-containing values — only the `::jsonb` cast rejects them. Any `WHERE col::text IS JSON` pre-filter also matches nothing.
3. The only reliable pure-SQL detection is an exception-safe per-row cast test: for each candidate row, `BEGIN; PERFORM col::jsonb; EXCEPTION WHEN others THEN UPDATE ... SET col = NULL; END;` keyed on `ctid`, with `quote_ident()` for identifiers (the DO-block pattern used in the 2026-08-26 prod cleanup).
4. `ALTER ... TYPE jsonb USING col::jsonb` is NOT lossless on populated databases. Before shipping any json-to-jsonb migration, run the exception-safe scan against every target column (all 74 json columns were checked in the incident) and clean NUL rows first.
5. NUL-byte test data must be inserted via psycopg/Python (parameter binding), never a SQL literal — a test that tries to INSERT `'\x00'` via SQL either errors or inserts the wrong bytes.
