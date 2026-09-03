# Architecture Guide

Modulo is a self-hosted agent governance platform for building governed, repeatable AI-assisted software delivery pipelines. This document covers the system architecture, tech stack, key components, data flow, database schema, authentication, and deployment.

## System overview

```
┌──────────────────────────────────────────────────────────────┐
│                    Browser UI (Vue 3 SPA)                     │
│  Standard Theme (light) │ Agent Theme (dark, v1)             │
│  Pinia stores → Composables → Views → Components             │
│  PrimeVue component library                             │
└──────────────────────┬───────────────────────────────────────┘
                       │ HTTP REST + WebSocket (Event Bus)
┌──────────────────────▼───────────────────────────────────────┐
│                   API Layer (FastAPI)                         │
│  Routes → Dependencies → Auth → ViewModel Commands           │
│  MCP Server at /mcp (HTTP + SSE)                             │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│                  Core Engine (Python)                         │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Pipeline   │  │  HITL    │  │  Eval    │  │ Trigger   │  │
│  │ Engine     │  │ Manager  │  │  Engine  │  │ Engine    │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────┘  │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Connector  │  │  Model   │  │  Audit   │  │ Feedback  │  │
│  │ Hub        │  │BackendHub│  │  Logger  │  │ Manager   │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────┘  │
│  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Notifier   │  │ Runtime  │  │  Schema  │  │ Library   │  │
│  │            │  │ Provider │  │ Registry │  │ Service   │  │
│  └────────────┘  └──────────┘  └──────────┘  └───────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │ LangGraph (StateGraph execution)
                       │ SQLAlchemy async (asyncpg)
┌──────────────────────▼───────────────────────────────────────┐
│              PostgreSQL 16 + Redis 7                          │
│  Models → Migrations → RLS → LangGraph checkpoints            │
│  SAQ worker jobs (Redis-backed)                                │
│  Rate limiting (Redis token bucket or in-memory)              │
└──────────────────────────────────────────────────────────────┘
```

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Backend** | Python 3.12+ | Runtime |
| **API framework** | FastAPI | REST + WebSocket + MCP server |
| **Graph execution** | LangGraph (StateGraph) | Pipeline agent orchestration |
| **ORM** | SQLAlchemy 2.0 (async) | Database access |
| **Migrations** | Alembic | Schema versioning |
| **Task queue** | SAQ + Redis | Async job processing (required for multi-replica) |
| **Auth** | PyJWT[crypto], authlib (v1) | JWT, OAuth 2.0 |
| **LLM SDKs** | anthropic, openai | Model backend integrations |
| **Observability** | OpenTelemetry | Tracing, metrics |
| **Frontend** | Vue 3 + TypeScript | SPA |
| **State** | Pinia | Client-side state |
| **UI primitives** | PrimeVue | Component library (themed via `primevue-theme.ts` token bridge) |
| **Routing** | Vue Router | Client-side routing |
| **Styling** | CSS custom properties | Theming (standard/agent) |
| **Database** | PostgreSQL 16 | Primary data store |
| **Cache/queue** | Redis 7 | SAQ broker, rate limiting |
| **Container** | Docker Compose | Local dev, production |
| **Orchestration** | Docker Compose + Fly.io | Managed hosting (app.modulo.run) / self-hosted single-server |

## Key Components

### API Layer (`modulo/api/`)

FastAPI application providing REST endpoints, WebSocket event streaming, and the Remote MCP server at `/mcp`. Implements the ViewModel pattern – every user action maps to a named command. Routes are thin: they validate input, resolve dependencies (auth, org context, PlanContext), and delegate to core engine services.

Includes:
- CORS middleware (configurable via `CORS_ORIGINS`)
- Rate limiting middleware (Redis token bucket or in-memory fallback)
- OTel instrumentation middleware
- MCP server adapter (HTTP + SSE transport)

### Pipeline Engine (`modulo/core/pipeline_engine/`)

Built on LangGraph's `StateGraph` with `dict[str, Any]` state. Each pipeline snapshot compiles to a StateGraph at run-start. Node types: `agent`, `sandbox_agent`, `manual` (human output), `composite` (expand-only), `router` (ordered JMESPath rules + `default`, lowers to the conditional-edge compile path — FAR-402 P1), and `hitl` (human-in-the-loop gate that compiles to the existing synthetic-gate path — FAR-402 P1). `connector` is an internal engine resolution, never an API-authored node type. Edges carry HITL gate config, rejection routing, or a `loop`/`conditional`/`normal`/`reject` edge type.

Key design:
- Compiled graphs cached by `(pipeline_id, snapshot_id)` with LRU eviction
- `run_context` and `artifact` are sibling keys in state – context-setter-only write enforcement
- Human/manual nodes produce `interrupt()` in LangGraph
- `@cancellable_node` decorator wraps every node for graceful cancellation and per-node timeouts (`asyncio.wait_for`)
- Pipeline nesting max depth: 3 levels

### Eval Engine (`modulo/core/eval_engine/`)

Post-node automated quality checks. Runs before any HITL gate check on the same edge. Supports four eval types:

| Type | Description |
|------|-------------|
| `llm_judge` | LLM-as-judge – passes agent output to a model for scoring |
| `regex` | Pattern match against output |
| `json_schema` | Validate output against a JSON Schema |
| `custom_function` | User-defined Python function |

Each eval has a pass threshold and failure behaviour: `warn` (soft – run continues) or `block` (hard – run fails at this node). Eval results feed into the Feedback System.

### HITL Manager (`modulo/core/hitl_manager/`)

Manages Human-in-the-Loop gates using LangGraph's `interrupt()`. Atomic claim semantics via `SELECT ... FOR UPDATE` on `hitl_claims` table. Claim tokens are opaque random strings (alpha) or short-lived JWTs (v1).

Features:
- `human_only` flag – blocks LLM approval via MCP
- `required_team_id` – restricts claims to specific team members
- Claim expiry background job (default: 60s interval, Postgres advisory lock for single-worker execution)
- `manual` node type – same as HITL but human provides full output
- `hitl` node type (FAR-402 P1) – a draggable human-in-the-loop gate; compiles to the same synthetic-gate path as a legacy edge-level HITL gate. `manual` remains the non-gating human-output step.

**Decision-payload contract (normative, FAR-541):** every resume decision is a dict `{"action": <verdict>, "gate_id": <the identity it resolves>}` plus any per-action members (`output`, `modified_output`, `reason`, `notes`). `HITLManager._decide` is the single stamp authority: it stamps a payload that lacks `gate_id` with the claim row's gate id and refuses (422) a payload stamped for a *different* gate; call-site stamps (API routes, MCP) remain because they feed the direct `executor.resume` injection that bypasses `_decide`. A decision is honoured ONLY by the gate/node its stamp names — every consumer verifies the stamp against its own identity and fails closed on a missing/foreign stamp (re-interrupt, never resume):

| Writer | Stamp (`gate_id`) | Consumer | Recognized actions |
|---|---|---|---|
| `POST /runs/{id}/hitl/{gate}/approve`, `/approve-with-modification`, `/reject`, `/deliver-manual`; MCP `review_hitl` | the gate id from the URL | `_hitl_gate_resume_result` | `approved` (incl. `modified_output`), `rejected`, `deliver_manual` |
| `POST /runs/{id}/manual/{node}/submit` | the manual node's id | `_manual_node` | any dict payload stamped with this node's id completes the node (the documented writer is `manual_output` (+ `output`)) |
| `POST /runs/{id}/nodes/{node}/recover` (operator break-glass) | the run's pending claim row's gate id (node id for manual nodes; the guardrail gate id for conformance blocks); unstamped when no undecided row exists | `_manual_node` / `_handle_conformance_resume` | `skip`, `replay` |
| Conformance override via HITL API | the blocked node id or the block's guardrail gate id | `_handle_conformance_resume` | `approved`, `deliver_manual` (override); `rejected` fails closed |

Interrupt payloads carry the same identity: the gate node interrupts with its `gate_id`, a manual node with `gate_id: <node_id>`, a conformance block with the block's guardrail gate id — the executor keys the pending `hitl_claims` row on that `gate_id` verbatim. The dispatcher reconcile resumes an `awaiting_human`/`claimed` run ONLY per this scoping matrix: claimed-undecided → skip (under the `uq_hitl_claims_run_gate` `UNIQUE (run_id, gate_id)` constraint a claimed-undecided row and a committed decision for the same gate cannot coexist — crash recovery for claimed runs routes through the no-undecided-rows branch once the decision commits); unclaimed undecided row → conservative skip; no undecided rows → crash-recovery resume when the decision's stamp routes it to a consumer that accepts it — `hitl_gate_*`/guardrail identities accept only the verdict actions, MANUAL-node identities also accept a committed `manual_output` with its `output` (legacy pre-stamping rows are stranded by design — at most the 2026-09-02 incident cohort; ops remedy is a manual DB stamp or ticket, no backfill migration). Recover-node refuses HITL gate targets (422) — gate decisions must go through approve/reject; user node ids squatting the reserved `hitl_gate_` prefix are rejected at graph-validation time.

### Connector Hub (`modulo/connectors/`)

Abstraction over external tool integrations. ConnectorType defines an abstract capability category (e.g. `git-host`, `shell`). ConnectorInstance is a configured, authenticated binding. ConnectorHub decrypts credentials once at run-start into a run-scoped context object – credentials never enter LangGraph state, checkpoints, OTel spans, or logs.

| Connector | Type | Operations |
|-----------|------|------------|
| `FilesystemConnector` | `git-host` | read/write files, git commit/push |
| `GitHubConnector` | `git-host` | read/write via API, create PR |
| `GitLabConnector` | `git-host` | read/write via API, merge requests |
| `ShellConnector` | `shell` | run commands in WorkspaceLease |
| `SlackConnector` | `messaging` | send messages, search channels |
| `JiraConnector` | `issue-tracker` | create/search/update issues |
| `LinearConnector` | `issue-tracker` | create/search/update issues |
| `NotionConnector` | `documentation` | read/write pages and databases |
| `ConfluenceConnector` | `documentation` | read/write pages |
| `PagerDutyConnector` | `incident-management` | trigger/acknowledge/resolve incidents |
| `SentryConnector` | `error-tracking` | list/search issues, create events |
| `DatadogConnector` | `monitoring` | query metrics, create monitors |
| `RestConnector` | `rest` | verb-agnostic HTTP read/write against a declared endpoint (see `docs/rest-connector.md`) |
| *(40+ built-in connectors total — see `modulo/connectors/`)* | | |

### Model Backend Hub (`modulo/model_backends/`)

Registered LLM provider wrappers. Agents bind to a model backend at pipeline-save time; `model_id` is resolved from `PipelineSnapshot.model_backend_pins_json` at run time – not the live entity – ensuring consistency across pauses/resumes.

| Provider | Status |
|----------|--------|
| Anthropic Claude | Alpha |
| OpenAI GPT | Alpha |
| Azure OpenAI | V1 |
| Bedrock | V1 |
| Ollama | V1 |
| Custom | V1 |

### Trigger Engine (`modulo/core/trigger_engine/`)

Accepts manual, webhook, cron, polling, and agent_signal trigger types. Creates Run records and initiates pipeline execution. Webhook flood protection via Postgres `SELECT ... FOR UPDATE SKIP LOCKED`. Payload deduplication via `webhook_dedup_hashes` table with configurable window.

### Audit Logger (`modulo/core/audit_logger/`)

Immutable event recording for all state-changing actions. Written in alpha; viewer/export is team-gated. All events carry `organisation_id`, `actor_id`, `action`, `resource_type`, `resource_id`, and `timestamp`.

### Notification System (`modulo/core/notifier/`)

Push notifications (WebSocket events) and outbound webhooks. Per-endpoint HMAC-signed delivery with 3 retries and dead-letter logging. Endpoints auto-disable after repeated failures. Team-scoped notification endpoints.

### Runtime Provider Hub (`modulo/core/runtime_provider/`)

Sandboxed execution environments for coding agents. RuntimeProvider ABC (parallel to ConnectorHub/ModelBackendHub). First implementation: E2B (sandboxed cloud containers). WorkspaceLease is run-scoped and ephemeral.

### Auth System (`modulo/auth/`)

Authentication and authorization – JWT, API keys, OIDC/SAML (v1), Basic Auth (alpha). Dual-layer scope enforcement for MCP (middleware + ViewModel command layer). See dedicated section below.

### Schema Registry (`modulo/core/schema_registry/`)

Versioned JSON Schema definitions (draft-07). Schemas are org-scoped, versioned (semver), reusable, and composable. Abstract schemas enable type-constraint matching during workflow import. Schema inference generates draft schemas from sample connector data.

### Library Service (`modulo/core/library_service/`)

Manages the local and community library of reusable primitives (agents, schemas, workflows, integrations). Community primitives are Ed25519-signed. Copy-to-adapt via `CopyToAdaptWizard` UI component (ownership picker + optional binding step).

## Data Flow

### Pipeline run lifecycle

1. **Trigger** – A trigger fires (manual POST, webhook HMAC-verified, cron schedule, or agent_signal). TriggerEngine validates input against the entry agent's `input_schema`. A Run record is created in `pending` status. TriggerEvent is logged.

2. **Snapshot** – The pipeline's current definition is frozen as a PipelineSnapshot (all agent versions, schema pins, connector bindings, model backend pins, environment profile). The run now executes against this immutable snapshot; the snapshot is tagged `version_kind='run'`.

   **Live-edit history + release channels (ADR 025 / FAR-402 P6):** the snapshot
   machinery is reused for versioning beyond run-start freezes. The editor's
   save action creates a new snapshot tagged `version_kind='edit'` (the live-edit
   chain), leaving prior rows immutable so rollback is a pointer swap to a prior
   snapshot. A snapshot also carries a `release_channel` (`none` | `stable` |
   `canary`); a trigger bound to a `stable`/`canary` channel resolves to the
   latest snapshot of that channel (`TriggerEngine.resolve_snapshot_id_for_trigger`),
   while an unbound trigger pins the live graph (current behaviour).
   `diff_snapshots` surfaces port-signature deltas + a deterministic downstream
   impact oracle (`compute_port_change_impact`), and a save-time check
   (`check_port_change_breaking`) flags port changes that would drop/alter data
   read by a downstream edge.

3. **Compile** – PipelineExecutor loads the snapshot, compiles the `StateGraph`, and caches it by `(pipeline_id, snapshot_id)`.

4. **Execute** – Each node:
   a. ConnectorHub resolves bound ConnectorInstances and decrypts credentials once per run
   b. ModelBackendHub resolves the pinned model backend
   c. The agent's Jinja2 prompt is rendered (sandboxed environment) with `run_context` and previous outputs
   d. The LLM is called through the model backend
   e. Output is validated against the output Schema
   f. EvalEngine runs configured evals (llm_judge, regex, json_schema, custom_function)
   g. If eval fails with `block` behaviour, run enters `failed` state
   h. If the outgoing edge has a HITL gate, `interrupt()` pauses the run

5. **HITL** – A human claims the gate (atomic DB lock), inspects context, and approves or rejects. Approval continues to the next node; rejection routes to the reject-target node (or produces a FeedbackRecord).

6. **Complete** – After the terminal node, the run transitions to `complete` or `failed`. OTel spans, audit events, and run metrics are persisted. Notifications are dispatched.

### WebSocket event flow

```
LangGraph astream_events()
  → Per-run event broker (in-process pub/sub)
    → WebSocket connections subscribe (per Vue tab)
    → MCP SSE connections subscribe (per LLM client)
```

In multi-worker deployments: Redis pub/sub replaces in-process broker.
On reconnect: client re-fetches current state via `GET /api/v1/runs/{id}`, then replays missed events via `?since_event_seq=N` (ring buffer, 100 events).

## Database Schema

### Core entities

```
Organisation
  ├── User (org-scoped)
  │   ├── TeamMembership (user_id, team_id, team_role)
  │   └── ApiKey (user_id, role, key_hash)
  ├── Team (org-scoped)
  │   └── TeamMembership (as above)
  ├── Pipeline (org-scoped, optional owner_team_id, visibility)
  │   ├── PipelineSnapshot (immutable, run-start freeze)
  │   ├── Trigger (pipeline_id, trigger_type, config_json)
  │   ├── PipelineEdge (pipeline_id, source, target, edge_type, hitl_gate_config)
  │   └── Run (pipeline_id, snapshot_id, status, state machine)
  │       ├── hitl_claims (run_id, gate_id, claimed_by, claim_token, expires_at)
  │       └── TriggerEvent (trigger_id, validation_result, run_id)
  ├── Stage (org-scoped, optional owner_team_id, visibility)
  ├── Schema (org-scoped)
  │   └── SchemaVersion (schema_id, version, definition_json)
  ├── Agent (org-scoped)
  │   └── prompt_version_history (agent_id, version, template)
  ├── ConnectorInstance (org-scoped, optional owner_team_id)
  ├── ModelBackend (org-scoped)
  ├── EnvironmentProfile (org-scoped)
  ├── LibraryPrimitive (org-scoped, primitive_type, content_json)
  ├── AuditEvent (org-scoped, immutable)
  ├── EvalDefinition (org-scoped)
  ├── FeedbackRecord (org-scoped, run_id, node_id)
  └── VariantGroup (org-scoped, run comparisons)
```

### RLS enforcement

Every table carries `organisation_id`. Row-Level Security is enforced via `SET LOCAL app.organisation_id` inside transactions. The session pool resets org context on checkout. LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) do not have RLS – this is a known gap for SaaS (V2).

### Key constraints

- `(trigger_id, payload_hash)` unique on `webhook_dedup_hashes` – deduplication window
- `(run_id, gate_id)` unique on `hitl_claims` – one claim per gate per run
- SchemaVersion deletion protected by active agent/pipeline references
- ModelBackend deletion protected by active references (soft-delete via `status: deprecated`)

## Authentication & Authorization

### Authentication methods

| Method | Status | Use case |
|--------|--------|----------|
| JWT (access + refresh) | Alpha | Browser UI sessions – 15-min access, 7-day refresh |
| API key (bearer token) | Alpha | CI/CD, MCP clients – role-scoped (operator/runner) |
| Basic Auth | Alpha | Multi-user alpha (`MODULO_USERS` env var) |
| OAuth 2.0 (authlib) | V1 | MCP clients (PKCE, exact redirect_uri) |
| OIDC / SAML 2.0 | V1 (team) | SSO with JIT provisioning |

### JWT Security

- Access tokens: 15-min expiry
- Refresh tokens: 7-day expiry, rotated on use
- Algorithm pinning: `HS256` only – `none` and other algs rejected
- SECRET_KEY: minimum 32 bytes (256 bits) – refused at startup if insufficient
- Token family invalidation on revocation
- WebSocket auth via short-lived opaque `ws-token` (60s TTL, single-use, in `Authorization` header, never query string)

### API keys

Format: `mk_<lookup_prefix>_<random_secret>`. Stored as SHA-256 hash. Role set: `operator` (trigger runs, approve HITL) and `runner` (trigger runs, read-only). Admin actions require human session. Keys shown once at creation.

### Row-Level Security

All tenant isolation is at the database layer via `SET LOCAL app.organisation_id` inside transactions. Every query runs within the org scope. This prevents cross-tenant leaks even if application-level scoping is bypassed. Team-visibility resources return 404 (not 403) for non-members – no existence enumeration.

### MCP Scope Enforcement – Dual Layer

1. **Token middleware** – validates required scope on every request
2. **ViewModel command layer** – re-validates scope for every command

Both layers must agree. This prevents scope bypass via routing misconfiguration.

### Rate limiting

Hardcoded sliding-window rules enforced by `RateLimitMiddleware` (see `backend/src/modulo/api/middleware/rate_limiter.py`):

| Path prefix | Limit | Window |
|-------------|-------|--------|
| `/api/v1/runs` | 60 | 60s |
| `/api/v1/triggers` | 100 | 60s |
| `/api/v1/errors/ingest` | 10 | 60s |
| `/mcp` | 200 | 60s |
| Auth endpoints (`/api/v1/auth/`) | 10 attempts | 60s (configurable via `MODULO_AUTH_MAX_ATTEMPTS`) |

Redis-backed sliding window (ZADD + ZREMRANGEBYSCORE). Falls back to in-memory no-op when Redis is unavailable. Auth rate limiter requires Redis and is disabled without it.

## Deployment Architecture

### Modes

| Mode | Components | Use case |
|------|-----------|----------|
| **Standalone** | Single process + SQLite file | Local dev, quick evaluation |
| **Docker Compose** | Backend + Frontend + PostgreSQL 16 + (optional) Redis 7 + (optional) OTel stack | Single-server production |

### Docker Compose

Compose files (`docker-compose*.yml` at the repo root; the non-default ones live under `deploy/compose/`):
- `docker-compose.yml` – dev mode (builds from source, Postgres 16, Redis 7)
- `docker-compose.local.yml` – with observability profile (otel-collector, Prometheus, Grafana)
- `deploy/compose/docker-compose.prod.yml` – self-hosted single-server production (prebuilt image)
- `deploy/compose/docker-compose.test.yml` – CI test environment
- `deploy/compose/docker-compose.mariadb.yml` – MariaDB alternative (experimental multi-backend – **deprecated 2026-07-11**, not actively tested or maintained)

### Kubernetes (Helm)

The Kubernetes/Helm example deployment configs were removed – they were never
exercised by CI or used in production. Self-hosting is via Docker Compose
(`deploy/compose/docker-compose.prod.yml`); the managed deployment path is Fly.io.

### Redis dependency

Redis is **required** for production: SAQ (the only dispatch path) uses Redis as
its job broker. Redis is also required for:
- Multi-replica coordination (cron triggers, polling, task queues)
- Distributed rate limiting (Redis token bucket)
- WebSocket event broker (Redis pub/sub)

Without Redis: SAQ dispatch, cron firing, and the scheduler are unavailable.
In-memory rate limiting and in-memory event broker are fallbacks for
non-production use.

### Scaling

- **Vertical**: Uvicorn worker processes (`uvicorn --workers`) for multi-core single replica
- **Horizontal**: Multiple backend replicas behind a load balancer. Redis mandatory for coordination. PG advisory locks work cross-replica natively.

### CI/CD Pipeline

Hosted Ubicloud runners (ubicloud-standard-2). Workflows:
- Lint, type-check, unit test, frontend build, audit, and WCAG contrast test on every push
- Each backend/frontend container is built once, scanned with Trivy, and published to ghcr.io only from `main` or a version tag
- Staging smoke, WCAG, and regression suites share one dependency/browser setup while retaining separate result artifacts
- Release workflow (tag-driven, semver)

### Observability

OpenTelemetry-native. Default exporter: stdout JSON. Configurable OTLP endpoint (gRPC or HTTP) for Jaeger, Grafana Tempo, or any OTel-compatible backend. Optional LangSmith exporter. Pre-built Grafana dashboards for pipeline performance, HITL review, and cost tracking.

### Supporting Resources

- [System Requirements](./system-requirements.md) – minimum resources, supported databases
- [Configuration Reference](./configuration-reference.md) – full environment variable reference
- [Deployment Guide](./deployment.md) – production deployment instructions
- [Deployment Journeys](./deployment-journey.md) – three deployment paths
- [Upgrade Process](./upgrade-process.md) – upgrading existing deployments
- [Public Launch Checklist](./public-launch-checklist.md) – production readiness verification

---

## Architecture Decision Records

ADRs live in the private `farnalabs/devtools` repo at `Repos/devtools/adr/` (migrated out of this repo 2026-09-02, FAR-434; they were previously in-repo under `docs/adr/`). They document key trade-offs:

| ADR | Title | Status |
|-----|-------|--------|
| 001 | Agent Execution Environment as a V1 Primitive | Implemented |
| 002 | Multi-Backend Database Abstraction Strategy | Draft |
| 003 | Agent Dispatch Model | Supersedes ADR 001 |
| 003 | Packaging & Distribution Strategy | Draft |
| 004 | Agent as a Self-Contained Bundle | Accepted |
| 004 | User Offboarding Uses Deactivation (Not Hard Deletion) | Accepted |
| 005 | Agent Architecture: Two-Tier Orchestration + Execution | Accepted |
| 005 | Self-Hosted Deployments Use One Org; Teams Are the Separation Boundary | Active |
| 006 | Dashboard Performance: Application Cache Over Materialized View | Active |
| 007 | Remy UI Commands: Frontend-Mediated Browser Automation | Active |
| 008 | Core Shared Manifest: Single Source of Truth for Page Structure | Active |
| 009 | Frontend Monitor Backend Abstraction | Accepted |
| 010 | Integration Tier Classification (Native / Preview / In-Dev) | Accepted |
| 011 | Remy Context Sources: Configurable Knowledge Domains with Progressive Disclosure | Active |
| 012 | Migrate to Managed Fly Postgres | Proposed – implementation deferred until production data warrants backups |
| 014 | Remy Stream: JWT as MCP API Key | Accepted |
| 015 | Bundle Format v2 (YAML) | Accepted |
| 016 | Agent Log Observability | Accepted |
| 017 | Celery to SAQ Migration | Accepted |
| 017/018 | Centralized Authorization: Shared Permission Registry for REST + MCP | v9 – revised after 7 plan-review-iterate cycles |
| 019 | Cost Formula Engine + E2B Rate/Fallback Decision | Accepted |
| 020 | Analytics: run_daily_facts + typed-params query surface | Accepted |
| 025 | Generic REST Integration Connector | Accepted |

Note: ADR numbers 003/004/005 are shared by two distinct ADR files each (the numbering mirrors the filesystem). ADR 017/018 – Centralized Authorization – exists as both `017-centralized-authorization.md` and `018-centralized-authorization.md` (a duplicated file), so it is listed once here under the combined number.

## Import Contracts (enforced by import-linter)

- `modulo.api` must not import `langgraph` directly
- `modulo.connectors` must not import `modulo.api` or `modulo.auth`
- `modulo.core`, `.api`, `.connectors` must not import `modulo_cloud`
- `modulo.otel_bridge` must not import `core.pipeline_engine`, `hitl_manager`, `eval_engine`

## Testing Strategy

| Layer | Tool | Speed | DB |
|-------|------|-------|----|
| Unit | pytest | <30s | None (mocked) |
| Integration | testcontainers | <2m | Real Postgres |
| BDD | pytest-bdd | <5m | Real Postgres |
| E2E | Playwright | <10m | Real Postgres + Frontend |

Coverage targets: `modulo.auth` 90%, `pipeline_engine` 85%, `db.rls` 95%, overall 80%.
