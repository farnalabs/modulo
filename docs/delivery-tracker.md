# Implementation Tracker

## QA Iterate Loop

Uses the `qa-iterate` skill (`qa-iterate <target-path>` from project root) to run multi-lens QA against a code path, fix all critical+major findings, then loop until zero remain. See `Development/Dev-Harness/delivery/delivery-plan.json` for the machine-readable task graph. Checkboxes below track completion.

### Backend — Core Engine
- [ ] **Pipeline engine** — `backend/src/modulo/core/pipeline_engine/`
- [ ] **Secrets backends** — `backend/src/modulo/core/secrets_backend/`
- [ ] **Remaining core** — `backend/src/modulo/core/schema_registry/` (schema registry, trigger engine, hitl manager, audit logger, eval engine, connector hub, model backend hub)

### Backend — Connectors & Data Layer
- [ ] **Connectors** — `backend/src/modulo/connectors/`
- [ ] **DB models** — `backend/src/modulo/db/models/`
- [ ] **DB CRUD** — `backend/src/modulo/db/crud/`
- [ ] **DB infra** — `backend/src/modulo/db/`

### Backend — Model Backends & API
- [ ] **Model backends & OTel** — `backend/src/modulo/model_backends/`
- [ ] **API layer** — `backend/src/modulo/api/` (hardening pass)

### Frontend
- [ ] **Pinia stores** — `frontend/src/stores/`
- [ ] **Composables** — `frontend/src/composables/`
- [ ] **Views** — `frontend/src/views/`
- [ ] **Components** — `frontend/src/components/`

---

## Phase 0 — Foundation (nothing else can start without these)

1. **Alembic schema** — all tables with `organisation_id`, `owner_team_id` (nullable), `visibility`, `evals JSON` column on Agent, pipeline edge entity, `hitl_claims`, `org_api_keys`. Get the full schema right before any code uses it.
2. **`db/rls.py`** — `SET LOCAL` helper, SQLAlchemy connection event hook, isolation integration test. This is the tenant boundary.
3. **`StubModelBackend`** — implements `BaseChatModel` async interface, returns `AIMessage` from fixture map, raises `UnexpectedInputError` on unmapped input. Must exist before any pipeline test.

## Phase 1 — Core runtime (blocking dependency for everything else)

4. **LangGraph→OTel bridge** (`otel_bridge/`) — custom callback handler mapping `on_chain_start/end`, `on_llm_start/end`, `on_tool_start/end` to OTel spans with correct parent propagation. **This is the explicit blocking dependency.** No OTel span assertion in any test is valid until this is built and merged. Assign as a dedicated task.
5. **Basic auth + `SECRET_KEY` enforcement** — startup check, JWT decode with `algorithms=["HS256"]`, `MODULO_ADMIN_PASSWORD`, session lifecycle.
6. **Core entity CRUD** — Pipeline, Agent, Schema, ConnectorInstance, ModelBackend — with org scoping and RLS.

## Phase 2 — Pipeline execution

7. **ConnectorHub** — credential decrypt once per run, run-scoped context object, `FilesystemConnector` with `base_path` chroot, `GitHubConnector`.
8. **ModelBackendHub** — Anthropic + OpenAI + `StubModelBackend` registration, health check, rotation.
9. **`@cancellable_node` decorator** — wraps every node: cancellation check, per-node timeout via `asyncio.wait_for`, run_context write guard for non-context-setter nodes.
10. **Sequential pipeline execution** — `StateGraph` compile + cache, `AsyncPostgresSaver`, manual trigger (`POST /api/v1/runs`), run state machine.
11. **Graph validator** — topology, schema compatibility, connector capability, model backend health.

## Phase 3 — HITL + events

12. **HITL mechanics** — `interrupt()`, atomic claim (`UPDATE ... WHERE claimed_by IS NULL`), `claim_token` (opaque string, 15-min TTL), expiry job, approve/reject, `human_only` enforcement.
13. **WebSocket event broker** — per-run broker, `astream_events()` fan-out, 100-event ring buffer, reconnection + `?since_event_seq=N` replay.
14. **Webhook trigger** — HMAC-SHA256 validation, `payload_mapping`, flood protection (Postgres-backed), deduplication, `TriggerEvent` log.

## Phase 4 — API surface + MCP

15. **ViewModel REST API** — full CRUD, `GET /api/v1/me`, `GET /api/v1/viewmodel/current`, paginated lists.
16. **Remote MCP server** — `/mcp` HTTP+SSE, MCP tools and resources, API key bearer auth, dual-layer scope enforcement, onboarding page `/settings/mcp`.

## Phase 5 — Frontend

17. **shadcn-vue component library init** — install `radix-vue`, `shadcn-vue`, `lucide-vue-next`, `class-variance-authority`, `clsx`, `tailwind-merge`; run `shadcn-vue init`; install baseline primitives (`Button`, `Badge`, `Dialog`, `Tooltip`, `DropdownMenu`, `Input`, `Label`, `Separator`) into `src/components/ui/`. Must be complete before any feature UI is built.
18. **Vue 3 + Pinia scaffold** — org context in all stores; `planStore` hydrated from `GET /api/v1/license` on page load; theme system (`data-theme`, `standard` + `agent`), `?theme=<name>` override; sidebar nav with tier badge (Free/Enterprise pill in footer, reads `planStore`).
19. **`/settings/license` page** — tier card, active feature checklist, license key paste/verify/apply UI, upgrade CTA.
20. **Pipeline canvas** — Vue Flow, node/edge serialisation, connector binding picker, HITL gate edge type.
21. **HITL review UI** — claim, approve, reject, overdue badge, claimed-by indicator.
22. **Run inspection UI** — per-node input/output/prompt/response, sensitive payload masking, "Copy as test fixture".
23. **Stage board** — search, filter by status, `awaiting_human` quick filter.
24. **Library browser** — list, preview, copy-to-adapt wizard.
25. **Demo pipeline + first-run walkthrough** — `MODULO_DEMO_MODE`, pre-loaded `prd-to-requirements`.

## Phase 6 — Alpha exit checklist
Per §10.3b of the PRD: all six criteria must be met explicitly. Alpha does not become v1 by default.
