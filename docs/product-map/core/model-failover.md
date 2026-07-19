---
id: feat-core-model-failover
prd: 8.1
delivery-tasks: [task-nv9-model-failover]
bdd:
  - backend/tests/bdd/features/model_backends/backend_selection.feature
  - backend/tests/bdd/features/model_backends/backend_health_check.feature
  - backend/tests/bdd/features/model_backends/backend_crud.feature
  - backend/tests/bdd/features/model_backends/backend_error_handling.feature
  - backend/tests/bdd/features/model_backends/rate_limiting.feature
code:
  - backend/src/modulo/core/model_backend_hub/__init__.py
  - backend/src/modulo/api/routes/model_backends.py
  - backend/src/modulo/db/models/model_backend.py
  - backend/src/modulo/db/crud/model_backend.py
  - backend/src/modulo/db/migrations/versions/0003_v2_pipeline_runtime.py
unit-tests:
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
- [x] Raises `BackendUnavailableError` for unregistered ID (same exception as unhealthy, not `BackendNotFoundError`)
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

- [x] List returns 501 when model_backends table does not exist (ProgrammingError)
- [x] Create returns 501 when model_backends table does not exist (ProgrammingError)
- [x] Get returns 501 when model_backends table does not exist (ProgrammingError)
- [x] Update returns 501 when model_backends table does not exist (ProgrammingError)
- [x] Delete returns 501 when model_backends table does not exist (ProgrammingError)
- [x] All 5 routes catch SQLAlchemyError → 503 Service Unavailable (non-migration DB errors)
- [x] All 5 routes catch generic Exception → 500 Internal Server Error (Python-level errors)
- [x] `_validate_provider` logs plugin registry failures instead of silently swallowing them

### Database

- [x] Migration adds `fallback_backend_ids` JSON column to `model_backends`
- [x] Column is nullable (backends without fallbacks)
- [x] Downgrade drops the column
- [x] Empty fallback_backend_ids list round-trips as `[]` not `None` in API response
- [ ] Constraint or FK to validate fallback IDs reference existing ModelBackend rows
- [ ] Deletion protection: deleting a backend referenced as a fallback elsewhere

### BDD

BDD step definitions exist in `steps/test_model_backends.py` — all 5 feature files (`backend_selection`, `backend_health_check`, `backend_crud`, `backend_error_handling`, `rate_limiting`) are wired with real given/when/then steps. However, steps simulate backend resolution at the mock level, not via actual API calls.

#### backend_selection.feature
- [x] Scenario: Node uses configured backend override (real step defs)
- [x] Scenario: Default backend used when no override exists
- [x] Scenario: Fallback chain activates on primary failure
- [x] Scenario: Unknown backend returns error

#### backend_health_check.feature
- [x] Scenario: Save-time validation blocks on unhealthy backend
- [x] Scenario: Run-time validation blocks on unhealthy backend
- [x] Scenario: Healthy backend passes validation
- [x] Scenario: Never-checked backend passes validation

#### backend_crud.feature
- [x] Scenario: Create a model backend with valid data
- [x] Scenario: List model backends returns backends in the org
- [x] Scenario: Get a specific model backend by ID
- [x] Scenario: Update a model backend name and model ID
- [x] Scenario: Update a model backend API key
- [x] Scenario: Delete a model backend
- [x] Scenario: Get non-existent backend returns 404
- [x] Scenario: Delete non-existent backend returns 404
- [ ] Scenario: Create backend with duplicate name returns error → code has no duplicate name check (409 expected, actual 201)
- [ ] Scenario: Create backend with invalid provider returns error → code has no provider validation at create time (422 expected, actual 201)
- [x] Scenario: Create backend with missing required fields returns error

#### backend_error_handling.feature
- [x] Scenario: Invalid API key on invoke returns auth error
- [x] Scenario: Network error on invoke returns service error
- [x] Scenario: Rate-limited response from provider is handled
- [x] Scenario: Timeout during invoke returns timeout error
- [x] Scenario: Unknown provider returns configuration error
- [x] Scenario: Empty response from provider is handled

#### Missing BDD (unit test coverage exists)
- [ ] Scenario: healthy primary returns immediately (covered by test_get_returns_healthy_primary)
- [ ] Scenario: unhealthy primary with healthy fallback (covered by test_get_fails_over_to_healthy_fallback)
- [ ] Scenario: all backends unhealthy returns error (covered by test_get_raises_when_all_fallbacks_unhealthy)
- [ ] Scenario: fallback list order is respected (covered by test_get_tries_fallbacks_in_order)
- [ ] Scenario: audit event emitted on failover (covered by test_get_calls_audit_logger_on_failover)
- [ ] Scenario: removing fallback from update removes it from rotation (no test coverage)

### Edge Cases

- [x] Primary unhealthy, fallback list empty — raises `BackendUnavailableError` (test_get_raises_when_unhealthy_no_fallback)
- [x] Primary unhealthy, fallback list contains unregistered IDs — skipped (test_get_skips_unregistered_fallback)
- [x] Primary unhealthy, fallback contains self-referencing ID — skipped, does not crash (test_initialise_self_referencing_fallback_does_not_crash)
- [x] All registered backends unhealthy — falls through both steps, raises (test_get_raises_when_all_fallbacks_unhealthy)
- [x] `get_with_rotation` with empty hub raises `BackendUnavailableError` (test_get_with_rotation_empty_hub_raises)
- [ ] Concurrent health checks on same backend (not thread-safe — races on `_healthy`)
- [x] Plugin provider build failure during initialise — propagates to caller (test_initialise_plugin_build_failure_propagates)

### Security

- [x] Credentials never exposed in API responses
- [x] Fernet encryption at rest
- [x] Audit trail on failover events

## Known Gaps
- ~~No ProgrammingError/501 catch on any of the 5 API routes (violates established pattern)~~ **RESOLVED** — all 5 routes have ProgrammingError→501 catch with unit tests
- ~~No duplicate name check~~ **RESOLVED** — create route has duplicate name check returning 409 with unit test
- ~~No provider validation at API level~~ **RESOLVED** — create route has `_validate_provider()` returning 422 with unit tests
- No unique constraint on `(organisation_id, name)` in DB schema — duplicates silently allowed
- Fallback ID validation at create time not implemented — API accepts any UUID without verifying it references an existing backend
- No deletion protection for backends referenced as fallbacks by other backends
- No audit events on CRUD operations (create/update/delete)
- 6 DB columns not exposed via API: owner_team_id, status, cost_tracking, currency, last_health_check_at, last_health_check_error
- `get_with_rotation()` has `audit_logger` parameter but scan-all-fallbacks path does not emit audit events (only configured-fallback path does)
- No concurrent-access guards on `_healthy` dict (documented not thread-safe)
- 5 BDD scenarios only covered by unit tests, not wired as BDD step definitions (healthy primary, unhealthy+fallback, all unhealthy, fallback order, audit event)
- 1 BDD scenario has zero coverage: "removing fallback from update removes it from rotation"
- ~~3 edge cases lack unit tests: self-referencing fallback ID, empty hub rotation, plugin build failure~~ **RESOLVED** — all 3 now tested

## QA History
- 2026-07-08: Cross-cutting QA (index 267) — Fixed CRITICAL — added `except Exception → 500` catches with `except HTTPException: raise` guard to all 5 CRUD routes in model_backends.py (previously missing generic exception guard — Python-level errors like TypeError, KeyError, ValueError from `_to_response` processing propagated as raw 500 to CatchAllMiddleware). Fixed MAJOR — `_validate_provider` bare `except Exception: pass` replaced with `logger.warning` so plugin registry failures are visible in logs (previously silently swallowed). Fixed MAJOR — `_to_response` changed `if raw_fallback_ids:` to `if raw_fallback_ids is not None:` so empty `[]` list round-trips correctly (previously returned `None` for empty lists). Added 6 new unit tests in `test_model_backends_endpoint.py` (5× Exception→500 for all routes + 1× empty fallback_ids round-trip). Updated product map Error Handling section (3 new [x] checkboxes) and Database section (1 new [x] checkbox). All 33 model backend endpoint tests + 13 hub failover tests pass. Merged to main at v0.3.218. Status: partial.
- 2026-07-05: Cross-cutting QA (index 146) — Fixed 5 stale ProgrammingError→501 checkboxes [ ]→[x]; removed stale Known Gap #1; added 3 missing BDD feature files to frontmatter; documented BDD scenarios from all 5 feature files with coverage status; added duplicate name check (409) and provider validation (422) to create route; added 4 unit tests for duplicate name + provider validation; marked stale Edge Case boxes [x] where unit tests exist; added QA History section.
- Unhandled ValueError from `_build_backend()` in create route — invalid provider causes 500, not 422
- No duplicate name check — backend_crud.feature expects 409 but code allows duplicates
- No fallback ID validation — API accepts any UUID, doesn't verify they reference existing backends
- No deletion protection for backends referenced as fallbacks
- No audit events on CRUD operations (create/update/delete)
- 6 DB columns not exposed via API: owner_team_id, status, cost_tracking, currency, last_health_check_at, last_health_check_error
- `get_with_rotation()` has `audit_logger` parameter but scan-all-fallbacks path does not emit audit events (only configured-fallback path does)
- `_make_backend()` test helper sets `created_by` instead of `account_id` — causes mock attribute mismatch
- No concurrent-access guards on `_healthy` dict (documented not thread-safe)
- ~~3 edge cases lack unit tests~~ **RESOLVED** (index 174)
- 2026-07-12: Cross-cutting QA (Round 2) — Fixed 5× B904 in `model_backends.py`: `IntegrityError` handlers now use `raise ... from None` instead of bare `raise` (list, create, get, update, delete endpoints). Fixed stale frontmatter: migration file path corrected from `0035_model_fallback.py` (does not exist) to `0003_v2_pipeline_runtime.py` (actual file). Fixed stale Known Gap #9: `get_with_rotation()` description updated to reflect it now has `audit_logger` parameter; the actual gap (scan-all-fallbacks path emitting no audit events) is preserved. Verified no CancelledError issues (all `except Exception:` blocks correctly don't catch `BaseException` in Python 3.12). Verified no dead code (all imports and variables used). Status: partial.
- 2026-07-04: Cross-cutting QA (index 174) — Changed get_with_rotation() to raise BackendUnavailableError for unregistered IDs (matching product map spec); added SQLAlchemyError→503 catch to all 5 CRUD routes (matching established pattern); fixed integration test `created_by`→`account_id` param name (broken since column rename); added unit tests: empty hub rotation, self-referencing fallback, plugin build failure, get_with_rotation unregistered ID, 5 SQLAlchemyError→503 endpoint tests; updated product map checkboxes (5 [ ]→[x]); resolved 3 Known Gaps (unchecked edge cases).
