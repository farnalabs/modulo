# Alpha — Modulo Platform

**Last updated:** 2026-06-30  
**Current status:** Pre-release / Internal alpha  
**Scope source:** PRD §10.3a, §10.3b, §13

---

## Current Status

Modulo is in **internal alpha** — not publicly released. The platform is functional for internal development and feedback only. The goal is to prove composability, "the remainder" (user-defined schemas/agents), and connector swappability before committing to a v1 public release.

Two concrete implementations of every primitive type exist to validate the abstraction layer.

---

## Included in Alpha

### Primitive Coverage (2 per type)

| Primitive | Implementation A | Implementation B |
|---|---|---|
| Connector | `FilesystemConnector` (`git-host`) | `GitHubConnector` (`git-host`) |
| Trigger | `manual` | `webhook` |
| Model backend | Anthropic Claude | OpenAI GPT-4o |
| Library schema | `markdown-document` | `structured-requirements` |
| Library agent | `document-loader` | `requirements-extractor` |
| Library workflow | `prd-to-requirements` | `requirements-to-file` |

### Infrastructure

- Organisation-scoped data model; all tables carry `organisation_id`
- Postgres RLS with `SET LOCAL` (lint-rule enforced; no bare `SET`)
- Startup sequence: Alembic `upgrade head` → `AsyncPostgresSaver.setup()` → app start
- Postgres advisory lock for multi-worker startup safety
- Async drivers enforced (`asyncpg` / `aiosqlite`; no sync DB in async path)
- LangGraph + PostgresSaver/SqliteSaver with `org_id:` thread ID prefixing
- FastAPI ViewModel + WebSocket event bus (separate transport concerns)
- Basic auth (`MODULO_USERS`) + JWT with `algorithms=["HS256"]` pinned; `SECRET_KEY` ≥ 32 bytes enforced at startup (refuses to start)
- Fernet encryption on all connector and model backend credentials
- Docker Compose (Postgres + API + UI); SQLite fallback for dev
- Security lint rules: `SandboxedEnvironment`, `yaml.safe_load()`, credential-in-state; pre-commit hooks
- API rate limiting middleware with in-memory fallback and startup warning
- `owner_team_id` (nullable) + `visibility` (`org`/`team`) columns in initial Alembic migration (team enforcement is v1)
- `evals: JSON` nullable column on Agent table (not surfaced or executed in alpha — avoids painful v1 migration)
- Pipeline edge entity in initial migration
- Org-level API key entity (`mk_<lookup_prefix>_<secret>`; SHA-256 hash)

### Model Backend Management

- `ModelBackend` entity with provider, model_id, Fernet-encrypted credentials, cost_tracking, currency
- `ModelBackendHub` registration and resolution
- Health check (test inference call on save)
- Credential rotation action
- Two built-in configurations: Anthropic Claude + OpenAI GPT-4o

### Connectors

- `ConnectorType` interface + capabilities list + `ConnectorHub`
- `ConnectorBinding` spec on pipeline nodes (`{type, instance_id}`)
- `FilesystemConnector`: read/write/git push; `base_path` chroot via `os.path.realpath` prefix check
- `GitHubConnector`: read/write/create PR; scope verification in health check
- Pre-run and on-save connector health checks
- Advisory write lock (`pg_try_advisory_lock`) on shared resources
- Connector ACL (visibility, allowed_operations)

### Triggers

- `Trigger` entity + type registry (many-to-one with pipeline)
- Manual trigger (`POST /api/v1/runs`)
- Webhook trigger: HMAC-SHA256 validation, `X-Modulo-Timestamp` required, ±300s replay window, JSONPath `payload_mapping`, flood protection, deduplication, `TriggerEvent` log, replay action
- Pre-run input validation against entry agent schema

### Pipeline + Agent

- Pipeline CRUD with org scoping + `connector_binding` on each node
- Agent CRUD: prompt template (`SandboxedEnvironment`), `model_backend_id` reference, `prompt_version_history`, retry policy
- Schema CRUD: versioning, soft-delete deletion protection
- PipelineSnapshot: pins pipeline definition + schema version refs + prompt version refs + connector bindings
- Graph validation: topology, schema compatibility, connector capability, model backend health, pre-run input schema check
- Sequential pipeline execution via LangGraph `StateGraph`
- Per-node retry policy with configurable backoff
- Run state machine (all states including `claimed`, `waiting_for_lock`)
- Run concurrency controls (`max_concurrent_runs` per pipeline)
- Error recovery: retry-from-node, retry-from-start
- Manual (placeholder) Node: pause for human-provided output with `output_schema_id` validation; review UI identical to HITL claim flow

### HITL

- `interrupt()` → `awaiting_human` state
- Atomic claim (`UPDATE ... WHERE claimed_by IS NULL RETURNING id`)
- Alpha `claim_token`: cryptographically random opaque string stored in `hitl_claims.claim_token` with TTL
- `human_only: boolean` flag on gate definition; ViewModel enforces 403 on MCP approve when `true`
- Per-gate configurable claim expiry; background expiry job
- Approve → resume; Reject → reject-target with required reason; both require `claim_token`
- HITL overdue warning with configurable threshold
- Run retention policy (90-day default)

### Notifications + Audit

- Outbound webhook: HMAC-SHA256 signed, multiple endpoints, events: `hitl_awaiting`, `run_failed`, `claim_expired`, `hitl_overdue`
- AuditEvent writes on all state transitions (no viewer UI in alpha)

### Observability

- LangGraph→OTel bridge (custom callback handler mapping `on_chain_start/end`, `on_llm_start/end`, `on_tool_start/end` to OTel spans)
- Stdout OTel exporter (default); env var config for OTLP/LangSmith
- OTel spans on all LLM calls and connector operations

### Library

- Local library service: CRUD for schemas, agents, workflows
- 2 built-in schemas (`markdown-document`, `structured-requirements`)
- 2 built-in agents (`document-loader`, `requirements-extractor`)
- 2 built-in workflows (`prd-to-requirements`, `requirements-to-file`)
- Copy-to-adapt (clone library primitive into org workspace with ownership picker)
- Rating system deferred to V1

### Frontend

- Vue 3 + Pinia scaffold; org context in all stores; `planStore` hydrated from `GET /api/v1/license`
- shadcn-vue + Radix Vue baseline primitives
- Theme system: `data-theme` on root element; `standard` + `agent` themes; `?theme=<name>` override; `localStorage` persistence
- Sidebar tier badge (Free/Enterprise/License expired pill in nav footer)
- `/settings/license` page (tier card, feature checklist, license key management, upgrade CTA)
- Enterprise-gated feature pattern (lock icon + badge + disabled control + tooltip)
- Vue Flow pipeline canvas with ConnectorBinding picker, HITL gate edge type, inline validation
- Agent config UI (prompt editor with sandbox warning, model backend selector, connector binding, prompt version history)
- Schema editor (field definition, type selection, version history, deletion guard)
- Model backend management UI (register, health check, rotate)
- Connector instance management (create, health check, rotate, ACL)
- Trigger config UI (manual one-click, webhook with path/secret/payload_mapping)
- HITL review UI (full context, claim, approve/reject, claimed-by indicator, overdue badge)
- Run list + detail (state badge, per-node status, error detail, recovery actions, TriggerEvent log)
- Library browser (list, preview, copy-to-adapt)
- Stage board (search, filter by status, `awaiting_human` quick filter)
- Demo pipeline pre-loaded with guided first-run walkthrough
- Real-time progress via WebSocket

### Remote MCP Server

- FastAPI MCP endpoint at `/mcp` (HTTP + SSE, MCP protocol)
- MCP resources: pipelines, runs, HITL gates, library, schemas, connectors, model backends
- MCP tools: `trigger_pipeline`, `get_run_status`, `get_run_output`, `cancel_run`, `review_hitl`, `list_pipelines`, `list_pending_hitl`, `browse_library`, `copy_library_primitive`, `get_trigger_events`
- Cursor-based pagination on all list tools
- API key bearer token auth (alpha); OAuth 2.0 deferred to v1
- Dual-layer scope enforcement (token middleware + ViewModel command layer)
- Per-event SSE org context validation
- MCP onboarding page `/settings/mcp`

### Demo and First-Run

- `MODULO_DEMO_MODE=true`: auto-configures `StubModelBackend` + `FilesystemConnector` with pre-canned data; demo runs with zero external API keys
- Compiled StateGraph cache (in-memory LRU keyed by `snapshot_id`)
- Per-run event broker (in-memory pub/sub fan-out for WebSocket + SSE)

### Testing

- `StubModelBackend` (async `BaseChatModel`, fixture map, `UnexpectedInputError`)
- `pytest-bdd` + Playwright for all alpha features
- Cross-tenant RLS isolation integration test
- Separate integration suite for live connector operations

---

## NOT Included (Alpha Gaps / Limitations)

### Infrastructure

| Feature | Status | Target |
|---|---|---|
| Active multi-tenant routing | Not in alpha | V1 |
| SSO (OIDC / SAML) | Not in alpha (Basic Auth only) | V1 |
| Redis-backed multi-worker broker | Not in alpha (in-memory only) | V1 |
| Cron / polling triggers | Not in alpha | V1 |
| Community library registry | Not in alpha (local library only) | V2 |
| License key enforcement | Not in alpha | V1 |
| Team management (RBAC, visibility enforcement) | Not in alpha (schema columns only) | V1 |

### Features

| Feature | Status | Target |
|---|---|---|
| Eval System (llm_judge, regex, json_schema, custom_function) | Not in alpha | V1 Core |
| Conditional HITL gating (eval → interrupt) | Not in alpha | V1 Core |
| Schema Inference (LLM-assisted drafts) | Not in alpha | V1 Core |
| Run Variants / A/B Testing | Not in alpha | V1 Core |
| Feedback System (FeedbackRecord, correction runs) | Not in alpha | V1 Core |
| Complexity-reviewer library primitive | Not in alpha | V1 Core |
| Run trace / observability UI | Not in alpha (stdout OTel only) | V1 Core |
| Cost controls UI | Not in alpha | V1 Core |
| Audit log viewer / export | Not in alpha (recording is active, viewer is V1) | V1 Core |

### Pipeline Execution

| Feature | Status | Target |
|---|---|---|
| Kick-back edges / parallel branches | Not in alpha (sequential only) | V1 Extended |
| Pipeline creation/editing via MCP | Not in alpha (browser-only) | V2 |
| Schema union/collection types | Not in alpha | V1 Extended |
| Migration functions between schema versions | Not in alpha | V1 Extended |
| OTel exporter config UI | Not in alpha | V1 Extended |

### Connectors

| Feature | Status | Target |
|---|---|---|
| GitLab connector | Not in alpha | V1 Core |
| Jira connector | Not in alpha | V1 Core |
| Linear connector | Not in alpha | V1 Core |
| n8n integration | Not in alpha | V2 |
| Pluggable SecretsBackend (Vault, AWS) | Not in alpha | V2 |

### Security

| Feature | Status | Target |
|---|---|---|
| Checkpoint blob encryption | Not in alpha (plaintext—known gap) | V2 |
| LangGraph PostgresSaver org_id isolation | Not in alpha (thread ID prefix only) | V2 |

### Community

| Feature | Status | Target |
|---|---|---|
| Public release | Not in alpha (internal only) | V1 Core |
| Rating system | Not in alpha | V1 Core |
| Registry protocol (publish/pull) | Not in alpha | V2 |
| Verified publisher program | Not in alpha | V2 |

---

## Known Limitations and Workarounds

### LangGraph Checkpoint Isolation
Checkpoint blobs store all agent inputs/outputs in plaintext in the `langgraph.*` schema, which has no `organisation_id` column. RLS cannot apply. Thread ID prefixing (`org_id:thread_id`) provides application-layer isolation only — insufficient for multi-tenant.
- **Workaround**: Single-org only in alpha. Restrict Postgres access to the application service account; do not grant direct DB access to operators.
- **Target fix**: Subclass `PostgresSaver` with org_id column (V2, before SaaS).

### SQLite Mode
SQLite mode (for local dev only) lacks `SET LOCAL` RLS, `pg_try_advisory_lock`, and `SELECT FOR UPDATE SKIP LOCKED`. No security/concurrency features.
- **Workaround**: Use Postgres (Docker Compose) for any shared deployment.
- **Startup warning**: Logged automatically on SQLite mode.

### Webhook + TLS
Alpha webhook triggers are tested with generic HTTP payloads. GitHub requires HTTPS. Local dev needs ngrok.
- **Workaround**: Use ngrok or similar tunnel for local webhook testing.
- **Target**: Real TLS with reference Caddy config; GitHub webhooks are a V1 use case.

### Single-Process Event Broker
The WebSocket fan-out event broker is in-memory only in alpha — one `astream_events()` consumer per run, in-process pub/sub.
- **Workaround**: Redis pub/sub is required for multi-worker deployments (V1).
- **Startup warning**: Logged on non-Redis configurations.

### In-Memory Rate Limiting
Rate limiting uses an in-memory token bucket — not suitable for multi-process deployments.
- **Workaround**: Single-process for rate-limited endpoints. Redis-backed rate limiting in V1.
- **Startup warning**: Logged when in-memory fallback is active.

### Sensitive Data in Agent Output
Agent-generated output is not automatically masked. If a pipeline reads files containing credentials, those values appear in the run inspection UI.
- **Workaround**: Do not point `FilesystemConnector` at paths containing credentials. Agent output masking is V2.
- **Documented in**: Deployment docs and UI warning on `FilesystemConnector`.

### DOM Sensitive Data
Sensitive values (API keys, connector credentials) are `●●●●●●` by default with a 30-second server-authenticated reveal. CSS hiding is not a security control.
- **Workaround**: N/A — this is the intended behaviour.

### No Audit Viewer
AuditEvents are recorded for all state-changing actions in alpha, but there is no viewer UI or export.
- **Workaround**: Direct DB queries for audit inspection (development only).
- **Target**: Viewer in V1 Core; export in V1 Extended.

### No Community Library Registry
Alpha ships with a local library only. No registry protocol exists for sharing primitives across orgs.
- **Workaround**: Manual YAML bundle export/import for primitive sharing.
- **Target**: Registry protocol in V2.

### No Cost Controls UI
Token counting and cost tracking run in alpha, but there is no UI for viewing or configuring budgets.
- **Workaround**: Check `config/model_pricing.yaml` and run records directly.
- **Target**: Cost controls UI in V1 Core.

---

## Getting Started

### Prerequisites

- Docker Desktop (PostgreSQL 16 + Redis 7)
- Python 3.12+
- `uv` package manager

### Quickstart (from `Development/Product/`)

```powershell
# 1. Start infrastructure
docker compose -f docker-compose.local.yml up -d

# 2. Set up backend (from Development/Product/backend/)
uv sync
# Create .env (see quickstart.md for full reference)
uv run alembic upgrade heads

# 3. Start backend
uv run uvicorn modulo.api.main:app --reload --port 8000

# 4. Start frontend (from Development/Product/frontend/)
npm install
npm run dev
```

### Run the Demo

Set `MODULO_DEMO_MODE=true` in your `.env`. The pre-loaded `prd-to-requirements` pipeline is available on the dashboard. No external API keys are needed — the demo uses `StubModelBackend`.

**Full quickstart**: See [quickstart.md](./quickstart.md)  
**Architecture overview**: See [architecture.md](./architecture.md)  
**Deployment guide**: See [deployment.md](./deployment.md)

---

## Feature Flag State

All feature flags in alpha default to **enabled** (no license key enforcement). The `FreeTierPlanContext` is the default when no `MODULO_LICENSE_KEY` is set.

### Always Enabled (Free Tier)

| Flag | Purpose |
|---|---|
| `parallel_branches` | Pipeline nodes with multiple outgoing edges running concurrently |
| `eval_system` | Eval engine (§7.16 — not surfaced in alpha; placeholder flag) |
| `webhook_trigger` | Inbound webhook triggers |
| `mcp_server` | Remote MCP endpoint |
| `community_library` | Browse and copy community registry primitives |
| `cron_trigger` | Scheduled triggers (V1 — placeholder flag in alpha) |

### Enterprise Gate (requires license key — not in alpha)

| Flag | Purpose | Target |
|---|---|---|
| `sso` | OIDC/SAML authentication | V1 |
| `team_rbac` | Team entity and team-scoped roles | V1 |
| `audit_viewer` | AuditEvent viewer and export | V1 |
| `admin_spend_limits` | Org/team spend and run limit configuration | V1 |
| `view_modes` | Multiple named UI views | V1 |

---

## Alpha Exit Criteria

Alpha is done when ALL six conditions from PRD §10.3b are met. Alpha does not become v1 by default — an explicit decision is required.

| # | Criterion | Verification | Status |
|---|---|---|---|
| 1 | Demo pipeline (`prd-to-requirements`) walkable by 3 non-authors without assistance, using `MODULO_DEMO_MODE` | Manual sign-off by each walker | PENDING |
| 2 | All happy-path BDD scenarios green in CI | Automated (`scripts/verify-alpha-exit.ps1` runs `pytest tests/bdd/`) | PENDING |
| 3 | At least one non-demo pipeline built by an internal user and run to completion | Manual sign-off by builder | PENDING |
| 4 | HITL approve and reject demonstrated by two different named users (`MODULO_USERS` with ≥2 entries) | Manual sign-off by reviewer | PENDING |
| 5 | Connector swap demonstrated: same pipeline run against both `FilesystemConnector` and `GitHubConnector` by rebinding | Manual sign-off | PENDING |
| 6 | Run Context demonstrated: context-setter agent (e.g. complexity-reviewer) changes downstream agent behaviour | Manual sign-off; verified in run inspection | PENDING |

### Verification Script

Run the automated verification script:

```powershell
.\scripts\verify-alpha-exit.ps1
```

This script:
- Runs `pytest tests/bdd/` to check criterion #2
- Prints human-verifiable checklists for criteria #1, #3, #4, #5, #6
- Writes a report to `alpha-exit-report.txt`
- Exits 0 if machine checks pass, 1 if they fail

### Full Exit Checklist

See `docs/prd.md#103b-alpha-exit-criteria` (§10.3b) for the authoritative definition.

---

## References

- [Product Requirements Document](./prd.md) — full PRD with all alpha scope
- [Alpha Scope (PRD §13)](./prd.md#13-alpha-scope) — feature checklist
- [Alpha Exit Criteria (PRD §10.3b)](./prd.md#103b-alpha-exit-criteria)
- [Alpha Documentation Requirements (PRD §10.3a)](./prd.md#103a-alpha-documentation)
- [Non-Goals (PRD §4)](./prd.md#4-non-goals-alpha)
- [Alpha Exit Verification Script](../scripts/verify-alpha-exit.ps1)
- [Architecture Overview](./architecture.md)
- [Quickstart Guide](./quickstart.md)
- [Delivery Tracker](./delivery-tracker.md)
- [Implementation Order (AGENTS.md)](../AGENTS.md#implementation-order)
