# Architecture Guide

Modulo is a self-hosted orchestration layer for agentic SDLC pipelines. This document covers the system architecture, tech stack, key components, data flow, database schema, authentication, and deployment.

## System overview

```
┌──────────────────────────────────────────────────────────────┐
│                    Browser UI (Vue 3 SPA)                     │
│  Standard Theme (light) │ Agent Theme (dark, v1)             │
│  Pinia stores → Composables → Views → Components             │
│  shadcn-vue / Radix Vue primitives                           │
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
│  Celery broker + result backend (optional)                    │
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
| **Task queue** | Celery + Redis | Async task processing (optional) |
| **Auth** | python-jose, authlib (v1) | JWT, OAuth 2.0 |
| **LLM SDKs** | anthropic, openai | Model backend integrations |
| **Observability** | OpenTelemetry | Tracing, metrics |
| **Frontend** | Vue 3 + TypeScript | SPA |
| **State** | Pinia | Client-side state |
| **UI primitives** | shadcn-vue / Radix Vue | Component library |
| **Routing** | Vue Router | Client-side routing |
| **Styling** | CSS custom properties | Theming (standard/agent) |
| **Database** | PostgreSQL 16 | Primary data store |
| **Cache/queue** | Redis 7 | Celery broker, rate limiting |
| **Container** | Docker Compose | Local dev, production |
| **Orchestration** | Helm + Kubernetes | Production (multi-replica) |

## Key Components

### API Layer (`modulo/api/`)

FastAPI application providing REST endpoints, WebSocket event streaming, and the Remote MCP server at `/mcp`. Implements the ViewModel pattern — every user action maps to a named command. Routes are thin: they validate input, resolve dependencies (auth, org context, PlanContext), and delegate to core engine services.

Includes:
- CORS middleware (configurable via `CORS_ORIGINS`)
- Rate limiting middleware (Redis token bucket or in-memory fallback)
- OTel instrumentation middleware
- MCP server adapter (HTTP + SSE transport)

### Pipeline Engine (`modulo/core/pipeline_engine/`)

Built on LangGraph's `StateGraph` with `dict[str, Any]` state. Each pipeline snapshot compiles to a StateGraph at run-start. Nodes map to agents; edges carry HITL gate config or rejection routing.

Key design:
- Compiled graphs cached by `(pipeline_id, snapshot_id)` with LRU eviction
- `run_context` and `artifact` are sibling keys in state — context-setter-only write enforcement
- Human/manual nodes produce `interrupt()` in LangGraph
- `@cancellable_node` decorator wraps every node for graceful cancellation and per-node timeouts (`asyncio.wait_for`)
- Pipeline nesting max depth: 3 levels

### Eval Engine (`modulo/core/eval_engine/`)

Post-node automated quality checks. Runs before any HITL gate check on the same edge. Supports four eval types:

| Type | Description |
|------|-------------|
| `llm_judge` | LLM-as-judge — passes agent output to a model for scoring |
| `regex` | Pattern match against output |
| `json_schema` | Validate output against a JSON Schema |
| `custom_function` | User-defined Python function |

Each eval has a pass threshold and failure behaviour: `warn` (soft — run continues) or `block` (hard — run fails at this node). Eval results feed into the Feedback System.

### HITL Manager (`modulo/core/hitl_manager/`)

Manages Human-in-the-Loop gates using LangGraph's `interrupt()`. Atomic claim semantics via `SELECT ... FOR UPDATE` on `hitl_claims` table. Claim tokens are opaque random strings (alpha) or short-lived JWTs (v1).

Features:
- `human_only` flag — blocks LLM approval via MCP
- `required_team_id` — restricts claims to specific team members
- Claim expiry background job (default: 60s interval, Postgres advisory lock for single-worker execution)
- `manual` node type — same as HITL but human provides full output

### Connector Hub (`modulo/connectors/`)

Abstraction over external tool integrations. ConnectorType defines an abstract capability category (e.g. `git-host`, `shell`). ConnectorInstance is a configured, authenticated binding. ConnectorHub decrypts credentials once at run-start into a run-scoped context object — credentials never enter LangGraph state, checkpoints, OTel spans, or logs.

| Connector | Type | Operations |
|-----------|------|------------|
| `FilesystemConnector` | `git-host` | read/write files, git commit/push |
| `GitHubConnector` | `git-host` | read/write via API, create PR |
| `ShellConnector` | `shell` | run commands in WorkspaceLease |

### Model Backend Hub (`modulo/model_backends/`)

Registered LLM provider wrappers. Agents bind to a model backend at pipeline-save time; `model_id` is resolved from `PipelineSnapshot.model_backend_pins_json` at run time — not the live entity — ensuring consistency across pauses/resumes.

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

Immutable event recording for all state-changing actions. Written in alpha; viewer/export is enterprise-gated. All events carry `organisation_id`, `actor_id`, `action`, `resource_type`, `resource_id`, and `timestamp`.

### Notification System (`modulo/core/notifier/`)

Push notifications (WebSocket events) and outbound webhooks. Per-endpoint HMAC-signed delivery with 3 retries and dead-letter logging. Endpoints auto-disable after repeated failures. Team-scoped notification endpoints.

### Runtime Provider Hub (`modulo/core/runtime_provider/`)

Sandboxed execution environments for coding agents. RuntimeProvider ABC (parallel to ConnectorHub/ModelBackendHub). First implementation: E2B (sandboxed cloud containers). WorkspaceLease is run-scoped and ephemeral.

### Auth System (`modulo/auth/`)

Authentication and authorization — JWT, API keys, OIDC/SAML (v1), Basic Auth (alpha). Dual-layer scope enforcement for MCP (middleware + ViewModel command layer). See dedicated section below.

### Schema Registry (`modulo/core/schema_registry/`)

Versioned JSON Schema definitions (draft-07). Schemas are org-scoped, versioned (semver), reusable, and composable. Abstract schemas enable type-constraint matching during workflow import. Schema inference generates draft schemas from sample connector data.

### Library Service (`modulo/core/library_service/`)

Manages the local and community library of reusable primitives (agents, schemas, workflows, integrations). Community primitives are Ed25519-signed. Copy-to-adapt via `CopyToAdaptWizard` UI component (ownership picker + optional binding step).

## Data Flow

### Pipeline run lifecycle

1. **Trigger** — A trigger fires (manual POST, webhook HMAC-verified, cron schedule, or agent_signal). TriggerEngine validates input against the entry agent's `input_schema`. A Run record is created in `pending` status. TriggerEvent is logged.

2. **Snapshot** — The pipeline's current definition is frozen as a PipelineSnapshot (all agent versions, schema pins, connector bindings, model backend pins, environment profile). The run now executes against this immutable snapshot.

3. **Compile** — PipelineExecutor loads the snapshot, compiles the `StateGraph`, and caches it by `(pipeline_id, snapshot_id)`.

4. **Execute** — Each node:
   a. ConnectorHub resolves bound ConnectorInstances and decrypts credentials once per run
   b. ModelBackendHub resolves the pinned model backend
   c. The agent's Jinja2 prompt is rendered (sandboxed environment) with `run_context` and previous outputs
   d. The LLM is called through the model backend
   e. Output is validated against the output Schema
   f. EvalEngine runs configured evals (llm_judge, regex, json_schema, custom_function)
   g. If eval fails with `block` behaviour, run enters `failed` state
   h. If the outgoing edge has a HITL gate, `interrupt()` pauses the run

5. **HITL** — A human claims the gate (atomic DB lock), inspects context, and approves or rejects. Approval continues to the next node; rejection routes to the reject-target node (or produces a FeedbackRecord).

6. **Complete** — After the terminal node, the run transitions to `complete` or `failed`. OTel spans, audit events, and run metrics are persisted. Notifications are dispatched.

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

Every table carries `organisation_id`. Row-Level Security is enforced via `SET LOCAL app.organisation_id` inside transactions. The session pool resets org context on checkout. LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) do not have RLS — this is a known gap for SaaS (V2).

### Key constraints

- `(trigger_id, payload_hash)` unique on `webhook_dedup_hashes` — deduplication window
- `(run_id, gate_id)` unique on `hitl_claims` — one claim per gate per run
- SchemaVersion deletion protected by active agent/pipeline references
- ModelBackend deletion protected by active references (soft-delete via `status: deprecated`)

## Authentication & Authorization

### Authentication methods

| Method | Status | Use case |
|--------|--------|----------|
| JWT (access + refresh) | Alpha | Browser UI sessions — 15-min access, 7-day refresh |
| API key (bearer token) | Alpha | CI/CD, MCP clients — role-scoped (operator/runner) |
| Basic Auth | Alpha | Multi-user alpha (`MODULO_USERS` env var) |
| OAuth 2.0 (authlib) | V1 | MCP clients (PKCE, exact redirect_uri) |
| OIDC / SAML 2.0 | V1 (enterprise) | SSO with JIT provisioning |

### JWT Security

- Access tokens: 15-min expiry
- Refresh tokens: 7-day expiry, rotated on use
- Algorithm pinning: `HS256` only — `none` and other algs rejected
- SECRET_KEY: minimum 32 bytes (256 bits) — refused at startup if insufficient
- Token family invalidation on revocation
- WebSocket auth via short-lived opaque `ws-token` (60s TTL, single-use, in `Authorization` header, never query string)

### API keys

Format: `mk_<lookup_prefix>_<random_secret>`. Stored as SHA-256 hash. Role set: `operator` (trigger runs, approve HITL) and `runner` (trigger runs, read-only). Admin actions require human session. Keys shown once at creation.

### Row-Level Security

All tenant isolation is at the database layer via `SET LOCAL app.organisation_id` inside transactions. Every query runs within the org scope. This prevents cross-tenant leaks even if application-level scoping is bypassed. Team-visibility resources return 404 (not 403) for non-members — no existence enumeration.

### MCP Scope Enforcement — Dual Layer

1. **Token middleware** — validates required scope on every request
2. **ViewModel command layer** — re-validates scope for every command

Both layers must agree. This prevents scope bypass via routing misconfiguration.

### Rate limiting

| Endpoint | Limit |
|----------|-------|
| Auth login | 10/min per IP |
| Run creation | 60/min per API key |
| Webhook inbound | 100/min per trigger |
| HITL review | 20/min per user |
| MCP tools | 200/min per client ID |

Redis-backed token bucket. In-memory fallback with startup warning for single-process deployments.

## Deployment Architecture

### Modes

| Mode | Components | Use case |
|------|-----------|----------|
| **Standalone** | Single process + SQLite file | Local dev, quick evaluation |
| **Docker Compose** | Backend + Frontend + PostgreSQL 16 + (optional) Redis 7 + (optional) OTel stack | Single-server production |
| **Kubernetes** | Multiple replicas + Celery workers + Redis + PostgreSQL 16 (Bitnami sub-chart) | Multi-replica production |

### Docker Compose

Three compose files:
- `docker-compose.yml` — dev mode (builds from source, Postgres 16, Redis 7)
- `docker-compose.local.yml` — with observability profile (otel-collector, Prometheus, Grafana)
- `docker-compose.test.yml` — CI test environment
- `docker-compose.mariadb.yml` — MariaDB alternative (experimental multi-backend)

### Kubernetes (Helm)

Helm chart at `helm/modulo/`. Referenced images: `ghcr.io/anomalyco/modulo-backend` and `modulo-frontend`.

| Component | Replicas | Dependencies |
|-----------|----------|-------------|
| Backend API | 2+ | PostgreSQL, Redis |
| Celery worker | 1+ | Redis |
| Celery beat | 1 | Redis |
| Frontend (nginx) | 2+ | Backend API |

### Redis dependency

Redis is optional for single-replica deployments. Required for:
- Multi-replica coordination (cron triggers, polling, task queues)
- Distributed rate limiting (Redis token bucket)
- WebSocket event broker (Redis pub/sub)

Without Redis: in-process asyncio scheduler, in-memory rate limiting, in-memory event broker.

### Scaling

- **Vertical**: Gunicorn workers (`GUNICORN_WORKERS` env var) for multi-core single replica
- **Horizontal**: Multiple backend replicas behind a load balancer. Redis mandatory for coordination. PG advisory locks work cross-replica natively.

### CI/CD Pipeline

Self-hosted GitHub Actions runner on Windows. Workflows:
- Lint + type-check + test on every push
- Docker image build + push on tag (ghcr.io)
- Release workflow (tag-driven, semver)

### Observability

OpenTelemetry-native. Default exporter: stdout JSON. Configurable OTLP endpoint (gRPC or HTTP) for Jaeger, Grafana Tempo, or any OTel-compatible backend. Optional LangSmith exporter. Pre-built Grafana dashboards for pipeline performance, HITL review, and cost tracking.

## Architecture Decision Records

ADRs at `docs/adr/` document key trade-offs:

| ADR | Title | Status |
|-----|-------|--------|
| 001 | Agent execution environment as a V1 primitive (E2B sandbox) | Active |
| 002 | Multi-backend database abstraction strategy | Draft |
| 003 | Packaging & distribution strategy (tiered: Docker → PyPI → binary) | Draft |
| 004 | User offboarding uses deactivation (not hard deletion) | Accepted |

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
