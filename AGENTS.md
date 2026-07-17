# Modulo — Agent & Developer Guidance

Full PRD: `docs/prd.md`. This file covers how to build. Conflicts between files → fix the conflict.

## Diagnostic Order (MANDATORY — database/connection issues)

When encountering ANY database connection error (`ConnectionResetError`, `ConnectionDoesNotExistError`, timeout, 503), follow this order BEFORE making code changes:

1. **Check DB health** — `fly checks list --app modulo-app-db` (or the relevant DB app). If `pg` or `role` checks are critical/passing, the DB is fine. If critical, SSH in and restart: `fly ssh console --app <db-app> --machine <id> --command "su - postgres -c '/usr/lib/postgresql/17/bin/pg_ctl start -D /data/postgresql'"`. The `check-db-health.ps1` watchdog runs every 5 minutes as a scheduled task — check its log first.

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
| Multi-task delivery sprint | Use the `deliver` skill (`.agents/skills/deliver/SKILL.md`) which orchestrates parallel subagents autonomously. |
| QA fix | Spawn a subagent in its own worktree branch. Never apply a fix directly from the QA session. |

The root `AGENTS.md` has the full non-negotiable rule under **Agent Isolation: All Code Goes Through Subagents** — read it for the rationale and enforcement details.

## Skills

- **`qa`** — Multi-lens quality review. Invoke with `qa <target-path>`. Runs 7 lenses (correctness, bugs, maintainability, SOLID, DRY, simplification, deps) via parallel subagents, validates findings, and applies fixes. Auto-invokes `lessons-learned` on fixed findings. Path: `.agents/skills/qa/SKILL.md`.
- **`lessons-learned`** — Extracts recurring patterns from QA findings and codifies them as AGENTS.md guidance at the most specific level of the hierarchy (auto-invoked by `qa` / `qa-iterate`). Standalone: `/lessons-learned <target> <findings>`. Path: `.agents/skills/lessons-learned/SKILL.md`.

## Delivery Workflow for QA

1. Check `docs/delivery-tracker.md` — QA Reviews section.
2. Run each QA review using the `qa` skill.
3. After finishing a review, toggle its checkbox and add the date + outcome.
4. Do not start QA #N+1 until QA #N is complete.

---

## Definition of Done

### Manifest updated
- [ ] **Manifest updated** — if the delivery adds or modifies a page route, the corresponding entry in `frontend/src/manifest.yaml` was created or updated

---

## Task Tracker

The authoritative task list lives at `../harness/delivery/delivery-plan.json`. Do not edit it directly — use the task script:

```powershell
../devtools/harness/tools/task.ps1 list                          # show all tasks and current status
../devtools/harness/tools/task.ps1 show <id>                     # full detail + history for one task
../devtools/harness/tools/task.ps1 start <id>                    # begin a task (rejects if deps unmet)
../devtools/harness/tools/task.ps1 complete <id> -Evidence "..."  # mark done with test evidence
../devtools/harness/tools/task.ps1 block <id> -Evidence "..."    # record a concrete external blocker
```

The conductor picks the first `pending` task whose entire `dependsOn` array is `completed`. Tasks span phases 0–9 (alpha through v2). Run `/deliver` from the project root to start an autonomous delivery sprint — this invokes the `deliver` skill at `.agents/skills/deliver/SKILL.md`.

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
- `StateGraph` cached keyed by `(pipeline_id, snapshot_id)` with LRU eviction.

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

---

## Testing Strategy

**Unit** (`tests/unit/`): no DB, no Docker, `StubModelBackend` for all LLM calls, run in < 30s.
**Integration** (`tests/integration/`): real Postgres via testcontainers, Alembic migrations applied first, Factory Boy for entities. Cross-tenant isolation test is mandatory.
**BDD/E2E** (`tests/bdd/features/`, `tests/bdd/steps/`): pytest-bdd + Playwright. All Playwright against `?theme=agent`. Use `waitForSelector('[data-loading="false"]')` — never `waitForTimeout()`. Every interactive element needs `data-testid`.

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
npm run dev
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

The `@smoke` tag is set per-test via `{ tag: '@smoke' }` in `frontend/tests/e2e/`. Add it to any critical test that should gate merges. Run just the smoke subset with `npm run test:e2e:smoke`.

The history of this rule: `SchemaBuilderView.vue` existed as an untracked file, was deleted during cleanup, and the router still imported it — causing a 500 on every page load. The smoke test would have caught it.

### OpenAPI type generation

`npm run dev` auto-generates TypeScript types from the backend's OpenAPI spec (`http://localhost:8000/openapi.json` → `frontend/src/lib/api/schema.d.ts`). The backend must be running for this to work.

To generate types manually without starting the dev server:

```powershell
npm run generate:api
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
npm run generate:api      # one-shot
npm run dev               # auto-generates on start
```

After regenerating, verify the frontend still compiles with `npm run build`.

---

## Schema Generation

The frontend TypeScript types in `frontend/src/lib/api/schema.ts` are auto-generated from
the backend FastAPI OpenAPI schema. When backend API contracts change (new/modified routes,
request/response models), the schema must be regenerated:

```powershell
cd frontend
npm run generate:api
```

This runs `scripts/generate-api-types.ps1` which imports the backend, dumps the OpenAPI
schema as JSON, and feeds it to `openapi-typescript` to produce the typed client.

There is no pre-commit hook for this — the pre-commit framework runs `generate-api-types` as a manual-stage hook only (`gate.ps1` Phase 1d). You must regenerate manually or run `pre-commit run generate-api-types` when the backend API changes. If CI fails because `schema.ts` is out of date, run `npm run generate:api`, commit the updated file, and retry.

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
`npx` / `npm run dev` don't work for backgrounding — always use `node.exe` with the full path to `vite/bin/vite.js`.

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
Start-Process -NoNewWindow -FilePath "cmd.exe" -ArgumentList "/c npm run dev -- --host 0.0.0.0"

# Check health
Wait-Process -Name "uv" -ErrorAction SilentlyContinue  # doesn't block; just confirms launched
```

### Frontend smoke tests

| Test | File/Command | What it catches |
|---|---|---|
| Unit | `tests/unit/app-bootstrap.spec.ts` | Missing route component files, module-level import errors |
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

Migration collision check (`check-migration-heads.ps1`) runs both in
pre-commit (when migration files staged) and in gate.ps1 Phase 0 (even
with `-SkipTests`). If blocked: renumber your migration to the next free
sequential number and fix its `down_revision` to point at the current head.

### Rebasing: only when another branch merged first � and how to resolve conflicts

In general, **no pre-rebase is needed** � the worktree branch is based on
main and the PR flow handles merging. If another PR merged first (changing
shared files), rebase to catch up.

If the rebase produces conflicts, resolve them inline:

1. Read all three versions: base, main (ours), worktree (theirs)
2. Understand the intent of each side's change
3. Produce a merged version that satisfies both intents � never silently
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

**Backend** � from `Repos/modulo/backend/`:
```
pytest tests/unit/ --tb=short -q --timeout=120
```
The backend suite takes ~35-40 min (14700+ tests). Frontend � from `Repos/modulo/frontend/`:
```
npm run test:unit
```
(478 tests, ~4 min). Both must pass before reporting "tests pass" or proceeding with any merge.

### Frontend worktrees and node_modules

`git worktree add` creates a new working tree with no installed dependencies.
Frontend Workers cannot reliably run `npm run lint`, `npx vue-tsc`, or `npm test`
inside worktrees. Workers implement and commit without frontend tooling;
verification happens via GitHub CI on the PR.

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
## Deployment: checkpointer init silently fails when pg_connection_string strips sslmode

`pg_connection_string()` had `.split("?")[0]` which stripped `?sslmode=require` from Fly.io's DATABASE_URL. psycopg's `AsyncConnection.connect()` needs SSL on Fly.io Postgres, so without sslmode the connection fails silently (exception is caught and logged as a warning). This means the `checkpoint_migrations` table is never created, which causes the health check to fail, which blocks bluegreen deployments.

Fix: never strip query params from DATABASE_URL. `asyncpg.connect()` and `psycopg.AsyncConnection.connect()` both accept standard Postgres query params like `sslmode=require`.

### Deployment: health check `finally` block `conn.close()` can override inner `return`

In `_check_checkpointer()`, the inner `try/except` catches query failures and returns "degraded". But the `finally` block runs `conn.close()` before the return completes. If `conn.close()` raises, the exception propagates to the outer `except Exception`, overrides the "degraded" result, and produces "unavailable" with empty detail — even though the query failure was the real issue.

Fix: wrap `conn.close()` in a nested `try/except` so a close() failure can never override the inner result.

### Deployment: any unavailability blocks bluegreen — return "degraded" for non-critical checks

Fly.io's bluegreen strategy waits for ALL health checks to return non-"unavailable" before cutting over. A single non-critical check (like checkpointer tables missing) returning "unavailable" blocks the entire deployment. Change any check that the app can function without to return "degraded" instead of "unavailable".

### Frontend i18n: vue-i18n message compiler cannot parse JS ternary expressions in translation values

`{count === 1 ? '' : 's'}` inside a translation value is parsed by `@intlify/message-compiler` as a malformed interpolation expression, causing build failures with error code 7. Never use JS expressions inside translation strings. Use vue-i18n pluralization syntax (`"key | key_plural"`) or simplify the message.

### Ops: npm install on Windows generates lockfile with platform-specific packages

Running `npm install` on Windows adds packages like `@rollup/rollup-win32-x64-msvc` to the lockfile. Docker builds on Linux reject these with EBADPLATFORM. Use `npm install --force` to skip platform checks. The `--force` flag is needed because the lockfile is generated from a Windows development environment and deployed to Linux.

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

- `openapi-fetch` returns error objects (not strings) on non-2xx responses — never embed bare `${err}` in template literals. Always use `formatApiError(err)` (see `frontend/src/lib/api/formatError.ts`) to extract a readable `error.detail` or `error.message`.
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

- **Error tracking API endpoints that read from DB must fetch all data inside the `session.begin()` transaction block.** If a query like `get_error_group()` is made inside the transaction (for RLS context) but a subsequent `get_error_events_by_group()` call is made outside it, the second call runs without RLS context and can leak cross-org data or return stale results. Wrap all DB reads/writes in the same `async with session.begin():` block that contains the auth/RLS setup.
- **Redis async calls from sync context: always `await` the coroutine.** `_get_last_fired` and `_set_last_fired` in alert evaluation were defined as `async def` but called without `await` — the coroutine object was silently discarded, the cooldown never persisted to Redis, and the method returned `True` (non-None coroutine) so cooldowns appeared perpetually active. Never discard an `async` coroutine without `await` unless intentionally fire-and-forget (and even then, `asyncio.create_task` is preferred).
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

### Translation values must not contain newlines or HTML entities

Translation values in `en-US.js` (and all locale JSON files) must NEVER contain:
- Literal newlines (`\n`)
- HTML entities like `&#10;`, `&amp;`, etc.

If a UI element needs multi-line text (like a textarea placeholder with multiple lines), split it into separate translation keys and concatenate them in the template with `+ '\n' +`. This ensures:
1. Translation files stay machine-parseable and diffable
2. No encoding issues between JSON values and HTML attribute bindings
3. Translators see clean, single-line strings

### Frontend / Store & View Patterns

- Do not duplicate computed properties across a Pinia store and a Vue view. Define the computed once in the store and reference it from the view via `storeName.propertyName`.
- Runtime validation of API responses from the app's own backend should be minimal (top-level null/type checks or Zod schema), not 100+ lines of per-field manual type-checking. TypeScript and tests catch shape mismatches at build/test time — full field-level validation is over-engineering for internal endpoints.
- Keep store fetch methods consistent across the same store. Both `fetchSummary` and `fetchTrends` should follow the same error-handling pattern — no `console.warn` in production code, both should set `error.value` on failure.
- Event handler type guards (`if (event.type !== 'run' && event.type !== 'pipeline') return;`) must precede state mutations (`syncingIds.add`). Adding an ID before the type check means unhandled event types permanently block future events with the same ID.
- Inline markup duplicated between desktop and mobile variants (view mode toggles, brand headers) must be extracted to a shared component. If both sidebars render the same UI element, it belongs in a single `.vue` file.

### Backend / Async & Concurrency

- `asyncio.create_task()` called from sync code (SQLAlchemy listeners, signal handlers) → guard with `try: asyncio.get_running_loop(); except RuntimeError: log_warning(...) else: create_task(...)`. Without this guard, calling sync code without a running event loop crashes with `RuntimeError: no running event loop`.
- Monkey-patching stdlib types (`asyncio.Queue._user_id = ...`) → use a separate tracking structure (`dict[int, str]` keyed by `id(queue)`). Stdlib types are not guaranteed to have dunder-namespace stability and future CPython versions may add internal `_`-prefixed attributes that collide.
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

- **Before deploying, run `npm run build` locally to catch frontend build errors early.** The Docker build lacks interactivity and hides errors behind 10-minute retries. Common issues caught: Rolldown parser errors from Vue template syntax, missing dependencies imported but not in `package.json`, duplicate manifest.yaml keys from parallel distributed work. The local frontend build may fail due to a corrupted `lightningcss.win32-x64-msvc.node` binary (native module, Windows-specific). If that happens, delete `node_modules` and re-run `npm install` to regenerate the native binary.

- **`package-lock.json` must be regenerated when new dependencies are added to imports.** The gate.ps1 lockfile sync only bumps versions — it doesn't add missing dependencies. If a file imports `@tanstack/vue-query` or `date-fns` but neither is in `package.json`, the Docker build fails silently with Rolldown resolution errors. Run `npm install <package> --save` and commit the updated lockfile alongside the code that uses it. The `pre-commit` ESLint hook doesn't catch unresolved imports — this is a manual check. For CI, add a step that runs `node -e "require('./package.json').dependencies"` and cross-references against imports in `src/`.

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

### Ops / Database (Fly Postgres)

- **Unmanaged Fly Postgres (`fly postgres create`) does NOT auto-restart on crash.** When PostgreSQL on a Flex Postgres machine crashes (e.g. OOM, disk full, segfault), the monitoring agent and `repmgrd` keep running but the `postgres` process stays down. There is no systemd unit to restart it. To recover: SSH into the DB machine (`fly ssh console --app <db-app>`) and run `su - postgres -c '/usr/lib/postgresql/17/bin/pg_ctl start -D /data/postgresql'`. Consider adding a cron job or health check that restarts PostgreSQL if the process is missing. For production-critical DBs, migrate to Managed Postgres (`fly mpg create`).

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
