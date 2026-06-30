# Modulo — Agent & Developer Guidance

Full PRD: `docs/prd.md`. This file covers how to build. Conflicts between files → fix the conflict.

## Git Workflow

**Always use `git worktree` when branching.** Never check out branches in the main working tree — it must stay on `main`. Worktrees live under `.agents/worktrees/<branch-name>/`.

```powershell
# From Development/Product/
git fetch origin <branch>
git worktree add .agents/worktrees/<branch-name> <branch>
# Work in .agents/worktrees/<branch-name>/
# Commit, push, then clean up:
git worktree remove .agents/worktrees/<branch-name>
git branch -d <branch-name>
```

**Gate script:** `..\..\Dev-Harness\tools\gate.ps1` runs all CI checks (ruff, mypy, bandit, pytest, frontend build) and merges the worktree branch to local main on success — does NOT push to remote. From the worktree root:
```powershell
..\..\Dev-Harness\tools\gate.ps1
```
Use `-Fast` to skip mypy + frontend build, `-Yes` to skip confirmation.

**Publish:** A Windows scheduled task runs `publish.ps1` every 4 hours — it tests local main and pushes to remote only if clean. Remote main is always green.

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

## Task Tracker

The authoritative task list lives at `../Dev-Harness/delivery/delivery-plan.json`. Do not edit it directly — use the task script:

```powershell
../Dev-Harness/tools/task.ps1 list                          # show all tasks and current status
../Dev-Harness/tools/task.ps1 show <id>                     # full detail + history for one task
../Dev-Harness/tools/task.ps1 start <id>                    # begin a task (rejects if deps unmet)
../Dev-Harness/tools/task.ps1 complete <id> -Evidence "..."  # mark done with test evidence
../Dev-Harness/tools/task.ps1 block <id> -Evidence "..."    # record a concrete external blocker
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
    tests/bdd/                # pytest-bdd step definitions
    tests/features/           # Gherkin .feature files (source of truth)
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

- **Backend**: Python 3.12, uv, FastAPI, LangGraph, SQLAlchemy 2 async + asyncpg/aiosqlite/asyncmy, Alembic
- **Frontend**: Vue 3 (Composition API), Pinia, shadcn-vue + Radix Vue, Vue Flow, Tailwind, Playwright
- **API types**: FastAPI OpenAPI → `openapi-typescript` → typed `openapi-fetch` client at `src/lib/api/schema.d.ts`
- **Lint**: ruff, mypy --strict, bandit, semgrep, import-linter, gitleaks
- **Tests**: pytest + pytest-cov, pytest-bdd, testcontainers, factory-boy, pytest-xdist

---

## Key Implementation Constraints (non-negotiable)

### Database
- `SET LOCAL app.organisation_id = :org_id` **inside a transaction** — never bare `SET`. Semgrep-enforced.
- All async DB uses `asyncpg` (Postgres), `aiosqlite` (SQLite), or `asyncmy` (MariaDB/MySQL). No `psycopg2`/`sqlite3` in async path. Semgrep-enforced.
- Alembic `upgrade head` runs before `AsyncPostgresSaver.setup()` on startup. Postgres advisory lock for multi-worker startup.

### Multi-backend DB support

Modulo supports three database backends, configurable via `MODULO_DB` env var:

| Backend | `MODULO_DB` | Driver | Default `DATABASE_URL` |
|---|---|---|---|
| PostgreSQL | `postgres` (default) | `asyncpg` | `postgresql+asyncpg://modulo:modulo@localhost:5432/modulo` |
| MariaDB/MySQL | `mariadb` / `mysql` | `asyncmy` | `mysql+asyncmy://modulo:modulo@localhost:5435/modulo` |
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
# Sets MODULO_DB=mariadb, DATABASE_URL=mysql+asyncmy://modulo:modulo@db:3306/modulo
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
**BDD/E2E** (`tests/bdd/`, `tests/features/`): pytest-bdd + Playwright. All Playwright against `?theme=agent`. Use `waitForSelector('[data-loading="false"]')` — never `waitForTimeout()`. Every interactive element needs `data-testid`.

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
tests/features/
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
# From Development/Product/
docker compose -f docker-compose.local.yml up -d

# From Development/Product/backend/
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

# Start backend (from Development/Product/backend/)
uv run uvicorn modulo.api.main:app --reload --port 8000

# Start frontend (from Development/Product/frontend/)
npm run dev
```

### Known issues & gotchas

- **Migration ID length**: Branch-based migrations have revision IDs >32 chars (e.g. `0005_library_community_visibility`). Alembic creates the `alembic_version` table with `VARCHAR(32)` by default, which causes `StringDataRightTruncationError`. Manually pre-create the table with `VARCHAR(255)` before running migrations.
- **Migration 0014_fixture_contribution bug**: CHECK constraint on `contribution_status` is created before the column is added. The `add_column` call must precede the `create_check_constraint` call.
- **Frontend router missing import**: `PluginsSettingsView` is used as a route component in `src/router/index.ts` but was missing from the top-level imports. Always verify named imports match the import list.
- **Seed org/user (fixed)**: On startup, `main.py` now runs Alembic migrations, creates a default organisation if none exists, and seeds users from `MODULO_USERS`. Plaintext passwords in `MODULO_USERS` (e.g. `admin:admin`) are auto-hashed with bcrypt. Set `MODULO_DEMO_MODE=true` to also create a `demo:demo` read-only user.
- **Backend Settings reads `.env`**: The `.env` file must be in `backend/`, not `Development/Product/`. The `Settings` model uses `env_file=".env"` relative to the cwd.

### Pre-merge smoke test (MANDATORY)

Before merging any worktree branch to `main`, run the smoke test:

```powershell
../Dev-Harness/tools/smoke-test.ps1          # full check (vitest + file existence + type-check)
../Dev-Harness/tools/smoke-test.ps1 -Fast    # skip type-check
```

This checks:
1. **All route component files exist** on disk — catches missing `.vue` files that the router imports
2. **Vitest smoke tests pass** (`app-bootstrap.spec.ts` imports the router module and checks every import resolves)
3. **Vue type-check** (`vue-tsc --noEmit` catches type errors)

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

### Startup scripts (non-blocking)

Use these to launch services without getting blocked:

```powershell
# Start Postgres + Redis (from Development/Product/)
docker compose -f docker-compose.local.yml up -d

# Start backend (from Development/Product/backend/)
Start-Process -NoNewWindow -FilePath "uv" -ArgumentList "run uvicorn modulo.api.main:app --reload --port 8000"

# Start frontend with --host for network access (from Development/Product/frontend/)
Start-Process -NoNewWindow -FilePath "cmd.exe" -ArgumentList "/c npm run dev -- --host 0.0.0.0"

# Check health
Wait-Process -Name "uv" -ErrorAction SilentlyContinue  # doesn't block; just confirms launched
```

### Frontend smoke tests

| Test | File | What it catches |
|---|---|---|
| Unit | `tests/unit/app-bootstrap.spec.ts` | Missing route component files, module-level import errors |
| Playwright E2E | `tests/e2e/ux-review.spec.ts` | Console errors, broken links, missing CTAs, sparse pages |
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

### Product Map / improve-architecture

- When running `improve-architecture` on a feature entry, always check that `bdd:` and `unit-tests:` frontmatter fields are populated — **especially `bdd:`** which is commonly missing even when feature files exist.
- The HTTPBearer FastAPI dependency with `auto_error=False` returns `None` for missing credentials (not 403). The handler must raise 401 explicitly. Product map entries commonly claim 403 for missing bearer — verify against the actual code.
- SCIM CRUD functions (`scim_create_group`, `scim_create_user`) call Team/Account CRUD directly, bypassing REST API validation. Any validation gap in the underlying CRUD (e.g. duplicate name enforcement) is inherited by SCIM. Document this cross-module concern in the product map entry's Known Gaps.
- When auditing behaviours in the product map, do NOT assume an unchecked `[ ]` means "not implemented" — it often means "not verified." Read the test files and run `grep` for each behaviour before deciding status.
- The `prd:` frontmatter field must contain only the bare section number (e.g. `8.17`), never wrapped in quotes or prefixed with `§`. The `§` prefix or quotes cause `graph-validate.ps1` to fail section matching because `TrimStart('§')` cannot strip quotes, leaving a `"8.17"` string that doesn't match the PRD index.
