# Modulo — Agent & Developer Guidance

Full PRD: `docs/prd.md`. This file covers how to build. Conflicts between files → fix the conflict.

## Diagnostic Order (MANDATORY — database/connection issues)

When encountering ANY database connection error (`ConnectionResetError`, `ConnectionDoesNotExistError`, timeout, 503), follow this order BEFORE making code changes:

1. **Check DB health** — `fly checks list --app modulo-app-db` (or the relevant DB app). If `pg` or `role` checks are critical/passing, the DB is fine. If critical, SSH in and restart: `fly ssh console --app <db-app> --machine <id> --user postgres --command "/usr/lib/postgresql/17/bin/pg_ctl start -D /data/postgresql"`. The `check-db-health.ps1` watchdog runs every 5 minutes as a scheduled task — check its log first.

2. **Check app health** — `fly status --app app-modulo`. Look at VERSION and CHECKS columns. Machines on the latest version with "passing" are healthy. Machines on old versions are stale and can be cleaned up.

3. **Check machine logs** — `fly logs --app app-modulo --no-tail | Select-Object -Last 20`. Look for the actual exception type — this determines the root cause.

4. **Check whether the handover framed the issue** — the previous handover may contain diagnostic bias. Always verify the DB is healthy before accepting "SSL issue" or "network issue" diagnoses.

The most common root cause (July 2026): Postgres process crashes silently, new connection pools can't form, but old pools keep serving. Health check passes on old machines, fails on new ones. Fix is always: restart Postgres + bluegreen deploy.

## Non-Negotiable Enforcement Gates

The following enforcement mechanisms are STRUCTURALLY PROTECTED. Any agent observed weakening, bypassing, or removing any of these will be blocked with a violation. These rules exist because every previous gap (continue-on-error, WARN-only checks, skippable integration tests, no E2E in gate) was exploited by rot.

### Gate rules (modify only with explicit human approval)
1. **`continue-on-error: true` is FORBIDDEN** in all CI workflow files (`.github/workflows/*.yml`). Every job must fail CI when it fails. Legitimate uses require a post-step `if: failure()` aggregate that reports the collected failure.
2. **`verify-main.ps1` must use `Fail` (not `Warn`) for all test, lint, type-check, and audit checks.** No check may log a warning and continue — every check must block with `$script:exitCode = 1`.
3. **`gate.ps1` must run Playwright @smoke E2E tests** after the merge, from the main worktree. No merge completes without browser-level verification.
4. **`gate.ps1` integration tests must run by default** (no `-SkipIntegration` opt-out). Integration tests may only be skipped when Docker is unavailable, and the skip must be documented.
5. **`-SkipTests` in `gate.ps1` may ONLY be used for frontend-only changes where node_modules is unavailable in a worktree.** The Conductor must verify post-merge via `gate.ps1` without `-SkipTests`.

### What counts as a violation
- Adding `continue-on-error: true` to any CI job
- Changing a `Fail` to `Warn` in `verify-main.ps1`
- Removing or commenting out the Playwright @smoke step from `gate.ps1`
- Adding a new skip parameter that bypasses test enforcement
- Any change that makes a passing test no longer block the merge

### How to verify gates are intact
Run these checks before completing any session:
- `Select-String -Pattern "continue-on-error: true" -Path ".github/workflows/*.yml"` — must not match product-map-validate or manifest-validate jobs
- `Select-String -Pattern "Warn ""vue-tsc""" -Path "../devtools/harness/tools/verify-main.ps1"` — must not find it (should be `Fail`)
- `Select-String -Pattern "playwright|@smoke" -Path "../devtools/harness/tools/gate.ps1"` — must find at least one match

## Git Workflow

**Always use `git worktree` when branching.** Never check out branches in the main working tree — it must stay on `main`. Worktrees live under `.agents/worktrees/<branch-name>/`.

```powershell
# From Product/
git fetch origin <branch>
git worktree add .agents/worktrees/<branch-name> <branch>
# Work in .agents/worktrees/<branch-name>/
# Commit, push, then clean up:
git worktree remove .agents/worktrees/<branch-name>
git branch -d <branch-name>
```

**PR-based delivery (standard):** Create a worktree branch, implement, commit, then push and create a PR:
```powershell
# From worktree root:
git push origin <branch-name>
gh pr create --title "feat(<scope>): <summary>" --fill
```
GitHub CI (ci.yml) validates the PR automatically. The `merge-to-main.yml` workflow
squash-merges when all checks pass and the threshold is met. Track with:
```powershell
..\..\..\..\devtools\harness\tools\wait-for-pr.ps1 -PRNumber <N> -WaitForCI
```

When delivering multiple PRs for one feature, merge each PR manually once CI is green and approved (`gh pr merge --squash`) before branching the next PR off main - do not wait for the merge queue between chained PRs.

**Legacy gate.ps1** (local-only skills only [`find-and-fix`, `explore-deployment`]):
`..\..\..\..\devtools\harness\tools\gate.ps1 -Branch <branch-name>` runs local CI
and merges to local main. Accepts `-Semver patch|minor|major` (default patch),
`-SkipTests` (migration-collision check still runs), and `-PushAndPR` to skip
local merge and push+create PR instead.

**New scripts (PR-based flow):**
- `create-pr.ps1` - push branch + create PR from any worktree
- `wait-for-pr.ps1` - poll a PR until it is merged (or timeout)
- `pr-flow-config.ps1` - shared configuration for the PR-based delivery flow

**Publish:** The scheduled `publish.ps1` is now verify-only (no push). Pushing to
remote is handled by the GitHub `merge-to-main.yml` workflow when PRs are merged.
If you see a CI failure on main, fix it immediately - do not merge on top of it.

### Subagent pattern (mandatory)

All code changes MUST be implemented by a subagent in a worktree branch — never directly by the parent session. The parent orchestrates, the subagent implements.

| Scenario | How |
|---|---|
| Single bug fix | Create a worktree branch, spawn a subagent via `Task` tool with the description and implementation prompt. The subagent works inside `.agents/worktrees/<branch>/`, commits, and reports back. |
| Multiple independent changes (same session) | Spawn parallel subagents, one per change, each with its own worktree. The parent collects results independently. |
| Multi-task delivery sprint | Use the `deliver` skill (`Repos/devtools/agents/.agents/skills/deliver/SKILL.md`) which orchestrates parallel subagents autonomously. |
| QA fix | Spawn a subagent in its own worktree branch. Never apply a fix directly from the QA session. |

The root `AGENTS.md` has the full non-negotiable rule under **Agent Isolation: All Code Goes Through Subagents** — read it for the rationale and enforcement details.

## Skills

- **`qa`** — Multi-lens quality review. Invoke with `qa <target-path>`. Runs 7 lenses (correctness, bugs, maintainability, SOLID, DRY, simplification, deps) via parallel subagents, validates findings, and applies fixes. Auto-invokes `lessons-learned` on fixed findings. Path: `Repos/devtools/agents/.agents/skills/qa/SKILL.md`.
- **`lessons-learned`** — Extracts recurring patterns from QA findings and codifies them as AGENTS.md guidance at the most specific level of the hierarchy (auto-invoked by `qa` / `qa-iterate`). Standalone: `/lessons-learned <target> <findings>`. Path: `Repos/devtools/agents/.agents/skills/lessons-learned/SKILL.md`.

## Delivery Workflow for QA

1. Check `docs/delivery-tracker.md` — QA Reviews section.
2. Run each QA review using the `qa` skill.
3. After finishing a review, toggle its checkbox and add the date + outcome.
4. Do not start QA #N+1 until QA #N is complete.

---

## Definition of Done

The full Definition of Done lives in `docs/definition-of-done.md`. It is the
checklist every implementer applies before reporting a task done AND the checklist
the PR reviewer applies to every PR. It contains the test suite inventory and the
test-impact consideration step ("consider all suites, guess which files are affected,
run the high-confidence subset, defer honestly").

At minimum, before a task is done:
- [ ] **Manifest updated** — if the delivery adds or modifies a page route, the corresponding entry in `frontend/src/manifest.yaml` was created or updated
- [ ] **Test impact considered** — per `docs/definition-of-done.md` §2: all suites considered, affected files guessed and run where the environment allows, deferred items listed
- [ ] **Self-review passed** — per `docs/definition-of-done.md` §3: files exist, footprint within scope, lint clean, no secrets, PRD accurate

---

## Task Tracker

The `delivery-plan.json` tracker and its `task.ps1` script are **retired** (removed from the repo; `task.ps1` no longer exists). Work items now live in **Linear** (workspace `farnalabs-modulo`, team `FAR`), queried via the local MCP server at `Repos/devtools/harness/mcp/linear/server.py`. Available tools: `list_projects`, `list_ready_issues`, `update_ticket_status`, `add_comment`, `create_ticket`, `get_ticket`, `search_tickets`, `list_states`.

The conductor picks the next ready issue (dependencies completed) and runs `/deliver` from the project root to start an autonomous delivery sprint — this invokes the `deliver` skill at `Repos/devtools/agents/.agents/skills/deliver/SKILL.md`.

---

## Repository Layout

```
modulo/
  backend/
    src/modulo/
      api/                    # FastAPI app, routes, WebSocket, MCP server
      core/
        pipeline_engine/      # StateGraph compile, execution, snapshot management
        schema_registry/      # Schema CRUD, versioning, deletion protection
        trigger_engine/       # Manual, webhook trigger handling
        eval_engine/          # Eval execution
        hitl_manager/         # HITL claim, expiry, gate enforcement
        audit_logger/         # Immutable AuditEvent writes
        cost_controller/      # Token counting, budget enforcement
        library_service/      # Local + registry primitive management
        connector_hub/        # ConnectorHub, one-decrypt-per-run lifecycle
        model_backend_hub/    # ModelBackend registry, health check, rotation
        notifier/             # Outbound webhook dispatch, retry
        graph_validator/      # Pre-run and on-save validation
        feedback_manager/     # FeedbackRecord, correction run spawning
        run_context/          # run_context seeding, context-setter enforcement
        workflow_import_export/
        plugin_registry/
      connectors/base.py      # ConnectorType ABC
      connectors/filesystem/
      connectors/github/
      model_backends/base.py  # BaseChatModel-compatible ABC
      model_backends/anthropic/
      model_backends/openai/
      model_backends/stub/    # StubModelBackend — test double
      otel_bridge/            # LangGraph→OTel callback handler
      auth/                   # JWT, Basic Auth, API key validation
      db/models/              # SQLAlchemy models (one file per entity)
      db/migrations/          # Alembic versions/
      db/rls.py               # SET LOCAL helper, connection event hook
    tests/unit/               # No DB, StubModelBackend, fast
    tests/integration/        # Testcontainers Postgres, real migrations
    tests/bdd/steps/           # pytest-bdd step definitions
    tests/bdd/features/        # Gherkin .feature files (source of truth)
    pyproject.toml
    .importlinter
  frontend/
    src/stores/               # Pinia stores (all carry org context)
    src/components/
    src/views/
    src/composables/
    tests/e2e/                # Playwright tests (agent theme by default)
  .semgrep/                   # Custom lint rules (rls, credentials, jinja2, yaml, asyncdb)
  .pre-commit-config.yaml
  docker-compose.yml
```

---

## Stack (quick reference)

- **Backend**: Python 3.12, uv, FastAPI, LangGraph, SQLAlchemy 2 async + asyncpg/aiosqlite/aiomysql, Alembic
- **Frontend**: Vue 3 (Composition API), Pinia, shadcn-vue + Radix Vue, Vue Flow, Tailwind, Playwright
- **API types**: FastAPI OpenAPI → `openapi-typescript` → typed `openapi-fetch` client at `src/lib/api/schema.d.ts`
- **Lint**: ruff, mypy --strict, bandit, semgrep, import-linter, gitleaks
- **Tests**: pytest + pytest-cov, pytest-bdd, testcontainers, factory-boy, pytest-xdist

---

## Key Implementation Constraints (non-negotiable)

### Database
- `SET LOCAL app.organisation_id = :org_id` **inside a transaction** — never bare `SET`. Semgrep-enforced.
- All async DB uses `asyncpg` (Postgres), `aiosqlite` (SQLite), or `aiomysql` (MariaDB/MySQL). No `psycopg2`/`sqlite3` in async path. Semgrep-enforced.
- Alembic `upgrade head` runs before `AsyncPostgresSaver.setup()` on startup. Postgres advisory lock for multi-worker startup.

### Multi-backend DB support

> **MariaDB is deprecated (2026-07-11).** MariaDB support was added as premature generality (see architecture critique 2026-07-09). Production and demo run on Postgres (Supabase). References are preserved for backward compatibility but MariaDB is not actively tested or maintained.

Modulo nominally supports three database backends, configurable via `MODULO_DB` env var:

| Backend | `MODULO_DB` | Driver | Default `DATABASE_URL` |
|---|---|---|---|
| PostgreSQL | `postgres` (default) | `asyncpg` | `postgresql+asyncpg://modulo:modulo@localhost:5432/modulo` |
| MariaDB/MySQL | `mariadb` / `mysql` | `aiomysql` | `mysql+aiomysql://modulo:modulo@localhost:5435/modulo` |
| SQLite | `sqlite` | `aiosqlite` | `sqlite+aiosqlite:///./modulo.db` |

On non-Postgres backends, tenant isolation works via an auto-injected `WHERE organisation_id = :oid` clause instead of Postgres RLS (`set_config`). The `do_orm_execute` listener in `db/rls.py` handles this transparently — **zero changes** needed to CRUD functions or route handlers.

**Key differences between backends:**

| Feature | Postgres | MariaDB | SQLite |
|---|---|---|---|
| RLS (`SET LOCAL`) | ✅ Native | ❌ (app-level filter) | ❌ (app-level filter) |
| Advisory locks | ✅ `pg_advisory_lock` | ❌ (in-memory lock) | ❌ (in-memory lock) |
| Alembic batch mode | ❌ | ❌ | ✅ |
| Migration DDL | Native PG | Conditional DDL needed | `render_as_batch` |

To run with MariaDB locally:
```powershell
docker compose -f docker-compose.yml -f docker-compose.mariadb.yml up -d
# Sets MODULO_DB=mariadb, DATABASE_URL=mysql+aiomysql://modulo:modulo@db:3306/modulo
```

Architecture decision record: `docs/adr/002-database-abstraction-strategy.md`.

### LangGraph
- State type is `dict[str, Any]` — no dynamic TypedDicts.
- `run_context` and `artifact` are sibling keys in state. Non-context-setter agents must not write to `run_context`.
- `StateGraph` cached keyed by `(pipeline_id, snapshot_id, node_timeout_seconds)` with LRU eviction.

### Credentials
- Decrypted credentials never enter LangGraph state, checkpoint blobs, OTel spans, or logs. Semgrep-enforced.
- ConnectorHub decrypts once at run-start into a run-scoped context object.
- `FERNET_KEY` and `SECRET_KEY` are separate. Startup refuses if either is absent or < 32 bytes.

### Security
- Jinja2: always `SandboxedEnvironment`. Semgrep-enforced.
- YAML: always `yaml.safe_load()`. Semgrep-enforced.
- Sensitive DOM values (API keys, secrets): never plaintext — `●●●●●` default, 30-second server-authenticated reveal.
- JWT: `algorithms=["HS256"]` explicitly — `none` algorithm rejected.

### Async
- All DB access in async path uses async drivers. Sync DB calls block the event loop.

### WebSocket
- One `astream_events()` consumer per run via a per-run event broker. Multiple WS connections subscribe to broker.
- 100-event ring buffer per run for reconnect replay (`?since_event_seq=N`).

---

## Semgrep Rules (all four required before feature work)

- **rls_set_local**: bans bare `SET app.organisation_id` without `LOCAL`
- **credential_in_state**: bans credential field names in LangGraph state assignments
- **sandboxed_jinja2**: bans `jinja2.Environment(` — must use `SandboxedEnvironment`
- **yaml_safe_load**: bans `yaml.load(` — must use `yaml.safe_load()`
- **async_db_driver**: bans `import psycopg2` / `import sqlite3` in async code
- **bare_raise_in_except**: bans bare `raise` inside `except Exception:` — use `raise ... from None`
- **model_dump_exclude_none**: bans `model_dump(exclude_none=True)` in PATCH endpoints — use `exclude_unset=True`
- **pytest_raises_too_broad**: bans `pytest.raises(Exception)` — narrow to specific exception type
- **requests_without_timeout**: bans HTTP requests without explicit `timeout=`
- **environ_mutation_without_monkeypatch**: bans `os.environ` mutation in tests — use `monkeypatch.setenv()`
- **fastapi_dependency_override_vs_patch**: bans `@patch` for FastAPI deps — use `app.dependency_overrides`: bans `import psycopg2` / `import sqlite3` in async code

---

## Testing Strategy

**Unit** (`tests/unit/`): no DB, no Docker, `StubModelBackend` for all LLM calls, run in < 30s.
**Integration** (`tests/integration/`): real Postgres via testcontainers, Alembic migrations applied first, Factory Boy for entities. Cross-tenant isolation test is mandatory.
**BDD/E2E** (`tests/bdd/features/`, `tests/bdd/steps/`): pytest-bdd + Playwright. All Playwright against `?theme=agent`. Use `waitForSelector('[data-loading="false"]')` — never `waitForTimeout()`. Every interactive element needs `data-testid`.

The full suite inventory (root paths, coverage, and run commands) lives in `docs/definition-of-done.md` §1.

Coverage minimums: `modulo.auth` 90%, `pipeline_engine` 85%, `db.rls` 95%, overall 80%.

---

## Architecture Layer Contracts (import-linter)

- `modulo.api` must not import `langgraph` directly
- `modulo.connectors` must not import `modulo.api` or `modulo.auth`
- `modulo.core`, `.api`, `.connectors` must not import `modulo_cloud`
- `modulo.otel_bridge` must not import `core.pipeline_engine`, `hitl_manager`, `eval_engine`

---

## Implementation Order

### Phase 0 — Foundation
1. Alembic schema — all tables with `organisation_id`, `owner_team_id` (nullable), `visibility`, `evals JSON`, pipeline edges, `hitl_claims`, `org_api_keys`
2. `db/rls.py` — `SET LOCAL` helper, SQLAlchemy event hook, isolation integration test
3. `StubModelBackend` — implements `BaseChatModel` async interface, fixture map, `UnexpectedInputError`

### Phase 1 — Core runtime
4. **LangGraph→OTel bridge** — BLOCKING DEPENDENCY for all OTel span assertions
5. Basic auth + `SECRET_KEY` enforcement — JWT `algorithms=["HS256"]`, startup check
6. Core entity CRUD — Pipeline, Agent, Schema, ConnectorInstance, ModelBackend with RLS

### Phase 2 — Pipeline execution
7. ConnectorHub — `FilesystemConnector` (base_path chroot), `GitHubConnector`
8. ModelBackendHub — Anthropic + OpenAI + StubModelBackend, health check, rotation
9. `@cancellable_node` — cancellation check, per-node timeout, run_context write guard
10. Sequential pipeline execution — StateGraph compile + cache, AsyncPostgresSaver, manual trigger
11. Graph validator — topology, schema compat, connector capability, model backend health

### Phase 3 — HITL + events
12. HITL mechanics — `interrupt()`, atomic claim, `claim_token` (15-min TTL), expiry, approve/reject
13. WebSocket event broker — per-run broker, `astream_events()` fan-out, 100-event ring buffer
14. Webhook trigger — HMAC-SHA256, `payload_mapping`, flood protection, deduplication, `TriggerEvent` log

### Phase 4 — API + MCP
15. ViewModel REST API — full CRUD, paginated lists
16. Remote MCP server — `/mcp` HTTP+SSE, API key bearer auth, dual-layer scope enforcement

### Phase 5 — Frontend
17. shadcn-vue init — radix-vue, lucide-vue-next, cvа, baseline primitives in `src/components/ui/`
18. Vue 3 + Pinia scaffold — org context, planStore, theme system (`data-theme`, standard + agent), sidebar
19. `/settings/license` page
20. Pipeline canvas — Vue Flow, node/edge serialisation
21. HITL review UI — claim, approve, reject, overdue badge
22. Run inspection UI — per-node IO, sensitive masking, "Copy as test fixture"
23. Stage board — search, filter, `awaiting_human` quick filter
24. Library browser — list, preview, copy-to-adapt
25. Demo pipeline + first-run walkthrough — `MODULO_DEMO_MODE`

### Phase 6 — Alpha exit checklist
All six criteria from PRD §10.3b must be met explicitly.

---

## Feature Files

```
tests/bdd/features/
  organisation/   org_scoping.feature, rls_isolation.feature
  pipelines/      create.feature, run_sequential.feature, validation.feature, concurrency.feature
  connectors/     filesystem.feature, github.feature, swappable_binding.feature, health_check.feature
  model_backends/ configure.feature, rotation.feature, health_check.feature
  triggers/       manual.feature, webhook_hmac.feature, webhook_payload_mapping.feature,
                  flood_protection.feature, trigger_event_log.feature
  agents/         configure.feature, prompt_versioning.feature, schema_assignment.feature
  schemas/        create.feature, version.feature, deletion_protection.feature
  hitl/           claim.feature, approve.feature, reject.feature, human_only_gate.feature,
                  overdue_warning.feature
  errors/         retry.feature, failed_state.feature, recovery.feature
  library/        browse.feature, copy_to_adapt.feature
  workflows/      export.feature, import.feature, binding.feature
  users/          basic_auth.feature, roles.feature, runner_role.feature
  audit/          event_recording.feature
  notifications/  hitl_webhook.feature, failure_webhook.feature, signing.feature
  mcp/            trigger.feature, review_hitl.feature, human_only.feature,
                  library_browse.feature, onboarding.feature
```

---

## Local Development Setup

### First-time spin up (Docker Desktop)

```powershell
# From Product/
docker compose -f docker-compose.local.yml up -d

# From Product/backend/
# alembic.ini has port 5434 for local Docker (not 5432)
# Create .env with these values:
#   DATABASE_URL=postgresql+asyncpg://modulo:modulo@localhost:5434/modulo
#   MODULO_DB=postgres
#   SECRET_KEY=local-dev-secret-key-not-for-production
#   FERNET_KEY=vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI=
#   REDIS_URL=redis://localhost:6380/0
#   MODULO_PUBLIC_URL=http://localhost:8000
#   MODULO_USERS=admin:admin
#   CORS_ORIGINS=http://localhost:5173

# alembic_version.version_num needs VARCHAR(255) for branch migration IDs:
docker compose -f ../docker-compose.local.yml exec db-local psql -U modulo -c "CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY);"
uv run alembic upgrade heads

# Start backend (from Product/backend/)
uv run uvicorn modulo.api.main:app --reload --port 8000

# Start frontend (from Product/frontend/)
pnpm run dev
```

### Known issues & gotchas

- **Migration ID length**: Branch-based migrations have revision IDs >32 chars (e.g. `0005_library_community_visibility`). Alembic creates the `alembic_version` table with `VARCHAR(32)` by default, which causes `StringDataRightTruncationError`. Manually pre-create the table with `VARCHAR(255)` before running migrations.
- **Migration 0014_fixture_contribution bug**: CHECK constraint on `contribution_status` is created before the column is added. The `add_column` call must precede the `create_check_constraint` call.
- **Frontend router missing import**: `PluginsSettingsView` is used as a route component in `src/router/index.ts` but was missing from the top-level imports. Always verify named imports match the import list.
- **Seed org/user (fixed)**: On startup, `main.py` now runs Alembic migrations, creates a default organisation if none exists, and seeds users from `MODULO_USERS`. Plaintext passwords in `MODULO_USERS` (e.g. `admin:admin`) are auto-hashed with bcrypt. Set `MODULO_DEMO_MODE=true` to also create a `demo:demo` read-only user.
- **Backend Settings reads `.env`**: The `.env` file must be in `backend/`, not `Product/`. The `Settings` model uses `env_file=".env"` relative to the cwd.

### Pre-merge smoke test (MANDATORY)

Before merging any worktree branch to `main`, run the smoke test:

```powershell
../devtools/harness/tools/smoke-test.ps1          # full check (file existence + vitest + Playwright @smoke + type-check)
../devtools/harness/tools/smoke-test.ps1 -Fast    # skip type-check
```

This checks:
1. **All route component files exist** on disk — catches missing `.vue` files that the router imports
2. **Vitest smoke tests pass** (`app-bootstrap.spec.ts` imports the router module and checks every import resolves)
3. **Playwright @smoke E2E tests** — runs 5 critical tests (login error, login redirect, dashboard auth guard, sidebar, bootstrap) via `--grep "@smoke"`
4. **Vue type-check** (`vue-tsc --noEmit` catches type errors)

The `@smoke` tag is set per-test via `{ tag: '@smoke' }` in `frontend/tests/e2e/`. Add it to any critical test that should gate merges. Run just the smoke subset with `pnpm run test:e2e:smoke`.

The history of this rule: `SchemaBuilderView.vue` existed as an untracked file, was deleted during cleanup, and the router still imported it — causing a 500 on every page load. The smoke test would have caught it.

### OpenAPI type generation

`pnpm run dev` auto-generates TypeScript types from the backend's OpenAPI spec (`http://localhost:8000/openapi.json` → `frontend/src/lib/api/schema.d.ts`). The backend must be running for this to work.

To generate types manually without starting the dev server:

```powershell
pnpm run generate:api
```

#### Using the typed client

Two API access patterns coexist:

**1. `useApi` composable** (legacy, 46 existing files — no migration needed)
```typescript
import { useApi } from '../composables/useApi'
const { get, post } = useApi()
const data = await get<SomeType>('/api/v1/me')
```
Throws on error. Same API surface, works unchanged.

**2. `api` typed client** (NEW — preferred for new code)
```typescript
import { api } from '../lib/api/client'
import type { paths, components } from '../lib/api/client'

// Fully typed — path, body, query, and response are all inferred
const { data, error } = await api.GET('/api/v1/me')
if (data) console.log(data.display_name)

// POST with typed body
const { data: created } = await api.POST('/api/v1/pipelines', {
  body: { name: 'my-pipeline', description: '...' }
})

// Access any schema type directly
type UserPrefs = components['schemas']['SettingsResponse']
type Pipeline = components['schemas']['PipelineResponse']
```

Returns `{ data, error }` — no throw. Guard with `if (data)` or `if (error)`.

#### Commit policy

`schema.d.ts` is committed to the repo so CI/type-checking works without a running backend. When the backend API changes, regenerate:

```powershell
# Backend must be running on :8000
pnpm run generate:api      # one-shot
pnpm run dev               # auto-generates on start
```

After regenerating, verify the frontend still compiles with `pnpm run build`.

---

## Schema Generation

The frontend TypeScript types in `frontend/src/lib/api/schema.ts` are auto-generated from
the backend FastAPI OpenAPI schema. When backend API contracts change (new/modified routes,
request/response models), the schema must be regenerated:

```powershell
cd frontend
pnpm run generate:api
```

This runs `scripts/generate-api-types.ps1` which imports the backend, dumps the OpenAPI
schema as JSON, and feeds it to `openapi-typescript` to produce the typed client.

There is no pre-commit hook for this — the pre-commit framework runs `generate-api-types` as a manual-stage hook only (`gate.ps1` Phase 1d). You must regenerate manually or run `pre-commit run generate-api-types` when the backend API changes. If CI fails because `schema.ts` is out of date, run `pnpm run generate:api`, commit the updated file, and retry.

---

### Local frontend dev (fastest loop)

Start the frontend-only dev server that proxies API calls to app.modulo.run.
No local backend, DB, or Docker needed — just the frontend source code.

**Caveat — backend changes:** The local-frontend proxies `/api` and `/ws` to
`https://app.modulo.run` (production). Backend code changes (Python, DB
migrations, API routes, Pydantic models) are NOT picked up by this loop —
the proxy hits the deployed backend, not your local code.

Two options when your change touches the backend:
1. **Deploy to app.modulo.run** — merge to `main`, then run `/deploy` (canary rollout through staging). Fastest if you're confident.
2. **Run full local stack** — `docker compose -f docker-compose.local.yml up -d` (Postgres + Redis), then start the backend locally (`uv run uvicorn modulo.api.main:app --reload --port 8000`), and point Vite at it (`VITE_API_URL=http://localhost:8000`).

Rule of thumb: if you're only changing frontend code (`.vue`, `.ts`, CSS),
use the local-frontend loop. If you're changing backend code, deploy to
app.modulo.run unless you need iterative backend debugging (then use Docker).

```powershell
# From Product/frontend/
$env:VITE_API_URL = "https://app.modulo.run"
Start-Process -WindowStyle Hidden -FilePath "C:\nvm4w\nodejs\node.exe" -ArgumentList "node_modules\vite\bin\vite.js --port 5174 --host 0.0.0.0"
```

Access at `http://local-frontend.modulo.run:5174` (add hosts entry first — see root AGENTS.md).

**IMPORTANT:** Node.js is at `C:\nvm4w\nodejs\node.exe` (not `node` in PATH on Windows).
Use the full path in `Start-Process` because the background service has a different PATH.
`npx` / `pnpm run dev` don't work for backgrounding — always use `node.exe` with the full path to `vite/bin/vite.js`.

**`vue-i18n` pre-bundling fix:** If the page fails to load with `ReferenceError: init_runtime_dom_esm_bundler is not defined`, Vite's dep optimizer is breaking `vue-i18n`. Add it to `optimizeDeps.exclude` in `vite.config.ts`:

```typescript
optimizeDeps: {
  exclude: ['vue-i18n'],
},
```

Then delete `node_modules/.vite/` and restart Vite.

### Startup scripts (non-blocking)

Use these to launch full-stack services without getting blocked:

```powershell
# Start Postgres + Redis (from Product/)
docker compose -f docker-compose.local.yml up -d

# Start backend (from Product/backend/)
Start-Process -NoNewWindow -FilePath "uv" -ArgumentList "run uvicorn modulo.api.main:app --reload --port 8000"

# Start frontend with --host for network access (from Product/frontend/)
Start-Process -NoNewWindow -FilePath "cmd.exe" -ArgumentList "/c pnpm run dev -- --host 0.0.0.0"

# Check health
Wait-Process -Name "uv" -ErrorAction SilentlyContinue  # doesn't block; just confirms launched
```

### Frontend smoke tests

| Test | File/Command | What it catches |
|---|---|---|
| Unit | `src/__tests__/app-bootstrap.spec.ts` | Missing route component files, module-level import errors |
| Playwright @smoke | `--grep "@smoke"` across all `tests/e2e/` | Login, auth, navigation, golden path — critical browser flows |
| Route file check | Part of `smoke-test.ps1` | Every `.vue` imported by the router exists on disk |

---

## What Agents Must NOT Do

- `yaml.load()` → use `yaml.safe_load()`
- `jinja2.Environment()` → use `jinja2.sandbox.SandboxedEnvironment()`
- Decrypted credentials in LangGraph state, logs, or OTel spans
- `SET app.organisation_id` without `LOCAL` inside a transaction
- `import psycopg2` or `import sqlite3` in async code
- `page.waitForTimeout()` in Playwright → use `waitForSelector('[data-loading="false"]')`
- Import LangGraph from `modulo.api` directly → go through `modulo.core.pipeline_engine`
- Import `modulo_cloud` from anywhere in core
- `outline: none` on interactive elements without `--focus-ring` replacement
- Dynamic TypedDicts for LangGraph state → use `dict[str, Any]`
- Commit `.env` files or any file containing secrets
- Implement admin API keys — only `operator` and `runner` roles
- Treat a task as "blocked" because it needs both frontend + backend changes — fix both sides in the same session. The worktree + subagent workflow supports cross-cutting fixes. Agents are expected to be comfortable fixing Python and TypeScript/Vue in the same task.

## Lessons Learned

### Reserved LogRecord keys in `logging.extra={...}` crash at INFO level

Keys like `name`, `msg`, `args`, `exc_info`, `filename`, `module`,
`funcName`, `created`, `msecs`, `relativeCreated`, `thread`, `process`,
`levelname`, `lineno`, `pathname`, `stack_info`, `taskName`, `asctime`,
`message` are reserved attributes on `logging.LogRecord`. Passing one in
`extra={...}` raises `KeyError: Attempt to overwrite 'name'` the moment the
record is actually built. At WARNING (the pytest default) the INFO path is
never exercised, so unit tests stay green while production (INFO) crashes —
FAR-113: `_log.info("cost_components.seeded", extra={"name": ...})` silently
skipped every org's seed for weeks, yielding $0 runs. Enforced by the
`.semgrep/logging_reserved_extra_key.yml` rule.

Rules:
1. Use a non-reserved key in `extra=` (e.g. `component_name`, `schema_name`).
2. Unit tests that assert log records must `caplog.set_level(logging.INFO,
   logger="<module>")` so the production INFO path actually executes — WARNING
   masks INFO-path crashes.
3. Boot-time seeds emit `[boot] seed <name>: ok/FAILED` to stdout via
   `_boot_seed` in `main.py` — the structured JsonFormatter logger lines do
   not render in `fly logs`, so stdout is the only reliable boot signal.

### Multi-PR delivery: merge the first PR manually, then branch the next PR off main

When delivering a feature as multiple PRs (e.g. PR A -> PR B -> PR C), once PR N is green on CI and APPROVED by the reviewer, MERGE IT MANUALLY (`gh pr merge --squash`) rather than waiting for the merge queue. Then branch PR N+1 off the updated main. Waiting for the merge queue on a fast-moving main means the next PR's branch base goes stale while it waits, forcing repeated rebases. Manual merge after approval is the single biggest accelerator for multi-PR delivery. Only wait for the merge queue when you are NOT chaining PRs (single independent PR).

### Once a PR exists, stop chasing main - merge in only when required, never rebase

Once a PR branch exists, do NOT chase main — merge main in with a merge commit (`git merge origin/main --no-edit`) only when actually required (merge-queue conflict, rejected push, or a chained PR that needs latest main). NEVER rebase the branch onto main and NEVER rewrite its history with force-push while the branch is shared or being reviewed. Rationale: (a) rebase re-applies the whole diff and re-conflicts on every main movement; (b) merge composes with concurrent agents (Branch Fixer, other pipelines) instead of force-push-warring with them; (c) git records conflict resolutions in the merge commit so they don't re-conflict. If a push is rejected, fetch + merge + push again - do NOT force-push over a branch another agent may be touching. Before the first push, rebase against main ONCE — the branch is exclusively yours at that point (see the 'Rebase against main ONCE' lesson below).

### Rebase against main ONCE before first push — don't chase a fast-moving main

Main moves fast (25+ commits in a couple of hours is normal). Guidance that says "keep your branch current with main by merging main in" becomes a treadmill on that cadence — a merge every hour forever, and CI runs the merge ref anyway, so the final state is always validated against latest main regardless.

The rule:

1. **Before the first push** (branch is exclusively yours — never left the machine): `git fetch origin main` + `git rebase origin/main` **once**. Clean linear history, no force-push risk, one conflict-resolution session. After the rebase, run the FULL test package of every module you touched — main may have added tests while you worked, and those are the files most likely to expose your breaking change.
2. **After the push / once a PR exists**: **stop chasing main.** Don't merge main in on a schedule. Merge main in with a merge commit (`git merge origin/main --no-edit`) *only when actually required* — merge-queue conflict, rejected push, or a chained PR that needs latest main.
3. **Never** rebase or force-push a branch that is shared or being reviewed — merge commits compose with concurrent agents (Branch Fixer, other pipelines); rebases force-push-war with them.

Post-mortem (FAR-102, 2026-08-07): the analytics PR branched before main added `test_analytics_facts.py` (PR #815). CI runs the merge ref, so main's newest test ran against the branch — and failed — in a file none of the four sprint agents had ever seen. A pre-push rebase would have pulled that test into the branch so local runs caught it; the full-package corollary is what converts "CI surprise" into "caught locally".

### Use Python for file writes, not PowerShell string ops

Never edit source/text files with PowerShell string replacement (Get-Content -Raw + [System.IO.File]::WriteAllText or .Replace()). PowerShell's encoding handling corrupts UTF-8 (em-dashes, arrows, non-ASCII) into mojibake, producing lint errors and broken files. Use a Python script with io.open(path, 'r', encoding='utf-8') and io.open(path, 'w', encoding='utf-8', newline='\n') for every file write.

### Merge queue re-verifies review state at merge time - never trust the collection step alone

The merge queue (`.github/workflows/merge-queue.yml`) checks for APPROVED reviews in its "Collect open PRs" step, but between collection and the actual `gh pr merge` (which runs 15-30 minutes later, after squash-queueing and full backend/frontend CI), a reviewer can post CHANGES_REQUESTED - and the merge step never re-checked. This TOCTOU race caused PR #535 (a security fix) to merge despite a CHANGES_REQUESTED review posted minutes earlier; the buggy code reached main and needed a follow-up fix PR (#542).

The guard (added in #546): the workflow re-queries `reviewDecision` via `gh pr view` at TWO points - (1) before squash-merging each PR into the merge-queue branch, and (2) immediately before `gh pr merge --squash`. Any PR whose `reviewDecision` is not APPROVED at merge time is skipped and stays open for the next queue cycle.

Rules:
1. Any workflow that merges PRs must re-verify the review decision immediately before the actual merge, never rely on a check done at collection time.
2. When a security review lands with CHANGES_REQUESTED and the merge already happened, fix the finding in a follow-up PR immediately - do not leave the vulnerability on main.
3. The reviewbot does not pin reviews to a commit SHA; a CHANGES_REQUESTED posted mid-flight applies to the whole PR regardless of when the APPROVED was given.

### Branch-fixer / opencode coder agent

- **opencode auth step runs before fetch-ci** → `Configure opencode auth` references `steps.fetch-ci.outputs.ci_failures` but must run AFTER `Fetch CI failures`. GitHub Actions evaluates `if:` conditions at step execution time, and the referenced step's outputs are empty/false if it hasn't run yet. Always verify step ordering when a step's condition depends on another step's output.

- **opencode version must be modern** → Installing opencode from GitHub releases (v0.0.55 from June 2025) gives an ancient version that doesn't support the coder agent's file-editing tools. Always install from npm: `npm install -g opencode-ai` (current: v1.18.4). The `opencode run --agent coder` command requires a version that supports tool-using agents.

- **Use `repository_dispatch` instead of `workflow_dispatch` for triggering workflows** → GitHub has a known caching bug where `workflow_dispatch` triggers are not recognized for recently-modified workflow files, returning HTTP 422 "Workflow does not have 'workflow_dispatch' trigger". Use `repository_dispatch` via `gh api repos/.../dispatches` which is not affected by this bug. Both the sender (CI) and receiver (branch-fixer) need to support it.

- **`gh run view --log-failed` returns only the last failed step's output** → For CI workflows that run scripts (coverage thresholds, post-processing) after the actual tests, `--log-failed` returns the post-processing step's output — not the test failures. Use `gh run view --log | grep "FAILED"` to extract actual test failure lines.

- **GitHub Actions step ordering: `Configure opencode auth` must precede `Run opencode fix`** → The auth step writes the API key to `~/.local/share/opencode/auth.json`. Without it, opencode runs without credentials and cannot call the LLM, so it produces no file edits. Both steps need identical `if:` conditions referencing `steps.fetch-ci.outputs.ci_failures`.

### The opencode `agent`-node HTTP provider is unreliable — use `sandbox_agent` + the CLI

The `opencode` model provider is an OpenAI-compatible backend that talks to the
external zen HTTP gateway at `https://opencode.ai/zen/go/v1` (see
`backend/src/modulo/model_backends/opencode/` and the hub's
`_OPENAI_COMPATIBLE_BACKENDS` entry). It is an external dependency, not a
first-party endpoint:

- **The gateway's `/chat/completions` path can fail upstream while `/models` works.** Verified 2026-08-11: `/models` returned 200 with the same key, but every completion request returned HTTP 500. When an `agent` node uses the `opencode` provider, the run failed with a misleading `error_code` (`AuthenticationError` or the raw openai error type) even though the key was valid — the gateway was down, not the credentials.
- **The reliable path for opencode work is `sandbox_agent` + the opencode CLI** (`opencode run --model opencode-go/deepseek-v4-flash`, key via `OPENCODE_API_KEY` / `~/.local/share/opencode/auth.json`), which is what all production pipelines (Branch Fixer, PR Reviewer, improve-*) use. It does not depend on the zen gateway. Prefer it for any opencode-typed work.
- **Classification fix (2026-08-11):** `OpenAICompatibleBackend.invoke()`/`stream()` now re-raise upstream HTTP 5xx and connection failures as `ProviderUnavailableError` (run `error_code` = `ProviderUnavailableError`), while genuine 4xx auth errors still surface as `openai.AuthenticationError`. A gateway outage is now distinguishable from a bad key. The hub still builds the plain `OpenAICompatibleBackend` for `opencode`; `OpenCodeBackend` is a drop-in subclass that pins the zen base URL.

#
## Pre-commit hooks (appended from root AGENTS.md)

`pre-commit` (4.6.0) is installed as a global uv tool and configured in
`Repos/modulo/.pre-commit-config.yaml`. The framework manages the hook at
`Repos/modulo/.git/hooks/pre-commit` and runs every commit.

**Hooks are split into two stages:**

| Runs on every commit (default) | Runs in gate.ps1 only (`stages: [manual]`) |
|---|---|
| `ruff --fix` (staged .py) | `mypy --strict src/` |
| `ruff-format` (staged .py) | `vue-tsc type-check` |
| `bandit -r backend/src/` | `pip-audit` (deps scan) |
| `semgrep --config=.semgrep/` | `generate-api-types` |
| `gitleaks` (secret scan) | |
| `import-linter` | |
| `eslint` (staged .vue/.ts) | |
| `check-migration-heads` (if migrations staged) | |
| `graph-validate` (if product-map changed) | |
| `pre-commit-checks.ps1` (pattern scan) | |
| `check-merge-conflict` | |
| `check-yaml` / `check-toml` / `check-json` | |
| `end-of-file-fixer` | |
| `trailing-whitespace` | |
| `no-commit-to-branch main` | |

The split keeps per-commit hooks fast (<5s typical). Heavy hooks (mypy,
vue-tsc, pip-audit) run when you gate via `gate.ps1`, which calls
`pre-commit run --all-files --hook-stage manual` as Phase 1d.

### Platform-agnostic hooks (Windows + Linux)

The `eslint`, `vue-tsc`, and `semgrep` hooks are platform-agnostic wrappers in `scripts/`:

- `scripts/run_frontend_npm.py` runs `pnpm run <script>` in `frontend/` (npm
  fallback only) via the platform's package manager (`pnpm.cmd`/`npm.cmd` on
  Windows, `pnpm`/`npm` on Linux). Replaces the old
  `bash -c 'cd frontend && pnpm run ...'` entries, which broke on Windows where
  `bash` resolves to WSL and cannot execute Windows-installed `node_modules`.
- `scripts/run_semgrep.py` skips the incremental semgrep scan on Windows
  (semgrep-core cannot complete the full `backend/src/` scan with
  `--baseline-commit` on Windows — it hangs). Semgrep remains enforced on
  Linux: CI (`ci.yml`, `deploy.yml`) and E2B sandbox commits.

Windows note: when `ruff-format`, `mixed-line-ending`, or `end-of-file-fixer`
report "files were modified by this hook", they have normalised a Windows-tool
CRLF file back to LF (the committed blob is LF via `.gitattributes`). `git add`
the fixed files and re-commit — that is the fixer loop working as designed.

Migration collision check (`check-migration-heads.ps1`) runs both in
pre-commit (when migration files staged) and in gate.ps1 Phase 0 (even
with `-SkipTests`). If blocked: renumber your migration to the next free
sequential number and fix its `down_revision` to point at the current head.

### Rebasing: only when another branch merged first — and how to resolve conflicts

Note: this applies to the LOCAL gate.ps1 merge path and to already-pushed branches. For a branch's FIRST push in PR-based delivery, rebase against main ONCE instead (see the 'Rebase against main ONCE' lesson).

**PREFERRED: rebase against main ONCE before the first push (`git fetch origin main && git rebase origin/main` — the branch is exclusively yours). After the PR exists, only merge main in with a merge commit when actually required (merge-queue conflict, rejected push, chained PR); never rebase or force-push a shared/reviewed branch.**

In general, **no pre-merge is needed** — the worktree branch is based on
main and the PR flow handles merging. If another PR merged first (changing
shared files), merge main in with a merge commit instead of rebasing.

If the rebase produces conflicts, resolve them inline:

1. Read all three versions: base, main (ours), worktree (theirs)
2. Understand the intent of each side's change
3. Produce a merged version that satisfies both intents — never silently
   discard either side
4. `git add` the resolved file and `git rebase --continue`

**Do not rely on `-X theirs` or `-X ours` strategy flags.** These silently
drop one side's changes. Inspect each conflict and produce a correct merge
of both intents.

After a successful rebase (all conflicts resolved), push and create a PR:
```powershell
..\..\..\..\devtools\harness\tools\create-pr.ps1 -Branch <worktree-branch> -TaskId <id>
..\..\..\..\devtools\harness\tools\wait-for-pr.ps1 -PRNumber <N> -WaitForCI
```

### Test suites

**Backend** — from `Repos/modulo/backend/`:
```
pytest tests/unit/ --tb=short -q --timeout=120
```
The backend suite takes ~35-40 min (14700+ tests). Frontend — from `Repos/modulo/frontend/`:
```
pnpm run test:unit
```
(478 tests, ~4 min). Both must pass before reporting "tests pass" or proceeding with any merge.

The full inventory of suites and their root paths lives in `docs/definition-of-done.md` §1.
Agents run targeted files only (see §2 impact consideration) — never the full 35-40 min suite in a worktree.

### Frontend worktrees and node_modules (pnpm)

`git worktree add` creates a new working tree with no installed dependencies. The frontend uses pnpm, so a worktree gets its OWN real node_modules — no junctions, no shared physical tree. To provision:

```powershell
Set-Location <worktree>\frontend
pnpm install --frozen-lockfile
```

pnpm hard-links package contents from its global content store, so a warm install is ~2s and needs no re-download. The global store lives outside the repo (default `%LOCALAPPDATA%\pnpm\store` on Windows, `~/.local/share/pnpm/store` elsewhere) — worktrees share the store, never a mutable node_modules tree. This is why the OLD junction fast-path (mklink /J main's node_modules into worktrees) was removed: PowerShell's `Remove-Item -Recurse` follows NTFS junctions and deletes the target's contents (PowerShell/PowerShell#26913), and any install through a junction corrupted the one tree everyone read.

Rules:
1. **Every worktree provisions its own node_modules with `pnpm install --frozen-lockfile`** — never junction to main's tree, never copy main's node_modules.
2. **Installing through a junction is forbidden** (the junction no longer exists; if you see a leftover junction in an old worktree, remove it: `cmd /c rmdir "<worktree>\frontend\node_modules"`).
3. If `pnpm install --frozen-lockfile` fails, main's lockfile may have drifted — fix deps on MAIN (`pnpm install` in `Repos/modulo/frontend`, commit the lockfile) and re-run in the worktree.
4. Pre-commit hooks (eslint via `pnpm run lint`, etc.) work in a worktree once its own node_modules is installed.
5. If a worktree's node_modules is corrupt/missing, delete it and re-run `pnpm install --frozen-lockfile` — it is an isolated tree; nothing else is affected.

**Gotcha:** `pre-commit-checks.ps1` (harness Check 5) flags pre-existing
admin-view gaps whenever an `Admin*.vue` file is touched — every
`frontend/src/views/Admin*.vue` must contain a `<FeatureGate>` wrapper. If your
change touches an admin view that lacks one (e.g. `AdminViewsView.vue` until
FAR-117), the commit is blocked — add the wrapper with the correct feature name
(match sibling views) rather than bypassing the hook.

### Systemic patterns: apply as bulk sweeps, not per-feature QA

When a pattern appears in multiple QA findings across different areas, stop
iterating per-feature and write a systemic sweep instead. Run it as a Worker
sub-agent in a single worktree branch, apply the pattern to all matching files,
and merge once. This is faster, more complete, and cheaper in CI.

### Test rot: fix it once across all files

When a recurring test-rot pattern is identified (e.g. `created_by` ? `account_id`,
`MagicMock` ? `AsyncMock`), fix it once across all test files as a standalone
sweep rather than discovering it N times.

### Parallel Workers and overlapping files

When using `/distribute` with parallel Workers, check if any two groups'
file footprints overlap. If they do, merge the groups or accept that the
Conductor will resolve the conflict. When resolving, never silently discard
either side's changes.
### Deployment: health check `finally` block `conn.close()` can override inner `return`

In `_check_checkpointer()`, the inner `try/except` catches query failures and returns "degraded". But the `finally` block runs `conn.close()` before the return completes. If `conn.close()` raises, the exception propagates to the outer `except Exception`, overrides the "degraded" result, and produces "unavailable" with empty detail — even though the query failure was the real issue.

Fix: wrap `conn.close()` in a nested `try/except` so a close() failure can never override the inner result.

### Deployment: any unavailability blocks bluegreen — return "degraded" for non-critical checks

Fly.io's bluegreen strategy waits for ALL health checks to return non-"unavailable" before cutting over. A single non-critical check (like checkpointer tables missing) returning "unavailable" blocks the entire deployment. Change any check that the app can function without to return "degraded" instead of "unavailable".

### Frontend i18n: vue-i18n message compiler cannot parse JS ternary expressions in translation values

`{count === 1 ? '' : 's'}` inside a translation value is parsed by `@intlify/message-compiler` as a malformed interpolation expression, causing build failures with error code 7. Never use JS expressions inside translation strings. Use vue-i18n pluralization syntax (`"key | key_plural"`) or simplify the message.

### Ops: pnpm lockfiles are platform-agnostic — no --force needed

pnpm records os/cpu per package in pnpm-lock.yaml, so a lockfile generated on Windows installs correctly on Linux (Docker/CI). The old npm EBADPLATFORM failure (Windows lockfile + Linux Docker) and its `npm ci --force` workaround do not apply. Never pass `--force` to `pnpm install` to work around platform errors — it is not the same flag and masks real issues. If a Docker build fails to resolve a platform-specific optional dep, the lockfile or the package's os/cpu metadata is the problem, not the generating OS.

### Database / Multi-backend

- `GenericRepository.set_org_context` no-op (`pass`) on non-Postgres backends → must call `set_rls_org(session, org_id)` so that `session.info` is populated for the `do_orm_execute` tenant-filter listener
- `_inject_tenant_filter` breaking after first entity in JOIN queries → iterate ALL entities with `organisation_id`, not just the first match
- `column_descriptions` not available on ORM `UPDATE`/`DELETE` statements → use `execute_state.all_mapper_classes` to extract entities for tenant filtering on DML
- `func.now()` with `DateTime(timezone=True)` on SQLite → use `func.current_timestamp()` instead (SQLite's `func.now()` returns naive datetime)
- Backend type strings differ across sources: `dialect.name` returns `"postgresql"` but settings key returns `"postgres"` → always normalize with `.lower()` and compare against the settings canonical form; document the two sources

### Locking

- `pg_advisory_lock` with `asyncio.wait_for` creates a race between server-side lock acquisition and client timeout → use `pg_try_advisory_lock` in a polling loop for timeout-based acquisition
- `asyncio.Lock` timeout via `wait_for` can trigger a caller's `finally` block that calls `release()` on another task's lock → always track lock ownership (e.g. by task ID) and guard `release_lock` with an ownership check
- In-memory locks (`GenericLock`) must use module-level (shared) state so multiple `RepositoryHub` instances coordinate on the same lock namespace

### Frontend / Layout

- Every list/table page must have an empty-state message when data is empty — never leave a blank content area. Use the existing pattern: a centered card with title + description.
- Enterprise-gated pages (`FeatureGate` component) must never render infinite spinners — hide the sidebar link entirely on Free tier, or show a clear upgrade CTA with a link to `/settings/license`. A locked overlay with a permanent spinner beneath it is worse than showing nothing.

### Frontend / API & Errors

- API failures must not trigger full-page redirects — show an in-page `ErrorAlert` with retry button instead. This is especially critical for the feature-flags API called by `planStore.fetchPlan()`, which runs on every page mount.
- The 401 interceptor in `client.ts` does a hard `window.location.href = '/login'` — ensure the auth token is still valid before the interceptor fires. A single expired-token or failed feature-flags call can cascade into an unusable redirect loop.

### Frontend / Security

- Runtime Config values matching sensitive key patterns (`SECRET|PASSWORD|TOKEN|KEY|DATABASE_URL|ENCRYPTION|SIGNING|PRIVATE`, case-insensitive) must be masked by default with `"********"` and a per-key "Reveal" toggle — never displayed in plaintext. Non-sensitive keys (e.g. `APP_NAME`, `LOG_LEVEL`) display normally.
- Sidebar nav links for Enterprise-only features should be conditionally rendered based on the plan tier, not just visually dimmed — a visible-but-broken link is worse than no link.

### Frontend / Layout (continued)

- Mobile dropdown menus (`v-if="mobileOpen"`) inside a `flex` (row-direction) container get laid out as skinny horizontal columns instead of full-width panels below the header. Always position mobile dropdowns with `fixed top-14 left-0 right-0 z-40` to take them out of flex flow — never rely on the natural document flow inside a horizontal flex container for overlay-style elements.
- `pt-14` (56px) is a fragile approximation of a fixed header's height. The header's actual height varies with padding (`py-3` = 24px vertical), content (20px SVG), and border (1px) — real height is ~63px. Use `sticky` positioning for the mobile header instead of `fixed` + `pt-14`, or measure the actual height precisely. The gap between `pt-14` and true header height causes content to peek behind or leave a visible strip.
- The mobile layout has oscillated between `fixed` header + `pt-14` and in-flow/`sticky` header approaches multiple times (commits e3028c2, 8f36188, a080bfa, 393605d). Neither approach is inherently better — the choice depends on whether the dropdown/menu panel needs to push content down or overlay it. **Decide upfront:** overlay (fixed header, z-index stacking) vs. push (sticky header, content reflow). Don't flip-flop.
- When both the mobile menu panel and main content need scrolling, avoid nesting `overflow-auto`/`overflow-hidden` on multiple flex layers — it creates scroll-snapping issues where one layer traps scroll. Use a single scroll container (`overflow-y-auto` on `main`) and let the menu panel scroll within itself if needed.
- Commit `393605d` gutted the mobile layout (removed LogoMark, dynamic nav items with sections/icons, theme toggle, user profile, logout button) to work around a Vite 8 SFC parsing issue. If functionality needs to be restored, build it back incrementally rather than doing another full rewrite — the layout fundamentals are now stable.
- Remy panel default position (`window.innerWidth - 460`) must be clamped to `Math.max(8, ...)` to prevent off-screen rendering on viewports < 460px wide. Default size (`440×600`) must also be clamped to `Math.min(440, window.innerWidth - 16)` and `Math.min(600, window.innerHeight - 120)` so the panel fits mobile viewports. **Always clamp absolute-positioned UI defaults against viewport dimensions.**

### Product Map / improve-architecture

- When running `improve-architecture` on a feature entry, always check that `bdd:` and `unit-tests:` frontmatter fields are populated — **especially `bdd:`** which is commonly missing even when feature files exist.
- **Frontmatter YAML**: Never set `delivery-tasks: []` (flow empty list) on a line followed by orphaned indented list items. The `[]` terminates the value; subsequent `- item` lines become parse errors. Either keep `[]` empty with nothing after it, or use a block list without `[]` and add the proper parent key (e.g. `bdd:`).
- **`bdd:` field**: Every connector product map entry must have a `bdd:` field listing its BDD feature files. File paths like `backend/tests/bdd/features/connectors/foo.feature` belong in `bdd:`, never in `delivery-tasks:` (which holds delivery-plan task IDs, not file paths).
- **`depends-on` field**: Every connector file must declare `feat-connectors-hub` as a dependency. Features that use the connector hub's sampling/query interface (e.g. schema-inference) must also declare it. `graph-validate.ps1` will flag missing `depends-on` as orphaned refs.
- **`delivery-tasks` contains task IDs, not file paths**: The `delivery-tasks` field links to delivery-plan task IDs (e.g. `task-connector-hub-01`), not file paths. File paths for BDD features, unit tests, and code paths belong in their respective frontmatter fields (`bdd:`, `unit-tests:`, `code:`).
- **Known Gaps with `[x]` (checked) items**: The "Known Gaps" section must only list genuine gaps — items that are missing or incomplete. Do not list accomplished items (BDD scenarios that exist, unit tests that exist, working features) with `[x]` checkboxes in Known Gaps. Those belong in the Behaviours section or as plain prose notes.
- **`status: gap` vs `status: partial`**: If a feature has zero implemented behaviours (all `[ ]`), no tests, and "No implementation exists" in Known Gaps, use `status: gap`, not `status: partial`. `partial` implies some work is done.
- **Boilerplate deduplication**: Cross-cutting concerns that apply identically to all connectors (Credential Lifetime lifecycle, capability-based graph validation, token rotation, rate-limit handling, ConnectorHub pre-run health check) should be documented in `connector-hub.md` only, not duplicated across every individual connector file. Individual files should only document connector-specific behaviour.
- The HTTPBearer FastAPI dependency with `auto_error=False` returns `None` for missing credentials (not 403). The handler must raise 401 explicitly. Product map entries commonly claim 403 for missing bearer — verify against the actual code.
- SCIM CRUD functions (`scim_create_group`, `scim_create_user`) call Team/Account CRUD directly, bypassing REST API validation. Any validation gap in the underlying CRUD (e.g. duplicate name enforcement) is inherited by SCIM. Document this cross-module concern in the product map entry's Known Gaps.
- When auditing behaviours in the product map, do NOT assume an unchecked `[ ]` means "not implemented" — it often means "not verified." Read the test files and run `grep` for each behaviour before deciding status.
- The `prd:` frontmatter field must contain only the bare section number (e.g. `8.17`), never wrapped in quotes or prefixed with `§`. The `§` prefix or quotes cause `graph-validate.ps1` to fail section matching because `TrimStart('§')` cannot strip quotes, leaving a `"8.17"` string that doesn't match the PRD index.
- **`graph-validate.ps1 -Fix` can corrupt `_index.md` with literal `\n` strings.** The `-Fix` flag's PowerShell string replacement can produce raw `\n` (backslash-n) text instead of actual newlines. If `_index.md` shows visible `\n` in the rendered file, re-run `graph-validate.ps1` without `-Fix` to regenerate a clean index. File a bug against `deploy/harness/tools/graph-validate.ps1` if `-Fix` remains broken.

### Backend / API Schema Migrations

- When a DB column is renamed in a migration (e.g. `created_by` → `account_id`), every Pydantic response schema that references the old name must use `Field(validation_alias="account_id")` and add `"populate_by_name": True` to `model_config`. Without this, `model_validate(pipeline)` fails because the ORM model's attribute is `account_id` but the response schema expects `created_by`.
- After a column rename migration, grep ALL response schemas in `backend/src/modulo/api/routes/` for the old column name — it's common to miss several files.

### Backend / Models

- When a new column is added to a table via deployment schema patch (e.g. `ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS default_autonomy_level`), the SQLAlchemy ORM model MUST have the corresponding `mapped_column`. Without it, any CRUD function that passes the field to the model constructor raises `TypeError: 'default_autonomy_level' is an invalid keyword argument for Pipeline`.
- Keep `__table_args__` check constraints and ORM mapped columns in sync: if a check constraint references a column, the ORM must map it.
- For optional Pydantic fields in graph/JSON schemas (`PipelineGraphNode.agent_id`, etc.), always add `= None` default — otherwise dicts that omit the key entirely fail `model_validate` with `Field required`.

### Ops / Deploy

- `flyctl deploy` direct invocation without `--build-arg` flags leaves all git metadata fields (`git_sha`, `git_branch`, `git_commit_message`, `git_commit_timestamp`, `build_timestamp`) empty in the `/api/v1/deployment` endpoint. Always use `deploy.ps1` (which reads and passes build args automatically) instead of calling `flyctl deploy` directly.
- The `/deployments` page on modulo.run reads this endpoint. If git metadata is missing, the page shows blank fields — not a backend model change.
- `fly.toml` Python version hardcodes (e.g. `python3.12` in SSH commands) must match the project's actual Python version in `.python-version` and `pyproject.toml` `requires-python`. A mismatch causes the SSH command to fail silently. Search `fly.toml` for all hardcoded version strings when upgrading Python.

### Frontend / Resilient Rendering

- When displaying API data that may be temporarily empty (e.g. between redeploys), use `{{ value || '—' }}` fallback instead of `v-if="value"` conditional rendering. Empty strings are falsy in JS — `v-if` hides the entire field, making it look like the model changed. Always show the field label with a fallback value.

### Backend / Error Tracking & Observability

- **Redis async calls from sync context: always await the coroutine.** _get_last_fired and _set_last_fired in alert evaluation were defined as sync def but called without wait - the coroutine object was silently discarded, the cooldown never persisted to Redis, and the method returned True (non-None coroutine) so cooldowns appeared perpetually active. Never discard an sync coroutine without wait.
- **Error tracking API endpoints that read from DB must fetch all data inside the `session.begin()` transaction block.** If a query like `get_error_group()` is made inside the transaction (for RLS context) but a subsequent `get_error_events_by_group()` call is made outside it, the second call runs without RLS context and can leak cross-org data or return stale results. Wrap all DB reads/writes in the same `async with session.begin():` block that contains the auth/RLS setup.
- **Error forwarders must isolate failures per-forwarder.** A single forwarder's HTTP failure (network error, bad API key) must not prevent other forwarders from delivering, and must not crash the error ingestion pipeline. Wrap each `forward()` call in `try/except` and log the failure.
- **Alert evaluation cooldown keys should include both rule_id and fingerprint.** Without the fingerprint in the cooldown key, all errors matching a rule share a single cooldown — the first error that fires an alert suppresses alerts for entirely different errors. The key format should be `alert_cooldown:{org_id}:{rule_id}:{fingerprint}`.

### Backend / CLI Tools

- **Click decorators must decorate the command function directly.** Applying `@click.option()` to a wrapper function instead of the actual `@click.command()` function means the CLI never registers the options — the command accepts no arguments at runtime. The decorator chain must be: `@click.command()`, `@click.option(...)`, `def my_command(...)` — stacked in that order on the same function.

### Backend / Caching & Init Ordering

- In-memory caches that store mutable dicts must return a defensive copy (`json.loads(json.dumps(data))` or `copy.deepcopy(data)`) rather than the original reference. Returning the raw reference allows concurrent callers to mutate the cached data, corrupting the cache for subsequent requests within the TTL window.
- Initialization flags (`initialized = true`) must be set AFTER the init logic completes, not before. Setting `initialized = true` before `await loadLocaleMessages()` or `await setLocale()` means a failure in those async calls leaves the app in a half-initialized state where no retry is possible — the flag already blocks re-entry.
- When DB migration errors need to be caught for 501 responses, use `except ProgrammingError` (not `except SQLAlchemyError`). `ProgrammingError` indicates a missing table/column (migration not yet applied), while `SQLAlchemyError` is the base class that also catches `IntegrityError`, `DataError`, etc. — real data errors should surface as 500, not as misleading "Run database migrations" messages.

### Backend / Dashboard & Aggregations

- Idle run count must use tracked-status-only total when computing `idle = total - active - failed`, not `sum(status_counts.values())`. The latter includes non-tracked statuses (complete, cancelled, eval_failed) which silently inflate idle.
- Redis async connections (`Redis.from_url(...)`) must always be wrapped in `try/finally` with `await redis.aclose()` in the finally block. A bare try/except without finally leaks TCP connections on exception.

### Frontend / Internationalization

- `en-US.json` can accumulate non-user-facing artifacts (SVG path data, JS expressions with `??`/`||`, template literals, function calls) from the auto-extraction script. After extraction, verify all JSON values are human-readable text. Remove keys containing `??`, `${`, `||`, function calls, or SVG path data.
- When adding locale sync between frontend and backend, verify the Pinia store's payload shape matches the API model. `PUT /api/v1/me/settings` expects `{ locale: "..." }` at top level (flat), not `{ preferences: { locale: "..." } }` (nested). Misaligned shapes silently fail — the locale is never persisted. Verify both the send direction (`syncToBackend`) and the read direction (`initLocale`) match the backend's `SettingsResponse`/`SettingsUpdate` Pydantic models.

### Frontend / Store & View Patterns

- Do not duplicate computed properties across a Pinia store and a Vue view. Define the computed once in the store and reference it from the view via `storeName.propertyName`.
- Runtime validation of API responses from the app's own backend should be minimal (top-level null/type checks or Zod schema), not 100+ lines of per-field manual type-checking. TypeScript and tests catch shape mismatches at build/test time — full field-level validation is over-engineering for internal endpoints.
- Keep store fetch methods consistent across the same store. Both `fetchSummary` and `fetchTrends` should follow the same error-handling pattern — no `console.warn` in production code, both should set `error.value` on failure.
- Event handler type guards (`if (event.type !== 'run' && event.type !== 'pipeline') return;`) must precede state mutations (`syncingIds.add`). Adding an ID before the type check means unhandled event types permanently block future events with the same ID.
- Inline markup duplicated between desktop and mobile variants (view mode toggles, brand headers) must be extracted to a shared component. If both sidebars render the same UI element, it belongs in a single `.vue` file.

### Backend / Async & Concurrency

- Lazy-init side effects in dual-channel classes (pub/sub, read/write) → each method should only create its own channel. `publish()` must not create subscription connections and `subscribe()` must not create publishing connections. Use the shared `connect()` method for full initialization.

### Backend / BDD Feature Tests

- **Feature file API paths are step-text matching keys, not just documentation.** Changing a path in a `.feature` file (e.g. `/api/pipelines` → `/api/v1/pipelines`) breaks the pytest-bdd step matching unless the corresponding step definition `parsers.parse` pattern is updated in the same change. Fixes must be coordinated between `tests/bdd/features/` and `tests/bdd/steps/` — never change one without the other.
- **Background blocks reduce duplication without changing behavior.** When 6+ scenarios in a feature file start with the same `Given I am authenticated as an admin in org "acme"`, promote it to a `Background:` section. Scenarios needing different auth (e.g. viewer role) override the Background by repeating the same step text inline — pytest-bdd uses the scenario-level step, not the Background's.
- **Avoid module-level imports of `modulo.api.main` in conftest files.** Importing `modulo.api.main` at module level triggers MCP server startup and database connection pooling, which can hang the test suite. Use lazy imports inside the `client` fixture instead.
- **No two test modules should call `scenarios()` for the same .feature file.** This causes duplicate test registration and `StepDefinitionAlreadyRegistered` errors. Each `.feature` file should have exactly one `scenarios()` caller. If steps need to be shared, define them in a helper module that the single scenarios()-owning module imports.
- **Shared step text patterns must be defined only once.** The step `@given(parsers.parse('I am authenticated as an admin in org "{org}"'))` should live in `bdd/conftest.py` and not be duplicated in domain-specific conftests like `features/agents/conftest.py`. pytest-bdd silently uses whichever registration was last, making behavior non-deterministic.
- **`BasicScenario` (Gherkin) status code expectations must match the actual FastAPI route mapping.** Before adding a `Then the response status is NNN` to a `.feature` file, verify the actual route handler's exception-to-status mapping. HITL approve status codes were wrong in 3 scenarios (403→422 for missing token, 403→410 for expired token, 409→403 for wrong user's token). Check the route's `except` blocks, not assumptions.

### Ops / Deploy Workflow

- **Deploy scripts must refuse to deploy with a dirty working tree.** The old auto-stash pattern (`git stash -u -m "auto-stash before deploy"` in `deploy.ps1` and `deploy-all.ps1`) caused stash collisions and data loss when multiple scripts or trap handlers popped stashes out of order. Both scripts now check `git status --porcelain` at the start and exit with an error if the working tree is dirty. The caller is responsible for committing, branching, or stashing before deploying.

- **Use `deploy.ps1` or `deploy-all.ps1` — never `fly deploy` directly.** The scripts pass `--build-arg GIT_SHA`, `GIT_BRANCH`, `GIT_COMMIT_TIMESTAMP`, `GIT_COMMIT_MESSAGE`, and `BUILD_TIMESTAMP` automatically. Direct `fly deploy` invocations leave all metadata fields blank in `/api/v1/deployment`, causing the deployments page to show `?` for commit, branch, message, and dates. The branch guard (`main` or `deploy/*`) also prevents accidental deploys from worktree branches.

- **Lost git stashes can be recovered via `git reflog` + `git stash store`.** The `git stash drop` command (or a trap handler that pops the wrong stash) only removes the `refs/stash` reference — the commit object remains in `.git/objects/` until garbage collection. Find the stash commit SHA via `git reflog --all | Select-String "stash"`, verify with `git cat-file -t <SHA>`, then restore with `git stash store <SHA>`. The stash will reappear at `stash@{0}`.

- **Wrap every lifespan seed/init call in try/except — no single boot-time failure should block the app from starting.** The FastAPI lifespan runs migrations, seeds default data, and initialises the checkpointer. Any of these can crash from transient DB issues (SSL param changes, connection timeouts, schema drift from parallel branch merges). A single failed seed function in the lifespan crashes uvicorn at startup, which makes bluegreen deployments fail health checks and keeps stale machines running. Each call that isn't a hard prerequisite for the app to function (user seeds, demo data, environment profiles, checkpointer init, SSO providers, runtime config store) must be wrapped in `try/except` with `exc_info=True` logging so it's debuggable without blocking the deploy. Found during the ADR 001 staging deploy where `_seed_environment_profiles()` crashed from a DATABASE_URL SSL param issue.

- **Before deploying, run `pnpm run build` locally to catch frontend build errors early.** The Docker build lacks interactivity and hides errors behind 10-minute retries. Common issues caught: Rolldown parser errors from Vue template syntax, missing dependencies imported but not in `package.json`, duplicate manifest.yaml keys from parallel distributed work. The local frontend build may fail due to a corrupted `lightningcss.win32-x64-msvc.node` binary (native module, Windows-specific). If that happens, delete `node_modules` and re-run `pnpm install` to regenerate the native binary.

- **`pnpm-lock.yaml` must be updated when new dependencies are added to imports.** The gate.ps1 lockfile sync only bumps versions — it doesn't add missing dependencies. If a file imports `@tanstack/vue-query` or `date-fns` but neither is in `package.json`, the Docker build fails silently with Rolldown resolution errors. Run `pnpm add <package>` (or `pnpm add -D <package>`) and commit the updated lockfile alongside the code that uses it. The `pre-commit` ESLint hook doesn't catch unresolved imports — this is a manual check. For CI, add a step that cross-references `package.json` dependencies against imports in `src/`.

### Deploy throttle: anchor on last real deployment, never the most recent triggered run

On 2026-08-08 the deploy throttle in `.github/workflows/deploy.yml` anchored on the MOST RECENT TRIGGERED run — even one that was itself throttled/skipped/failed. Every new run then skipped itself against the previous skipped run, a self-perpetuating cascade: prod was stuck on a 10:52 build for 4+ hours, and even the merged production-incident fix did not deploy.

Rules:
- A throttled/skipped/failed/cancelled run must NOT push the throttle window forward. Anchoring on it creates the skip cascade above — the throttle must anchor on real completed deployments only.
- The anchor is the most recent run whose deploy-to-prod job ("Deploy to app.modulo.run") actually concluded `success`. The `deploy-throttle` job in `.github/workflows/deploy.yml` now walks the workflow runs and checks that job's conclusion before updating the window.
- This is the same "self-referencing measurements" trap as in Reviewer check 2 (see the "Self-referencing measurements" bullet): a throttle/measurement must exclude its own in-progress run and count only real completed outcomes.
- See PR #901 for the fix.

### entrypoint.sh: migration revision IDs must match actual Alembic filenames

`backend/entrypoint.sh` referenced `alembic upgrade 0001_initial_schema` but the actual revision ID is `0001_v2_identity_org` (post-squash). Alembic hangs (doesn't error) when it can't find the target revision, causing the Docker backend container to never start. When renaming or squashing migrations, update `entrypoint.sh` to match.

### Fix Workers must be scoped to specific files only

The fix/pipelines-copy Worker touched files that were already modified by other Workers (LibraryView.vue, ABTestModelsView.vue, VariantCompareView.vue), causing merge conflicts on sequential merges. Worker prompts must explicitly list which files to modify and instruct the Worker not to touch any others. If a Worker needs to also fix related files, it should be split into a separate batch.

### Eval Engine / Error Handling Audit

- **StrEnum validates at the Pydantic model level, not at the engine level.** `EvalType` is a `StrEnum`, so passing `"nonexistent_type"` to `EvalDefinition(eval_type="nonexistent_type")` raises `ValidationError` at construction, never reaching the `UnknownEvalTypeError` handler in the engine's `match/case` dispatch. The `UnknownEvalTypeError` is dead code for normal usage through the Pydantic model — it would only trigger if someone bypasses Pydantic (e.g. `object.__setattr__(eval_def, "eval_type", "bad")`). Tests for unknown-type dispatch must bypass Pydantic validation with `object.__setattr__`.
- **ReDoS detection pattern only catches nested quantifiers with `+` or `*` INSIDE the group before the outer quantifier.** `(a|b)+` is NOT caught because there's no `+` or `*` between `(` and `)` — only an alternation `|`. The `_RE_NESTED_QUANTIFIER` regex (`\(\s*[^)]*[+*][^)]*\s*\)[+*]`) requires a quantifier character inside the group. `(a|b)+` is still a potential ReDoS vector in Python's `re` module, but the current detection is conservative (only catches clear nested quantifiers like `(a+)+`, `(a*)*`).
- **Regex `None` field values are coerced to empty string `""`** via the `str(None)` → `"None"` issue. The code explicitly handles this: `value = "" if raw_value is None else str(raw_value)`. Always verify that `None` → empty string coercion is used in eval field extraction — `str(None)` produces `"None"` which falsely matches patterns like `r"^None$"`.
- **Custom function `functions` config must be validated as `dict`**, not assumed. The eval engine already handles this (`isinstance(fn_registry_raw, dict) else {}`) but it's untested. When audit-testing eval error paths, always check non-dict config values for optional dict-typed config keys.
- **JSON Schema "field not in output" is a separate error path from "field not in output for scoped validation."** The code checks `field not in output` when a field is configured (non-empty) but absent from the output dict — this is distinct from an empty field (validates whole output) or a mismatched schema.

### Ops / Staging Environment

- **Fly.io staging machine size:** `shared-cpu-1x:1024MB` is too small for E2E test load — returns 504 Gateway Timeout under concurrent test workers. Scale staging to `shared-cpu-2x:2048MB` minimum when running E2E tests against it. In `fly.staging.toml`, set `[vm] size = "shared-cpu-2x:2048MB"` and scale with `flyctl scale vm shared-cpu-2x:2048MB --app modulo-staging`.

### Ops / Database (Fly Postgres)

- **Unmanaged Fly Postgres (`fly postgres create`) does NOT auto-restart on crash.** When PostgreSQL on a Flex Postgres machine crashes (e.g. OOM, disk full, segfault), the monitoring agent and `repmgrd` keep running but the `postgres` process stays down. There is no systemd unit to restart it. To recover: SSH into the DB machine (`fly ssh console --app <db-app>`) and run `fly ssh console --app <db-app> --machine <id> --user postgres --command "/usr/lib/postgresql/17/bin/pg_ctl start -D /data/postgresql"`. Consider adding a cron job or health check that restarts PostgreSQL if the process is missing. For production-critical DBs, migrate to Managed Postgres (`fly mpg create`). Note: the server listens on port 5433 internally, not 5432.

### ADR 003 supersedes ADR 001 — Modulo dispatches, it doesn't run agents

The original ADR 001 "Agent Execution Environment" assumed Modulo agents would
run inside sandboxed environments (E2B, Docker) with shell access via
ShellConnector. This was the wrong strategy: established agent runtimes (Claude
Code, opencode, Cursor) are far better at tool-using execution loops. Modulo
should not compete with them.

ADR 003 establishes the **Agent Dispatch Model**:
- Modulo dispatches work to external agent runtimes in E2B sandboxes
- The `sandbox_agent` node type provisions a sandbox, writes prompt + context,
  runs the agent, collects structured output, and tears down
- Modulo owns: dispatch, auth, audit, cost tracking, eval gates, HITL
- The external agent runtime owns: the tool-using loop, file operations, git
- Wall-clock time and exit code are captured natively on every dispatch
- ShellConnector is deprecated — Modulo agents don't run inside sandboxes
- Post-hoc eval of agent output is a separate Modulo pipeline (code review, etc.)

When creating new pipeline features, prefer the `sandbox_agent` node type for
code-generation tasks. The `agent` node type (single-shot LLM call) remains
valid for non-coding tasks (classification, summarization, analysis).

### Fly --strategy immediate desyncs the proxy routing table

\lyctl deploy --strategy immediate\ replaces machines immediately, which desynchronizes Fly's edge proxy routing table from actual machine state. The machines remain healthy (health checks pass at 200), but the proxy returns 503 with "no known healthy instances found for route tcp/443" because old routing entries reference stale machines.

**Symptoms:**
- \lyctl status\ shows machines "started" with "1 total, 1 passing" health checks
- Edge proxy returns 503
- Individual restarts (\lyctl machine restart\, \lyctl apps restart\) do NOT fix it
- The error message hints: "are you using the 'immediate' strategy?"

**Fix — scale-to-zero then scale-up:**
\\\powershell
flyctl scale count 0 --yes -a app-modulo
flyctl scale count 2 --yes -a app-modulo
\\\
This destroys all machines and creates fresh ones that register correctly with the proxy routing table.

**Prevention:**
- \[deploy] strategy = 'rolling'\ is set in \ly.toml\ and \ly.staging.toml\
- Always use the deploy pipeline or \deploy.ps1\ — never \lyctl deploy\ directly
- Never pass \--strategy immediate\ — rolling/canary/bluegreen are the safe options

### Fly restart policies: `on-failure` does NOT restart cleanly-stopped machines; health checks don't self-heal

On 2026-08-08 a rolling deploy left both SAQ worker machines `stopped` for ~3h. The worker restart policy was `on-failure`, and Fly's `on-failure` policy does NOT restart a machine that exits cleanly or is left `stopped` by a rolling deploy — it only restarts on a non-zero exit code. The worker group also had NO health check, so nothing even observed the outage.

Fly restart policy semantics (PR #907 / ADR 021):
- `policy = "always"` — restart no matter the exit code, including a clean exit / `stopped` state.
- `policy = "on-failure"` — restart ONLY when the process exits with a non-zero code. A clean exit or a machine left `stopped` by a rolling deploy is NOT restarted.
- Health checks (`[checks]` / top-level checks) are observability-only — they report status but do NOT restart machines. The restart policy is the self-healing mechanism; a health check alone can never bring a machine back.

Rules:
- Background worker groups (e.g. SAQ workers) must use `policy = "always"` (as in `fly.toml` / `fly.staging.toml` after PR #907) — a worker that exits cleanly must come back.
- Give background workers a liveness check — the SAQ workers expose port 8082 in `deploy/fly/entrypoint.sh` for exactly this reason. A health check that observes the machine is useless without a restart policy that acts on it.
- See `docs/adr/021-worker-resilience.md` and PR #907 for the full postmortem.

### Playwright E2E: use `storageState` for shared login on staging

Authenticating 70+ test workers independently against staging.modulo.run
wastes time and risks rate-limiting. Use a `globalSetup` that logs in once,
saves the authenticated session via `page.context().storageState(...)`, and
lets all workers reuse it via `storageState` in `playwright.config.ts`.

Pattern:
- `globalSetup.ts` launches Chromium, navigates to `/login`, fills credentials,
  submits, waits for redirect, calls `storageState({ path: 'storageState-staging.json' })`
- `playwright.config.ts` sets `use: { storageState: 'storageState-staging.json' }` for
  non-local targets
- Individual tests skip the login step because `loginAsAdmin` detects the
  existing session via `localStorage.getItem('modulo_access_token')`

**Caveat — login page tests:** When `storageState` is loaded, navigating to
`/login` redirects to `/` (dashboard) because the persisted session is still
valid. Tests that expect login page elements (error messages, password fields)
will fail because they're redirected before assertions run.

For tests that need the login page visible:
- Skip `storageState` for those tests (use a separate config or override)
- Or explicitly clear the session in the test before navigating to login
- Or check for redirect: `await page.waitForURL('/login')` before testing elements

### Never merge PRs directly unless explicitly authorised

The `--admin` flag on `gh pr merge` bypasses the merge queue, CI checks, and all SDLC gates. This must NEVER be used unless the user explicitly says "merge it manually" or "use --admin".

Default merge path: let the merge queue pick up approved PRs. If a PR needs expediting, ask the user first. Direct merges skip:
- CI validation (lint, tests, security scans)
- Integration/BDD/Docker checks in the merge queue
- Auto-review re-triggering on the merge commit
- Audit trail in the merge queue

Exception: trivial documentation-only changes (typos, formatting) may be merged directly without asking.


### Manifest: preview: true means dev-mode-only, not feature preview

In rontend/src/manifest.yaml, preview: true on a route or element means it is ONLY visible when MODULO_DEV_MODE=true (dev mode). It does NOT mean "beta" or "coming soon." Routes with preview: true are hidden in normal mode via SidebarNav.vue:94: if (item.preview && !planStore.devMode) return false.

Remy was descoped from MVP and gated behind dev mode. BOTH /admin/remy and /settings/remy have preview: true for this reason — Remy is intentionally invisible in production until dev mode is enabled. Never remove preview: true from a dev-mode-only feature without explicit product direction.

Sidebar tests that check group header counts must account for preview-hidden groups. In simple mode with dev mode off, only core and settings groups are guaranteed visible. Test assertions should use 	oBeGreaterThanOrEqual(2) not 3.

### Manifest dev-gating field is `visibility: private_preview`, and the `<<: *community` anchor is required

The current manifest field for dev-mode-only routes is `visibility: private_preview` (the old `preview: true` boolean no longer exists). A `private_preview` entry MUST also carry the `<<: *community` tier anchor — without it, `required_tier` is undefined and the router's dev-mode guard (`if (to.meta?.visibility === 'private_preview' && !planStore.devMode) return { name: 'dashboard' }`) never runs because the guard only enters that branch when `requiredTier || private_preview` is set and the meta is hydrated from the manifest entry. Omitting the anchor makes the route reachable in production — dead-code protection. `/remy` (remy-only mode) and `/admin/remy` + `/settings/remy` are the canonical examples.

### Remy-only mode renders no permission UI — MCP tools execute un-gated

In the full-screen remy-only ingress (`/remy`), the UI-driving tool family is excluded server-side via `exclude_ui_tools` on the stream request. The frontend therefore renders **no permission card at all**: only UI-driving had a permission/NOGO flow; MCP tools execute server-side without a frontend approval step. When adding a new Remy UI feature, do not assume a permission prompt exists — check whether it runs through the `ui_command_batch`/permission path (gated) or the MCP tool path (ungated).

### Cross-tab/shared `activeSessionId` in remy-only mode is accepted, not solved

The remy-only tabs store persists `{ tabId, sessionId }` pairs in localStorage while titles and the active tab are derived from the shared `useRemyStore.activeSessionId`. The store never introduces a separate `activeTabId` — tab state is reconciled from the shared session state on every sessions fetch (prune dead tabs, reseed on first mount, reassign active on close). This coupling means the panel and the full-screen view intentionally share one active session; divergence is accepted (same as the existing no-cross-tab-sync stance in PRD §8.23).

## Modulo Pipeline Configuration (E2B Sandbox Agents)

All Modulo agent pipelines (Branch Fixer, PR Reviewer, Improve Tests, Improve Architecture, Codebase Improver, Daily Watcher) use a single `sandbox_agent` node type that runs an opencode agent inside an E2B sandbox. The sandbox is ephemeral — created per-run, destroyed after completion.

### E2B Sandbox Configuration

When creating or updating a Modulo pipeline with a `sandbox_agent` node, use these settings:

| Setting | Value | Reason |
|---|---|---|
| `template_id` | `"opencode"` (default, has opencode CLI) or `"modulo-opencode"` (managed cache-warmed template) | The "base" template lacks opencode CLI. "opencode" has it pre-installed; "modulo-opencode" adds dependency caches (faster starts). |
| `agent_command` | `OPENCODE_API_KEY="$APP_MODULO_OPENCODE_API_KEY" GITHUB_TOKEN="$GITHUB_REVIEWBOT_PAT" opencode run --model opencode/deepseek-v4-flash --auto --format json < /home/user/prompt.md` | `opencode run` reads the rendered prompt from stdin. `--auto` skips interactive prompts. `--format json` produces structured output. |
| `timeout_seconds` | `1200` (20 min) | 600s (10 min) is insufficient for complex tasks like rebase + lint fix + push. |
| `env_vars` | `{"GITHUB_REVIEWBOT_PAT":"ghp_..."}` | Needed for git push and gh pr create. Stored in vault as `github-reviewbot-pat`. |

### Identity Separation

The system uses two distinct GitHub identities:

| Identity | Username | PAT (vault entry) | Purpose |
|---|---|---|---|
| **Bot** | `farnalabs` | `github-dogfood-pat-all` | Creates PRs (automation pipelines, improve-* agents) |
| **Reviewer** | `modulo-reviewbot` | `github-reviewbot-pat` | Reviews and approves PRs (PR Reviewer pipeline), posts formal GitHub reviews |

The reviewer identity CANNOT be the same as the bot identity — GitHub does not allow self-approval of PRs. Always use `modulo-reviewbot` for posting reviews.

### Git Operations in E2B Sandbox

Inside the sandbox, git operations require explicit token auth. The `GITHUB_TOKEN` env var is available but not automatically used by git:

```bash
# Clone with auth:
git clone https://x-access-token:$GITHUB_TOKEN@github.com/farnalabs/modulo.git /tmp/repo

# Push with auth:
git push https://x-access-token:$GITHUB_TOKEN@github.com/farnalabs/modulo.git <branch>

# GitHub API for CI status checks:
curl -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/repos/farnalabs/modulo/commits/{sha}/status
```

### Running Python Tools in E2B Sandbox

The sandbox has Python 3.11 but the project requires Python 3.12+. Tell uv to install a compatible version:

```bash
uv python install 3.12 2>&1 | tail -3
```

The `modulo-opencode` template bakes Python 3.12 + dependency caches, so this is a fast no-op on managed sandboxes. It is still needed on the default `opencode` template.

After this, uv run works normally — it uses Python 3.12 instead of downloading CPython 3.14. Pre-commit hooks (which use uv run for ruff, semgrep, etc.) fire correctly on git commit:

```bash
uv python install 3.12 2>&1 | tail -3
pre-commit install
git add -A
git commit -m "..."     # triggers ruff, semgrep, bandit via pre-commit hooks
```

NEVER use `git commit --no-verify` or `git commit -n`. Pre-commit hooks are mandatory.
If a hook fails, fix the issue -- do not bypass it.

If you cannot use pre-commit (build-time constraints), run tools directly:
```bash
pip install ruff 2>&1 | tail -3
ruff check --fix . 2>&1
pip install semgrep 2>&1 | tail -3
semgrep scan --config=.semgrep/ --error backend/src/ 2>&1
pip install mypy 2>&1 | tail -3
mypy src/modulo/ 2>&1
```

### Pipeline Prompt Guidelines
```

If you cannot use pre-commit (build-time constraints), run tools directly:
```bash
pip install ruff 2>&1 | tail -3
ruff check --fix . 2>&1
pip install semgrep 2>&1 | tail -3
semgrep scan --config=.semgrep/ --error backend/src/ 2>&1
pip install mypy 2>&1 | tail -3
mypy src/modulo/ 2>&1
```

1. **CI failures are NEVER transient.** The agent must fix the root cause, not retrigger CI or push empty commits.
2. **Set git identity** before committing: `git config user.email "bot@farnalabs.com" && git config user.name "Branch Fixer Bot"`
3. **Force push after rebase:** `git push origin HEAD:<branch> --force-with-lease`
4. **Output JSON** must be written to `/home/user/output.json` with format: `{"summary":"...","changed_files":[],"pr_url":""}`
5. **Timeout**: If the default 1200s is insufficient, increase it on the pipeline's node definition.

### Known Issues

- **CI runners are hosted (ubicloud-standard-2).** If CI is backed up, check `gh api repos/farnalabs/modulo/actions/runners --jq '.runners[] | [.name, .status, .busy]'` — all runners are hosted; a full busy queue means a CI-backlog, not a local machine to restart (there is no self-hosted runner anymore).
- **Force pushes re-trigger CI normally.** A force-push to a PR branch fires a fresh pull_request (synchronize) event and CI re-runs on the new SHA; the autonomous lifecycle handles re-review and merge.
- **Concurrent run limits on triggers** default to 1. When a pipeline gets a burst of webhooks, increase `max_concurrent_runs` on the trigger to match expected burst volume.

### After raising a PR, poll checks until they pass or fail

After creating a PR via \gh pr create\, the Conductor MUST poll the PR checks until they complete (pass or fail). Do NOT exit the session or start the next task without knowing the CI outcome. The pattern:

`powershell
gh pr checks <PR-NUMBER> --watch
`

This waits for all checks to finish and returns the result. If checks fail, investigate and fix before moving on. A PR with failing checks that gets merged will break main and block all subsequent PRs.

This applies to ALL PRs, not just complex ones. Even a single-file rename can break CI (encoding issues, stale references, missing internationalisation keys). Always wait for green before calling it done.

### Worker restricted-scope contract: allowlist, no deletions, footprint verification

Every Worker prompt MUST carry an explicit file allowlist. The prompt must enumerate the exact files (absolute or repo-relative paths) the Worker is permitted to create or modify. The Worker is forbidden from touching any file outside the allowlist, and must verify `git status --short` before committing shows ONLY allowlisted files.

Deleting or disabling tests is FORBIDDEN. A Worker may never delete a test file, delete tests from a file, or disable tests (skip/xfail/comment out) to make CI pass. If a test genuinely conflicts with the change, the Worker must fix the test IN PLACE to reflect the new correct behaviour - never remove coverage. If a test failure is pre-existing on main and unrelated to the change, the Worker must REPORT it and leave it, not "fix" it by deletion.

Deleting product features/docs/PRD sections is FORBIDDEN. Workers must not delete or rewrite PRD sections, feature files, or documentation for functionality outside their task. The PRD-accuracy step means updating docs to match what was built, NOT deleting product scope.

The Conductor MUST verify footprint at commit time, not trust the report. After the Worker returns, the Conductor runs:
- `git -C <worktree> show --stat --oneline HEAD` - every changed file must be on the allowlist
- `git -C <worktree> show --name-status HEAD | Select-String "^D"` - zero deletions
- `git -C <worktree> log --oneline origin/main..HEAD` - new commits exist

If the footprint exceeds the allowlist or deletes anything, the branch is DISCARDED (worktree removed, branch deleted) and the task is respawned with a tighter contract. Never merge a branch whose footprint was not verified.

Learned on 2026-08-03 when the first Worker attempt at the break-glass deliverable A catastrophically exceeded scope: it deleted ~2736 lines of unrelated tests (test_trigger_crud_tools.py -488, test_executor.py -205, test_linear.py -195), deleted PRD product features (get/update/delete_trigger sections), and modified files across Linear, determination, rate_limiter, and MCP subsystems - all invisible from its commit message. The entire branch was discarded and the work redone by a Worker given a strict scope contract (18-file allowlist), which stayed clean and merged as PR #591. Skipping this contract silently deletes thousands of lines of tests and features, wastes hours, and ends in a discarded branch.


## Reviewer first-pass quality: contract round-trip, prove-the-fix, scope-diff

Analysis of 180 CHANGES_REQUESTED review bodies across 792 merged PRs
(2026-08-08) shows three recurring agent mistakes that cost a review cycle.
First-pass approval is ~82% in August; these three checks are the highest-value
ways to push it higher. Apply all three before pushing a branch.

### 1. Verify the frontend-backend contract round-trip, not just mocked tests

The costliest recurring bug: frontend sends camelCase keys the backend Pydantic
model silently ignores (snake_case), so the setting never persists — found
twice independently (PR #784, #796: `circuitBreakerEnabled` vs
`circuit_breaker_enabled`). CI stayed green because backend tests sent
snake_case and frontend tests mocked `api.GET`.

Rules:
- When a PR touches both frontend and backend (or changes any API
  request/response shape), verify the wire shape against the generated OpenAPI
  types (`frontend/src/lib/api/schema.ts`; regenerate with `pnpm run generate:api`). Frontend keys must match backend field names unless the
  Pydantic model has aliases / `populate_by_name`.
- A test that mocks `api.GET` / `api.POST` / `httpx.Response` does NOT
  validate the contract. Add at least one test that round-trips through the
  real endpoint with the real payload shape (integration or BDD scenario).
- When adding a frontend param/query value, confirm the backend accepts that
  exact value (enum, range, type) in the same PR (PR #767 sent `days=3` and
  `group_by=hour` the backend rejected → 422 at runtime; CI passed only
  because tests mocked `api.GET`).
- Backend tests must exercise the same payload shape the frontend actually
  sends — not an idealized snake_case-only shape.

### 2. Prove the fix actually fixes it — trace the code path, no no-op fixes

The most common single finding: the change does not change behaviour. Trace
the exact code path your change affects and verify observable effect before
pushing. Recurring no-op traps the reviewer has caught:
- Exit-code capture order: `RUNS_EXIT=$?` after a command substitution
  captures the substitution's status (0), not the command's (PR #729).
- Exception MRO ordering: `except ProgrammingError` after `except
  DBAPIError` is unreachable (ProgrammingError subclasses DatabaseError →
  DBAPIError) — order specific exceptions before their bases (PR #740).
- Self-referencing measurements: a throttle measuring against its own
  in-progress run is always "just ran" — exclude the current run (PR #865,
  #570).
- Cache keys must include every input that changes the cached value:
  `(pipeline_id, snapshot_id, node_timeout_seconds)`, not just the first two
  (PR #382).
- Boundary clamps must preserve the invariant they guard: `max(pool_size - 5,
  5)` exceeds a pool of size < 5, reintroducing the exact bug (PR #701).
- Grep patterns must match what the CLI actually emits — `grep -q
  "PreconditionError"` never matches when the CLI prints `error: ...` (PR
  #668).
- The thing being compared must be the thing that runs: jq comparing a number
  field to a quoted string literal is always unequal (PR #865).

Write the test that fails without your fix and passes with it. If you cannot
write such a test, the fix may be a no-op — reconsider.

### 3. Scope-diff before pushing — no silent reverts or deleted tests

"tests:" and "improve:" PRs repeatedly ship silent reverts of production
hardening and deletions of passing coverage (PR #792, #775, #759, #391,
#518). Before pushing any PR whose title scopes it to tests/docs/improve:
- Run `git diff main...HEAD --stat` and confirm every changed file is within
  the stated scope.
- After resolving any merge/rebase conflict, re-check the diff — never let a
  conflict resolution drop the other side's production work. A botched merge
  that reverts a security advisory fix or a production guard is a CRITICAL
  review finding even when CI is green.
- Never delete passing tests or gut behavioural assertions as part of a
  stylistic refactor. If a test conflicts, fix it in place; if a deletion is
  genuinely required, justify it in the PR description.

### 4. Migration heads: branch off the current head, never edit shipped migrations

Migration problems appear in 15 PRs (divergent Alembic heads, editing shipped
migrations in place, deleting applied migrations). Rules:
- Before creating a migration, verify `down_revision` points at the CURRENT
  head — not a mid-chain revision. A migration branching off an old revision
  creates a second Alembic head and a permanent fork every future migration
  must merge around (PR #381). Run `alembic heads` (or the `check-migration-heads`
  pre-commit hook, which fires when migration files are staged) before committing.
- NEVER edit a migration that has already shipped/merged. Alembic records it
  as applied, so the change only lands on fresh DBs — existing deployments
  never execute it (PR #367 edited migration 0022 in place). Add a NEW
  migration on top instead.
- NEVER delete a migration file that has been applied. `alembic_version`
  points at a now-missing revision, breaking the next `alembic upgrade` on
  every existing environment (PR #518).
- If `check-migration-heads.ps1` blocks you: renumber your migration to the
  next free sequential number and fix its `down_revision` to point at the
  current head.

### 5. i18n: no hardcoded user-facing strings in frontend views

The reviewer flags hardcoded English in `.vue` views that otherwise use
`$t()` (16 reviews). Rules:
- Every new user-facing string in a view that already uses
  `$t()`/`t()`/`useI18n` must use a locale key — a hardcoded string is a
  major finding (PR #816, #775, #784, #371).
- Add the key in the CORRECT namespace. A key under the wrong namespace
  silently fails to resolve (PR #438). Keys live in
  `frontend/src/locales/en-US.js` under the view's namespace
  (`views.<ViewName>...`) unless a shared namespace already exists.
- Add the key to ALL locale files, not just `en-US` — a missing key in a
  secondary locale is a silent UI gap.
- Never put JS expressions inside translation values — the vue-i18n message
  compiler cannot parse ternaries; use pluralization syntax
  (`"key | key_plural"`) or simplify the message.
- If the change removes a `$t(w.labelKey)` pattern, re-add the keys to the
  locale file — deleting keys orphans every other locale that references them.

### 6. A11y: dynamic status needs aria-live, icon buttons need labels, keyboards stay usable

The reviewer applies the ux-conformance criteria (A11Y-1/2/4, STATE-1/2/6) to
every frontend diff (33 reviews). Self-check these before pushing:
- Dynamic status messages that appear/disappear (banners, toggle labels
  swapping between "Pause all"/"Resume") need `role="status"` /
  `aria-live="polite"` — WCAG 4.1.3 Status Messages, Level AA (PR #674).
- Error feedback regions need `aria-live="assertive"` (or `role="alert"`) and
  should be wired via `aria-describedby` to the controls they describe (PR
  #784).
- Icon-only buttons need `aria-label` (or visible text). Check every new icon
  button in the diff.
- Keyboard handlers on a container must not break editable children:
  `@keydown.left/right.prevent` on a titlebar fires on bubbled events from a
  child `<input>`, breaking text-cursor movement — ignore events originating
  from `INPUT`/`TEXTAREA` (PR #634).
- Follow `ux-conformance/criteria.yaml` — the same criteria the reviewer
  loads. If a new interactive element lacks `data-testid`, `aria-label`,
  focus-visible ring, or keyboard equivalent, the reviewer will request
  changes.

### 7. A mock or hand-built fixture is not coverage of the changed path

Test-quality findings are the largest single CR category (89 reviews). The
recurring failure: the test exercises a mock or a hand-built fixture that
bypasses the function under test, so the bug lives in the untested real path.
Rules:
- The test must exercise the function under test with the REAL payload shape.
  If you fix `build_facts_query`, the test must go through
  `build_facts_query` — not feed a pre-shaped `SimpleNamespace` row that
  skips the SQL (PR #740). If you fix a percentage conversion, the test
  fixture must use raw values that round-trip through the conversion (PR
  #747).
- "Passes for the wrong reason" check: if you delete the code under test and
  the test still passes, the test is not testing the code (PR #587, #740).
  Assert on behaviour that only the real path can produce.
- Every new behaviour ships with a test that FAILS without the change and
  PASSES with it. This is the feature analogue of the prove-the-fix rule in
  check 2: no new behaviour without a regression test (PR #382, #381, #701).
- When reporting "tests pass", name the actual files run and confirm they are
  wired into CI. A test excluded by a `-m` marker, or that only runs in a
  deploy-only job, is not evidence (PR #547, #518). Check the test is in a
  path CI actually runs.
- A test that mocks `api.GET`/`api.POST`/`httpx.Response` validates the
  mock's opinion, not the endpoint — see check 1 (PR #767, #538).

### 8. The error path must survive the error it guards

Error-handling findings are the second-largest CR category (50 reviews). The
recurring failure: the defensive code itself is broken — the guard raises the
very error it protects against, or the failure path is dead/silent/fail-open.
Rules:
- For every `try/except` you add or touch: is the except reachable? Order
  specific exceptions BEFORE their bases (`except ProgrammingError` before
  `except SQLAlchemyError`); a base clause first makes the specific clause
  dead code (PR #740). Follow the route convention: ProgrammingError->501,
  SQLAlchemyError->503, IntegrityError->409, HTTPException->re-raise,
  Exception->500.
- Does the except do something observable? Log with `_log.exception(...)`,
  return a status, or set a fallback. A catch that silently swallows (bare
  `pass` or unreachable code) hides the failure — the semgrep rule
  `empty-catch-block` and `exception-mro-ordering` enforce this.
- Fail-open vs fail-closed: security/safety operations (auth, RLS, gate
  enforcement, permission checks) fail CLOSED — deny on error (PR #436,
  #470). Best-effort operations (audit, metrics, telemetry) fail open WITH a
  log (PR #497).
- Commit-then-error ordering: if a best-effort step fails AFTER the main
  mutation committed, return the success response and log the failure — do
  not turn a successful operation into a 500 (PR #497).
- Guard the guard: the code that checks the error must itself not raise
  (e.g. `_read_alert_thresholds` overflowed on the very huge ints it guarded
  against, PR #796). If the guard can throw, the failure path is untested —
  add a test for the corruption case the guard is meant to handle.

### 9. Raw-output retention: redact secrets, and never write into the Agent Return Contract columns

Two rules from the FAR-188 raw-output retention work (sandbox stdout retained when `output.json` fails to parse):

- **Redact before you persist.** Sandbox stdout routinely embeds credential-bearing content (tokenized git URLs `https://x-access-token:<PAT>@github.com/...`, `ghp_`/`gho_`/`github_pat_` values, `Bearer ` tokens, `token=` params) because the sandbox command runs with injected `OPENCODE_API_KEY` and `GITHUB_TOKEN`. Any code that stores raw agent output (a `raw_output` marker column, log tails, artifacts) MUST scrub tokenized-URL userinfo and known token patterns before writing. pr_url / structured evidence is extracted from the UNREDACTED source first, then the stored copy is redacted, then truncated. Redaction is best-effort and never blocks retention. Credentials must never enter persistence unmasked (repo rule).

- **Never write failure markers into `outputs_json` / `node_telemetry_json` (the Agent Return Contract columns).** Those columns are consumed by the finalize merge (`split_node_output` idempotence), the node-output API (`GET /runs/{id}/nodes/{nid}/output`), the recovery guard (`recover_node`: `node_id in outputs or node_id in telemetry` => `NodeAlreadyCompletedError`), and cost finalize. A retryable `SandboxNodeFailedError` marker written there (a) wedges the telemetry slot so a successful retry keeps a stale failure marker (evidence + cost undercount), (b) leaks raw stdout up to the API surface, and (c) makes the node look already-completed to recovery. Use a DEDICATED retention column (e.g. `runs.raw_output_markers` keyed by `attempt_key`) instead, and key markers per-attempt so a retry cannot clobber first-attempt PR evidence.

### 10. Terminalization facts: watch write ordering, and wire compensating sweeps + test the wiring

Three rules from the FAR-189 run-classification work (a classifier persisted at terminalization so a streak engine can query records instead of raw status):

- **A fact written AFTER the status write is invisible to an inline hook at the status write.** `work_intact` is computed and written by `_apply_work_intact` AFTER `finalize_cost → update_run_status` fires the classification hook — so every executor-terminalized run recorded `work_intact: None` and the compensating sweep (which skips already-classified rows) never corrected it. When a terminalization-time hook consumes a terminalization fact, verify the fact is written BEFORE the hook fires, or re-run the hook after the fact write.

- **A compensating backfill sweep that has zero production callers is a silent critical.** Raw-SQL terminalizers (cron_helpers sweeps, `saq_hooks._mark_run_failed`, pipeline_execution) bypass the CRUD hook, so a "record for every terminal run" guarantee silently regressed to "record for half of them" — and the sweep that was supposed to fix it was dead code mirroring an equally-unwired precedent (`evidence.reconcile_noop_evidence`). Wire the sweep into a real periodic path (e.g. `dispatcher_reconcile`) AND add an integration test that calls the periodic entrypoint and asserts the sweep was invoked — a direct helper call cannot catch a deleted wiring line.

- **Any every-N-minute production cron query needs a matching partial index.** The 60s `dispatcher_reconcile` sweep selected `WHERE status IN (terminal) AND run_classification IS NULL ORDER BY completed_at DESC LIMIT 50` with no supporting index — a full seq-scan of the wide-row runs table (JSONB outputs_json/raw_output_markers) every minute, even in steady state. A migration that adds a queryable column should add the index for the sweep that reads it, with a `postgresql_where` partial index matching the exact predicate.

### 11. Raw SQL sweeps: self-contained SQL (no cross-table refs outside FROM), and don't split an engine across a cron file

Two rules from the FAR-190 ongoing-streak-engine work (a DB-backed sweep that auto-deactivates ongoing triggers after N no-delivery runs):

- **Every assembled SQL fragment must be self-contained.** A shared boundary fragment referenced 	riggers.streak_epoch / 	riggers.organisation_id while being embedded in a runs-only query (FROM runs r with no 	riggers relation) — on Postgres that is `ERROR: missing FROM-clause entry for table "triggers"`, which rolled back the deactivation UPDATE so the engine could NEVER deactivate anything. The mock-based unit tests routed by SQL-substring matching never executed the SQL, so it shipped green. Any SQL fragment that is interpolated into more than one statement must reference ONLY tables in its own FROM clause or self-contained scalar subqueries / bind parameters — and the real-Postgres integration test (testcontainers) is the only thing that catches a semantic break; substring assertions do not.

- **A cohesive subsystem does not belong bolted onto the SAQ cron helpers file.** The streak engine (~1200 lines: its own SQL, config resolution, atomic deactivation, notification/retry queue, caps and alerting) landed inside cron_helpers.py alongside fire_due_triggers / _ongoing_topup / dispatcher_reconcile, turning it into a 4395-line god-module that every consumer imported whole. Extract it to its own module (core/trigger_streak.py) with a lazy import breaking the cycle, and have the cron file only wire the sweep. The test file was already named for the module — the tests knew the right shape before the code did.

### 12. A TriggerEvent `validation_result` value must be added to `VALIDATION_RESULT_VALUES` in the same change that writes it

From the FAR-190 deactivation lifecycle records (ADR 023): the streak engine writes a fire-outcome TriggerEvent with `validation_result='auto_deactivated'` through `cron_helpers._log_ongoing_event` inside the SAME transaction as the `UPDATE triggers SET active=false` and the AuditEvent. But `trigger_event.VALIDATION_RESULT_VALUES` (and the hardcoded twin in migration 0069) does NOT contain `'auto_deactivated'` — so on real Postgres the `ck_trigger_events_validation_result` CHECK constraint rejects the insert and **rolls back the whole deactivation transaction** (the engine can never deactivate anything), while the mock-based unit tests route by SQL-substring matching and stay green.

Rules:

- A CHECK-constraint vocabulary is a HARD DB gate. Any new `validation_result` value written through `_log_ongoing_event` / `_log_poll_event` must be added to `VALIDATION_RESULT_VALUES` in `trigger_event.py` AND shipped via a NEW migration that widens `ck_trigger_events_validation_result` (Postgres: drop constraint, re-add with `NOT VALID`, then `VALIDATE`; SQLite: Alembic batch mode), in the same change that writes it — never a follow-up. The new migration carries its OWN hardcoded `_VALIDATION_RESULT_VALUES` twin (migrations never import app constants). NEVER edit migrations 0003 or 0069 in place — both have shipped/merged, so an in-place edit changes nothing for already-migrated DBs (Alembic records them as applied and never re-runs them); a fresh DB gets the new vocabulary from the new migration.
- Grep the write sites (`result=` argument of `_log_ongoing_event` / `_log_poll_event`) before shipping a new result value; each must be in the vocabulary.
- Mock/fake-based unit tests never execute the real constraint — the real-Postgres integration test is the only thing that catches a vocabulary mismatch. The FAR-190 integration suite (`tests/integration/test_trigger_streak_engine_sql.py`) calls `_deactivate_trigger_on_no_delivery_streak` against testcontainers and WOULD catch this, but integration tests run only in the deploy workflow, not per-PR CI. Wire the integration test that exercises the real write into per-PR CI (or a merge-queue gate) so a vocabulary drift fails the PR, not the deploy.
