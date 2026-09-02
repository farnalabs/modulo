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

### Sandbox node timeout_seconds must stay under the E2B 1-hour cap (2026-08-31)

The e2b SDK upgrade shipped in the Aug 30 deploy (Python 3.14) began enforcing E2B's 1-hour sandbox timeout cap: a sandbox_agent node with timeout_seconds=3600 now fails provisioning with `400: Timeout cannot be greater than 1 hours` (previously accepted). GraphValidator rejects >3300 at save time (headroom for provisioning). A sandbox-provisioning failure must never be maskable as a completed run - the Prompt-to-PR pipeline's blocking output-contract eval is the reference mitigation; the failure envelope now carries the provider error.

### GitHub 404 on an Actions API call means token scope, not 'nothing found' (2026-09-01)

The deploy throttle's in-progress check used MODULO_REVIEWBOT_TOKEN, which cannot read the Actions runs API — GitHub returns 404 (not 403) for resources a token can't see. The script treated the failed check as 'a deploy run is already in progress' and skipped: five green deploy runs deployed nothing while production starved for 2 days (FAR-528). Rules: (1) in CI scripts, use ${{ github.token }} for same-repo API reads; a PAT lacking scope yields 404, which looks exactly like an empty result; (2) a guard step must distinguish 'API failed' from 'condition met' — fail open with a loud ::warning:: rather than skip on unknown state; (3) never interpolate an API response body into a notice as an identifier; (4) a deploy workflow that exits success without deploying is the worst failure mode — the deploy-staleness-check cron compares prod's build SHA to origin/main HEAD so starvation is visible.

### Bot PR closures must leave a loud, auditable reason (2026-08-31)

PR #2092 (deliver/FAR-438) was closed by `github-actions[bot]` in the 2026-08-31 00:11 UTC merge-queue tick and the closure reason was invisible at first glance: the `closed` timeline event carries no reason field, and the one bot comment that DID carry it (the duplicate-close comment) sat buried in a 50+ event thread behind hours of Branch Fixer churn. A later delivery session nearly re-raised work that had already piggybacked into main because "why was this closed?" took hours to answer. Every close site in `.github/workflows/merge-queue.yml` also used `gh pr close --comment "..." 2>/dev/null || true` - a comment-posting failure is silently swallowed, producing exactly the no-reason closure that is indistinguishable from a buggy or malicious one.

Rules:

1. Every bot-driven PR closure must post a closure comment stating the reason. The `closed` event has no reason field, so the comment IS the audit trail: post the reason with `gh pr comment` before closing (or pass `--comment` on the close), never a bare close.
2. Comment-posting failures must never be silenced - `2>/dev/null || true` on a close+comment command hides exactly the failure that makes closures un-auditable. Post the comment separately from the close: if `gh pr comment` fails, still proceed with the close but emit a visible `::warning::closure comment failed for PR $NUM - audit trail missing` annotation (reference pattern: the "Close duplicate PRs" and post-merge close steps in `.github/workflows/merge-queue.yml`).
3. When auditing "who closed this PR", read the issue-timeline `commented` events immediately BEFORE the `closed` event: `gh pr close --comment` posts the comment ~1 second before the close event, and `gh pr view --json` / the bare `closed` event alone make the closure look reason-less even when the comment posted (this exact false lead cost the #2092 investigation).
