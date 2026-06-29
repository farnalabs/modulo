# Architecture Guide

Modulo is an orchestration layer for agentic SDLC pipelines. This document
covers the system architecture, key design decisions, and module boundaries.

## System overview

```
┌─────────────────────────────────────────────────────┐
│                   Frontend (Vue 3)                   │
│  Pinia stores → Composables → Views → Components    │
└────────────────────┬────────────────────────────────┘
                     │ HTTP / WebSocket
┌────────────────────▼────────────────────────────────┐
│                    API (FastAPI)                      │
│  Routes → Dependencies → Auth → MCP Server           │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│                   Core Engine                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │ Pipeline │ │  HITL    │ │  Eval    │ │Trigger │ │
│  │ Engine   │ │ Manager  │ │  Engine  │ │ Engine │ │
│  └──────────┘ └──────────┘ └──────────┘ └────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │Connector │ │ Model    │ │  Audit   │ │Library │ │
│  │  Hub     │ │BackendHub│ │  Logger  │ │ Service│ │
│  └──────────┘ └──────────┘ └──────────┘ └────────┘ │
└────────────────────┬────────────────────────────────┘
                     │ SQLAlchemy async
┌────────────────────▼────────────────────────────────┐
│                  Database (PostgreSQL 16)             │
│  Models → Migrations → RLS → LangGraph checkpoints  │
└─────────────────────────────────────────────────────┘
```

## Backend architecture

### Module layout

| Directory | Purpose |
|-----------|---------|
| `modulo/api/` | FastAPI app: routes, middleware, DI, MCP server |
| `modulo/core/` | Business logic: pipeline engine, eval, HITL, connectors |
| `modulo/auth/` | JWT, API key, OIDC, SAML authentication |
| `modulo/connectors/` | External tool integrations (GitHub, GitLab, Jira, etc.) |
| `modulo/model_backends/` | LLM provider wrappers (Anthropic, OpenAI, Ollama) |
| `modulo/otel_bridge/` | LangGraph → OpenTelemetry span translation |
| `modulo/db/` | ORM models, CRUD, migrations, RLS |

### Key design decisions

**Row-level security (RLS):** All tenant isolation is enforced at the database
level via `SET LOCAL app.organisation_id`. Every query runs within the org
scope set by `set_rls_org()`. This prevents cross-tenant data leaks even if
application-level scoping is bypassed.

**Pipeline engine:** Built on LangGraph's `StateGraph` with `dict[str, Any]`
state. The `run_context` and `artifact` are sibling keys in state. Non-context-
setter agents must not write to `run_context`. Compiled graphs are cached by
`(pipeline_id, snapshot_id)` with LRU eviction.

**Credential security:** Decrypted credentials never enter LangGraph state,
checkpoint blobs, OTel spans, or logs. ConnectorHub decrypts once at run-start
into a run-scoped context object.

**HITL (Human-in-the-loop):** Uses LangGraph's `interrupt()` with atomic claim
semantics. Claim tokens are short-lived JWTs (15-min TTL) scoped to
`run_id + gate_id`. Human gates can be configured with `human_only` flag that
cannot be overridden.

**OTel observability:** All pipeline lifecycle events produce OTel spans via
the `LangGraphOtelBridge` callback handler. Spans are exported via OTLP gRPC
or HTTP. No credentials or sensitive payloads appear in span attributes.

## Frontend architecture

### Module layout

| Directory | Purpose |
|-----------|---------|
| `src/stores/` | Pinia stores (auth, pipeline, run, stage, org, library) |
| `src/composables/` | Vue composables (useApi, useWebSocket, useTheme) |
| `src/views/` | Route-level page components |
| `src/components/` | Reusable components (pipeline canvas, shadcn-vue primitives) |

### Theme system

Two themes: `standard` (light) and `agent` (dark). Controlled via
`data-theme` attribute on `<html>`. Set by URL parameter `?theme=<name>`,
localStorage, or the Appearance settings UI.

## Data flow

1. **User triggers a pipeline** (manual, webhook, cron, polling, agent signal)
2. **TriggerEngine** validates the trigger and creates a Run record
3. **PipelineExecutor** loads the snapshot, compiles the StateGraph, and runs it
4. Each node calls **NodeRunner** which invokes the agent's prompt through the
   bound **ModelBackend**, passing connector data via **ConnectorHub**
5. Output is validated against the output **Schema**
6. **EvalEngine** evaluates outputs against configured eval definitions
7. Results, OTel spans, and audit events are persisted

## Architecture Decision Records

ADRs at `docs/adr/` document key trade-offs:

| ADR | Title |
|-----|-------|
| 001 | Agent environment primitives (E2B sandbox model) |

## Import contracts (enforced by import-linter)

- `modulo.api` must not import `langgraph` directly
- `modulo.connectors` must not import `modulo.api` or `modulo.auth`
- `modulo.core`, `.api`, `.connectors` must not import `modulo_cloud`
- `modulo.otel_bridge` must not import `core.pipeline_engine`, `hitl_manager`, `eval_engine`

## Testing strategy

| Layer | Tool | Speed | DB |
|-------|------|-------|----|
| Unit | pytest | <30s | None (mocked) |
| Integration | testcontainers | <2m | Real Postgres |
| BDD | pytest-bdd | <5m | Real Postgres |
| E2E | Playwright | <10m | Real Postgres + Frontend |

Coverage targets: `modulo.auth` 90%, `pipeline_engine` 85%, `db.rls` 95%,
overall 80%.
