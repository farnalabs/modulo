---
id: feat-infra-sse-event-bus
prd: 8.22
delivery-tasks: [task-nv24-sse-event-bus]
bdd: []
unit-tests: []
code:
  - backend/src/modulo/core/events/event_bus.py
  - backend/src/modulo/core/events/listeners.py
  - backend/src/modulo/api/routes/events.py
  - frontend/src/composables/useEventStream.ts
depends-on: []
status: gap
---

# SSE Event Bus (Real-Time Frontend Sync)

In-memory (and optionally Redis-backed) event pub/sub that pushes resource-change notifications to connected frontend sessions via Server-Sent Events. Eliminates polling loops and keeps the UI in sync with backend mutations from any source (REST API, MCP, background jobs, Celery tasks, CLI scripts).

## Behaviours

### EventBus (in-memory)
- [ ] `EventBus` singleton supports `publish(type, id, action, version, org_id)` — fan-out to all subscriber queues
- [ ] `EventBus.subscribe()` returns `asyncio.Queue` — one per SSE connection
- [ ] `EventBus` catches and logs subscriber-side exceptions without crashing other subscribers
- [ ] Subscriber cleanup on disconnect — queue removed from fan-out set
- [ ] When `settings.redis_url` is set, events also broadcast via `RedisEventBroker` (existing `core/events/redis_broker.py`)
- [ ] In-memory only when no Redis configured — zero infrastructure for dev/test

### SQLAlchemy event listeners
- [ ] `after_insert` / `after_update` / `after_delete` listeners on: Run, Pipeline, Agent, Schema, ConnectorInstance, ModelBackend, Team, Trigger, EvalDefinition, FeedbackRecord, LibraryPrimitive
- [ ] Listeners are registered in a single module (`core/events/listeners.py`)
- [ ] Listeners construct `{type, id, action, version, org_id}` from the model instance
- [ ] `ProgrammingError` is caught — table existence is not assumed (safe during migrations)
- [ ] Listeners fire regardless of the mutation origin (API, MCP, Celery, CLI)

### SSE endpoint
- [ ] `GET /api/v1/events` returns `text/event-stream`
- [ ] Authenticates via standard `get_current_user` dependency
- [ ] Subscribes to `EventBus`, loops on `queue.get()`, yields SSE-formatted `data:` lines
- [ ] Filters events by `org_id` before sending — each subscriber sees only their org
- [ ] Cleans up subscription on client disconnect or connection error
- [ ] Returns 401 without streaming if auth token is invalid/missing

### Frontend EventSource composable
- [ ] `useEventStream()` connects to `/api/v1/events` with auth token
- [ ] Parses incoming `resource_changed` events and dispatches by resource type
- [ ] Registry maps resource types to store handlers (e.g. `run` → `useRunStore().handleSyncEvent`)
- [ ] Composable accepts inline handler for view-local state: `useEventStream({ resourceType: 'run', onEvent: ... })`
- [ ] Composable cleans up `EventSource` on component unmount

### Conflict detection (dirtyIds)
- [ ] Each store tracks `dirtyIds: Set<string>` of locally-edited entities
- [ ] SSE events for dirty IDs are silently dropped
- [ ] Saving or discarding removes the ID from `dirtyIds`
- [ ] Non-dirty events trigger a single-resource `api.GET()` fetch (not full-page refresh)

### Testing
- [ ] Backend: pytest with `httpx.AsyncClient` opens SSE, publishes event, asserts stream delivery
- [ ] Backend: test auth rejection (invalid token → 401)
- [ ] Backend: test org filtering (org A events not delivered to org B subscriber)
- [ ] Backend: test cleanup on disconnect (subscriber removed from fan-out)
- [ ] Backend: test SQLAlchemy listeners fire on model mutations
- [ ] Frontend: Vitest mocks `EventSource`, asserts dispatch to correct store
- [ ] Frontend: Vitest tests `dirtyIds` conflict pattern (event dropped when dirty)

### Error handling
- [ ] Subscriber that throws an exception does not break other subscribers
- [ ] Connection drop (client disconnect) removes subscriber queue cleanly
- [ ] `ProgrammingError` in listener (table doesn't exist yet) is caught — silent no-op
- [ ] No unbounded queue growth — slow subscribers drop events (bounded queue or TTL)
- [ ] SSE connection health-checked — stale connections cleaned up

### Security
- [ ] Endpoint requires valid Bearer token (same as all other API routes)
- [ ] Events filtered by `org_id` server-side — cross-org leakage impossible
- [ ] Event payload contains no sensitive data — just resource type, ID, action, version

## Known Gaps
- No event persistence — reconnecting frontend misses events that occurred while disconnected (REST API is authoritative fallback)
- No backpressure handling for slow SSE subscribers yet
- No per-event type opt-in from the SSE client (client gets all events for their org)
- `RedisEventBroker` cross-worker broadcast listed in PRD §8.22 but not yet wired
