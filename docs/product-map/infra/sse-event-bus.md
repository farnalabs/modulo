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
depends-on:
  - feat-auth-jwt-auth
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
- [x] Listeners fire after successful DB operations — table must exist for INSERT/UPDATE/DELETE to succeed; no `ProgrammingError` catch needed in listener path
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
- [x] BDD: 6 scenarios passing — auth rejection, org isolation, disconnect cleanup, resource mutation (created/updated/deleted) events

### Security
- [x] Endpoint requires valid Bearer token (same as all other API routes)
- [x] Events filtered by `org_id` server-side — cross-org leakage impossible
- [x] Event payload contains no sensitive data — just resource type, ID, action, version
- [x] Per-org and per-user connection limits prevent abuse

## Resilience & Integration Robustness

- [x] In-memory EventBus works independently of Redis — Redis is optional broadcast overlay
- [x] Redis broadcast failure logged, does not crash in-memory publish
- [x] Fire-and-forget Redis broadcast via `asyncio.create_task` — publisher never blocks on Redis
- [x] Background task tracking (`_background_tasks` set) prevents premature GC of fire-and-forget tasks
- [x] Slow consumer detection — bounded queues (maxsize=256) ejected on QueueFull
- [x] Cleanup on disconnect — unsubscribe removes queue from EventBus and active set
- [x] SSE keepalive heartbeat (configurable timeout) detects zombie connections
- [x] Per-org and per-user connection limits prevent connection exhaustion
- [x] Redis client connections properly closed on both successful `close()` and error paths (fixed in this session)
- [x] `EventBus._remove_dead_queues` is idempotent — no crash if queue already removed
- [x] `_untrack_connection` is idempotent — no crash if queue already removed from active set
- [x] `RedisEventBroker` double-checked locking prevents duplicate connection creation in concurrent access
- [x] Module-level reset in tests (`_reset_singleton`) prevents cross-test interference
- [x] `_test_reset_connections()` clears all tracked SSE connections for test isolation

## Edge Cases

- [x] Empty org returns no events (no subscribers → publish is no-op)
- [x] Non-existent org ID returns no events (no subscribers → publish is no-op)
- [x] Event published before any SSE subscription — event is not queued (no buffer for pre-subscription events; designed: REST API is authoritative)
- [x] Multiple rapid events — bounded queues prevent unbounded memory growth
- [x] SSE client drops connection between heartbeats — cleanup happens on next `yield` via ASGI CancelledError
- [x] Redis broker `publish()` failure closes old connection — prevents connection leak (fixed in this session)
- [x] Redis broker `subscribe()` failure closes old connection — prevents connection leak (fixed in this session)
- [x] `_pump` generator exception is caught — no 500 response leaked to client
- [x] `asyncio.CancelledError` in `_pump` is caught — graceful stream termination
- [x] Transaction rollback after event listener fires — phantom event sent (best-effort design; REST API is authoritative)

## Known Gaps
- No event persistence — reconnecting frontend misses events that occurred while disconnected (REST API is authoritative fallback). This is by design per PRD §8.22.
- No per-event type opt-in from the SSE client (client gets all events for their org)
- `planStore` and `dashboard` have `registerHandler()` wired; other stores (agents, schemas, connectors, triggers, model_backends, evals, feedback, library) do not yet register SSE sync handlers
- No website docs page at Website/modulo-website/src/docs/ covering SSE Event Bus
- ~~`_background_tasks` set has no cleanup mechanism for completed fire-and-forget tasks — the set grows without bound under sustained Redis broadcast volume, leaking memory proportional to event throughput. Should add `task.add_done_callback(set.discard)` or use `asyncio.TaskGroup`.~~ **FIXED**: Both `event_bus.py` and `listeners.py` call `task.add_done_callback(_background_tasks.discard)`. No memory leak.
- SSE per-org/per-user connection limit rejection has no specified error response — clients receive no structured error body explaining which limit was hit, their current count, or the limit value.

## QA History
- 2026-07-02: Cross-cutting QA (index 52). Fixed: frontend EventSource→fetch-based SSE with Bearer auth header (native EventSource can't set custom headers), frontend named event parsing (`event: resource_changed` via SSE protocol parser instead of `onmessage`), per-org monotonically increasing version counter in listeners (was hardcoded `version=0`), created BDD step definitions and verified all 6 scenarios pass. Updated product map: `gap`→`partial`, added `bdd:` and `unit-tests:` frontmatter, marked 40+ behaviours [ ]→[x], added code paths (redis_broker, useSyncStore, syncRegistry). Status: partial (4 known gaps remain).
- **2026-07-08**: Cross-cutting QA (improve-architecture index 252). Fixed MINOR — Redis connection leak in `redis_broker.py:publish()` and `subscribe()`: error handlers set `_pub`/`_sub` to `None` but did not close the old connection, leaking TCP connections on Redis failure. Added `close()` calls before clearing references. Added Resilience & Integration Robustness section (15 checkboxes) and Edge Cases section (10 checkboxes) to product map. Corrected inaccurate claim about `ProgrammingError` catch in listeners (listeners fire after successful DB ops — no SQL executed in listener path). Added 4 new unit tests for Redis broker error-path connection cleanup. All existing tests pass.
