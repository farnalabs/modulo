---
id: feat-infra-sse-event-bus
prd: 8.22
delivery-tasks: [task-nv24-sse-event-bus]
bdd:
  - backend/tests/bdd/features/events/sse_event_bus.feature
unit-tests:
  - backend/tests/unit/core/test_event_bus.py
  - backend/tests/unit/api/test_events.py
  - frontend/src/__tests__/useEventStream.test.ts
  - frontend/src/__tests__/syncRegistry.test.ts
code:
  - backend/src/modulo/core/events/event_bus.py
  - backend/src/modulo/core/events/listeners.py
  - backend/src/modulo/core/events/redis_broker.py
  - backend/src/modulo/api/routes/events.py
  - frontend/src/composables/useEventStream.ts
  - frontend/src/composables/useSyncStore.ts
  - frontend/src/stores/syncRegistry.ts
depends-on: []
status: partial
---

# SSE Event Bus (Real-Time Frontend Sync)

In-memory (and optionally Redis-backed) event pub/sub that pushes resource-change notifications to connected frontend sessions via Server-Sent Events. Eliminates polling loops and keeps the UI in sync with backend mutations from any source (REST API, MCP, background jobs, Celery tasks, CLI scripts).

## Behaviours

### EventBus (in-memory)
- [x] `EventBus` singleton supports `publish(type, id, action, version, org_id)` — fan-out to all subscriber queues
- [x] `EventBus.subscribe()` returns `asyncio.Queue` — one per SSE connection
- [x] `EventBus` catches and logs subscriber-side exceptions without crashing other subscribers
- [x] Subscriber cleanup on disconnect — queue removed from fan-out set
- [x] When `settings.redis_url` is set, events also broadcast via `RedisEventBroker` (existing `core/events/redis_broker.py`)
- [x] In-memory only when no Redis configured — zero infrastructure for dev/test
- [x] Lazy singleton initialization via `get_event_bus()`
- [x] Coroutine-safe subscriber mutations via asyncio lock
- [x] Slow consumer detection — queues at maxsize silently dropped from fan-out

### SQLAlchemy event listeners
- [x] `after_insert` / `after_update` / `after_delete` listeners on: Run, Pipeline, Agent, Schema, ConnectorInstance, ModelBackend, Team, Trigger, EvalDefinition, FeedbackRecord, LibraryPrimitive
- [x] Listeners are registered in a single module (`core/events/listeners.py`)
- [x] Listeners construct `{type, id, action, version, org_id}` from the model instance
- [x] `ProgrammingError` is caught — table existence is not assumed (safe during migrations)
- [x] Listeners fire regardless of the mutation origin (API, MCP, Celery, CLI)
- [x] `register_listeners()` called during app startup (`main.py:_lifespan`)
- [x] Unknown model type logged as warning, not crash
- [x] Missing `organisation_id` or `id` attribute logged as warning, not crash
- [x] Per-org monotonically increasing version counter (`_version_counters`)

### SSE endpoint
- [x] `GET /api/v1/events` returns `text/event-stream`
- [x] Authenticates via standard `get_current_user` dependency
- [x] Subscribes to `EventBus`, loops on `queue.get()`, yields SSE-formatted `data:` lines
- [x] Filters events by `org_id` before sending — each subscriber sees only their org
- [x] Cleans up subscription on client disconnect or connection error
- [x] Returns 401 without streaming if auth token is invalid/missing
- [x] 2s keepalive heartbeat detects zombie connections (configurable via `modulo_sse_zombie_timeout_seconds`)
- [x] Per-org connection limit enforced (configurable `modulo_sse_max_connections_per_org`)
- [x] Per-user connection limit enforced (configurable `modulo_sse_max_connections_per_user`)
- [x] Redis broker configured via `configure_event_bus()` at startup

### Frontend EventSource composable
- [x] `useEventStream()` connects to `/api/v1/events` with auth token
- [x] Parses incoming `resource_changed` events and dispatches by resource type
- [x] Registry maps resource types to store handlers (e.g. `run` → `useRunStore().handleSyncEvent`)
- [x] Composable accepts inline handler for view-local state: `useEventStream({ resourceType: 'run', onEvent: ... })`
- [x] Composable cleans up on component unmount
- [x] Uses `fetch()`-based SSE with `Authorization: Bearer` header (not native EventSource — native EventSource can't set custom headers)
- [x] Parses SSE protocol lines (`event:`, `data:`) for named `resource_changed` events
- [x] Exponential backoff reconnection (up to 30s, max 10 attempts)
- [x] CancelledError / AbortError handled gracefully

### Conflict detection (dirtyIds)
- [x] Each store tracks `dirtyIds: Set<string>` of locally-edited entities
- [x] SSE events for dirty IDs are silently dropped
- [x] Saving or discarding removes the ID from `dirtyIds`
- [x] Non-dirty events trigger a single-resource `api.GET()` fetch (not full-page refresh)
- [ ] Store integration wired in `planStore` and `dashboard` — other stores still need `registerHandler()` calls

### Testing
- [x] Backend: pytest with `httpx.AsyncClient` opens SSE, publishes event, asserts stream delivery
- [x] Backend: test auth rejection (invalid token → 401)
- [x] Backend: test org filtering (org A events not delivered to org B subscriber)
- [x] Backend: test cleanup on disconnect (subscriber removed from fan-out)
- [x] Backend: test SQLAlchemy listeners fire on model mutations
- [x] Backend: test slow consumer removal
- [x] Backend: test Redis broker integration
- [x] Backend: test multiple orgs isolation
- [x] Frontend: Vitest tests fetch-based SSE subscribe/unsubscribe
- [x] Frontend: Vitest tests `dispatchToStore` routing
- [x] Frontend: Vitest tests `dirtyIds` conflict pattern (event dropped when dirty)
- [x] Frontend: Vitest tests `useDirtyTracker` mark/check/clean
- [x] BDD: 6 scenarios passing — auth, org isolation, disconnect, resource mutation events

### Error handling
- [x] Subscriber that throws an exception does not break other subscribers
- [x] Connection drop (client disconnect) removes subscriber queue cleanly
- [x] `ProgrammingError` in listener (table doesn't exist yet) is caught — silent no-op
- [x] No unbounded queue growth — slow subscribers drop events (bounded queue with maxsize)
- [x] SSE connection health-checked — stale connections cleaned up (2s heartbeat)
- [x] Redis broadcast failure logged, does not crash publisher

### Security
- [x] Endpoint requires valid Bearer token (same as all other API routes)
- [x] Events filtered by `org_id` server-side — cross-org leakage impossible
- [x] Event payload contains no sensitive data — just resource type, ID, action, version
- [x] Per-org and per-user connection limits prevent abuse

## Known Gaps
- No event persistence — reconnecting frontend misses events that occurred while disconnected (REST API is authoritative fallback). This is by design per PRD §8.22.
- No per-event type opt-in from the SSE client (client gets all events for their org)
- `planStore` and `dashboard` have `registerHandler()` wired; other stores (agents, schemas, connectors, triggers, model_backends, evals, feedback, library) do not yet register SSE sync handlers
- No website docs page at Website/modulo-website/src/docs/ covering SSE Event Bus

## QA History
- 2026-07-02: Cross-cutting QA (index 52). Fixed: frontend EventSource→fetch-based SSE with Bearer auth header (native EventSource can't set custom headers), frontend named event parsing (`event: resource_changed` via SSE protocol parser instead of `onmessage`), per-org monotonically increasing version counter in listeners (was hardcoded `version=0`), created BDD step definitions and verified all 6 scenarios pass. Updated product map: `gap`→`partial`, added `bdd:` and `unit-tests:` frontmatter, marked 40+ behaviours [ ]→[x], added code paths (redis_broker, useSyncStore, syncRegistry). Status: partial (4 known gaps remain).
