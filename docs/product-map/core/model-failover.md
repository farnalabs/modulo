---
id: feat-core-model-failover
prd: 8.1
delivery-tasks: [task-nv9-model-failover]
bdd:
  - backend/tests/bdd/features/model_backends/backend_selection.feature
  - backend/tests/bdd/features/model_backends/rate_limiting.feature
code:
  - backend/src/modulo/core/model_backend_hub/__init__.py
  - backend/src/modulo/api/routes/model_backends.py
  - backend/src/modulo/db/models/model_backend.py
  - backend/src/modulo/db/crud/model_backend.py
  - backend/src/modulo/db/migrations/versions/0035_model_fallback.py
unit-tests:
  - backend/tests/unit/core/model_backend_hub/test_hub.py
  - backend/tests/unit/core/model_backend_hub/test_failover.py
  - backend/tests/unit/api/test_model_backends_endpoint.py
  - backend/tests/integration/crud/test_model_backend.py
depends-on: [feat-core-run-context]
status: partial
---

# Model Backend Failover

Automatic failover when a model backend is unhealthy. The `ModelBackendHub`
selects a configured fallback (or scans all registered backends) and emits an
audit event. API creates/updates/reads `fallback_backend_ids` on the entity.

## Behaviours

### Registration & Lifecycle

- [x] Hub registers a backend by UUID and returns it via `get()`
- [x] `get()` raises `BackendNotFoundError` for unregistered ID
- [x] `__aexit__` clears all backends and health state
- [x] `backend_ids` property returns frozen set of registered IDs
- [x] Hub is not thread-safe; each run gets its own instance

### Initialise

- [x] Decrypts Fernet-encrypted API key from secrets backend and builds backend object
- [x] Reads `fallback_backend_ids` from ORM row and populates `_fallbacks`
- [x] Handles ORM rows missing `fallback_backend_ids` attribute (pre-migration) without crashing
- [x] Raises `BackendDecryptError` on decryption failure
- [x] Raises `ValueError` for unknown provider (not in built-in list or plugin registry)
- [x] Raises `ValueError` when credentials lack `api_key`
- [ ] Validates fallback ID refers to a registered backend at initialise time

### Health Check

- [x] Pings backend with minimal inference call, updates health state
- [x] Returns `HealthResult(ok=True)` on success
- [x] Returns `HealthResult(ok=False)` on timeout (sets unhealthy)
- [x] Returns `HealthResult(ok=False)` on exception (sets unhealthy, detail truncated to 500 chars)
- [x] Returns `HealthResult(ok=False, detail="Backend not registered")` for unknown ID
- [x] `mark_unhealthy()` explicitly sets health flag to False
- [x] Health check recovers a previously marked-unhealthy backend

### Failover — `get()`

- [x] Returns healthy primary directly
- [x] Raises `BackendUnavailableError` when primary is unhealthy and no fallbacks configured
- [x] Fails over to first healthy fallback when primary is unhealthy
- [x] Tries fallbacks in configured order
- [x] Raises `BackendUnavailableError` when all fallbacks are unhealthy
- [x] Skips unregistered fallback IDs gracefully
- [x] Calls audit logger with `event_type: "model_failover"`, `primary_id`, `fallback_id` on failover
- [x] Does not call audit logger when primary is healthy

### Failover — `get_with_rotation()`

- [x] Returns primary unrotated when healthy
- [x] Rotates to configured fallback when primary unhealthy
- [x] Scans all registered backends when no fallback list configured
- [ ] Raises `BackendUnavailableError` for unregistered ID (same exception as unhealthy, not `BackendNotFoundError`)
- [x] Does not emit audit events (no audit_logger parameter)

### API — CRUD

- [x] Create endpoint accepts `fallback_backend_ids` as list of UUIDs
- [x] Update endpoint accepts `fallback_backend_ids`
- [x] Response includes `fallback_backend_ids` field
- [x] Response excludes credentials ciphertext — only `has_credentials` boolean
- [x] Credentials encrypted with Fernet before storage
- [x] 404 on get/update/delete of unknown backend
- [x] 401/403 on unauthenticated list

### Error Handling

- [ ] List returns 501 when model_backends table does not exist (ProgrammingError)
- [ ] Create returns 501 when model_backends table does not exist (ProgrammingError)
- [ ] Get returns 501 when model_backends table does not exist (ProgrammingError)
- [ ] Update returns 501 when model_backends table does not exist (ProgrammingError)
- [ ] Delete returns 501 when model_backends table does not exist (ProgrammingError)

### Database

- [x] Migration adds `fallback_backend_ids` JSON column to `model_backends`
- [x] Column is nullable (backends without fallbacks)
- [x] Downgrade drops the column
- [ ] Constraint or FK to validate fallback IDs reference existing ModelBackend rows
- [ ] Deletion protection: deleting a backend referenced as a fallback elsewhere

### BDD

BDD step definitions exist in `steps/test_model_backends.py` — all 5 feature files (`backend_selection`, `backend_health_check`, `backend_crud`, `backend_error_handling`, `rate_limiting`) are wired with real given/when/then steps. However, steps simulate backend resolution at the mock level, not via actual API calls.

- [ ] Scenario: healthy primary returns immediately
- [ ] Scenario: unhealthy primary with healthy fallback
- [ ] Scenario: all backends unhealthy returns error
- [ ] Scenario: fallback list order is respected
- [ ] Scenario: audit event emitted on failover
- [ ] Scenario: removing fallback from update removes it from rotation

### Edge Cases

- [ ] Primary unhealthy, fallback list empty — raises `BackendUnavailableError`
- [ ] Primary unhealthy, fallback list contains unregistered IDs — skipped
- [ ] Primary unhealthy, fallback contains self-referencing ID — skipped
- [ ] All registered backends unhealthy — falls through both steps, raises
- [ ] `get_with_rotation` with empty hub raises on healthy check
- [ ] Concurrent health checks on same backend (not thread-safe — races on `_healthy`)
- [ ] Plugin provider build failure during initialise — partial initialise state

### Security

- [x] Credentials never exposed in API responses
- [x] Fernet encryption at rest
- [x] Audit trail on failover events

## Known Gaps
- No ProgrammingError/501 catch on any of the 5 API routes (violates established pattern)
- Unhandled ValueError from `_build_backend()` in create route — invalid provider causes 500, not 422
- No duplicate name check — backend_crud.feature expects 409 but code allows duplicates
- No fallback ID validation — API accepts any UUID, doesn't verify they reference existing backends
- No deletion protection for backends referenced as fallbacks
- No audit events on CRUD operations (create/update/delete)
- 6 DB columns not exposed via API: owner_team_id, status, cost_tracking, currency, last_health_check_at, last_health_check_error
- `get_with_rotation()` lacks audit logger parameter — scan-all-fallbacks path emits no audit events
- `_make_backend()` test helper sets `created_by` instead of `account_id` — causes mock attribute mismatch
- No concurrent-access guards on `_healthy` dict (documented not thread-safe) 