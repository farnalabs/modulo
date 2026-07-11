---
id: feat-model-backends-hub
prd: 8.1
delivery-tasks: []
bdd: []
unit-tests:
  - backend/tests/unit/model_backend_hub/test_hub.py
  - backend/tests/unit/core/model_backend_hub/test_failover.py
code:
  - backend/src/modulo/model_backends/__init__.py
  - backend/src/modulo/model_backends/base.py
  - backend/src/modulo/core/model_backend_hub/__init__.py
depends-on:
  - feat-model-backends-management
status: partial
---

# Model Backend Hub

The runtime registry and resolution layer for model backends — parallel to
ConnectorHub. Manages backend instantiation, credential decryption
(one-decrypt-per-run), and runtime resolution of `model_backend_id`
references. The Hub is the runtime counterpart of the ModelBackend entity
management described in `feat-model-backends-management`.

## Behaviours

### Registry — backend instantiation and lifecycle

- [x] All model backends for an org registered in the ModelBackendHub — via hub.initialise()
- [x] Agents reference a `model_backend_id` instead of embedding provider/model config — AgentCreate/FastAPI schema has model_backend_id UUID; pipeline graph references resolution captures it as model_backend_pins
- [x] `model_backend_id` resolved to a concrete `ModelBackendBase` instance at run-start — hub.initialise() calls _build_backend() per row, hub.get(backend_id) returns the instance
- [x] Same resolution pattern as ConnectorHub (ConnectorHub and ModelBackendHub are parallel) — both use async context manager, initialise(rows), get(id)
- [ ] Backend instances cached per org with configurable TTL (default: 5 minutes) — Hub holds backends only for the lifetime of a run; no cross-run org-scoped cache
- [ ] Cache invalidation on credential update or status change — no cache exists yet

### Credential Decryption — one-decrypt-per-run

- [x] Credentials decrypted once at run-start during ModelBackend initialisation for that run
- [x] Decrypted backend instance held in run-scoped context object
- [x] One Fernet decrypt call per ModelBackend per run — not per node invocation
- [x] Run-scoped context never enters LangGraph state, checkpoint blobs, OTel spans, or logs (§6.13 credential-in-state rule)
- [x] Decrypted backend discarded at run end — `__aexit__` clears `_backends`, `_healthy`, `_fallbacks`
- [x] Decrypted credential references eagerly cleared — `del raw_str, creds` in initialise() after _build_backend returns

### Runtime Resolution — model_id pinning

- [x] `model_id` resolved from `PipelineSnapshot.model_backend_pins_json` — `_resolve_graph_references()` in `pipelines.py` creates model_backend_pins; stored in snapshot at creation time; graph validator reads from pins at save and run time
- [x] Not from current ModelBackend entity — ensures consistency across pauses/resumes
- [x] Operator updates take effect only on future runs (new snapshots)
- [ ] Pinned `model_id` that no longer exists in pricing config → cost falls back to zero with logged warning — no pricing config integration exists

### Pre-run Health Check — gate run start on backend health

- [x] All referenced model backends health-checked before run start — `_check_model_backends()` in `graph_validator/__init__.py`
- [x] Failed check surfaces as pre-run error with named failure — produces `MODEL_BACKEND_UNHEALTHY`, `MODEL_BACKEND_NOT_FOUND`, `MODEL_BACKEND_INACTIVE`
- [ ] Error types: `credential_expired`, `endpoint_unreachable`, `quota_exceeded`, `model_not_found` — graph validator only produces `MODEL_BACKEND_UNHEALTHY` with string description from `last_health_check_error`; no typed error codes
- [x] Run blocked until resolved — graph validator returns error list; pipeline creation is rejected
- [ ] Health check respects 5-minute staleness bound — no caching; no staleness tracking on health results

### ConnectorHub Parallel — shared architecture

- [x] `ModelBackendBase` is the ABC parallel to `ConnectorBase`
- [x] Both use the ABC pattern with `invoke()`/`stream()` (model) vs `query()`/`write()` (connector)
- [x] Stub test double exists for both subsystems (`StubModelBackend` parallel to `_GitHubActionsTestDouble`)
- [x] Hub registration/discovery pattern matches ConnectorHub — `initialise()` + `get()` + async context manager
- [ ] Capability-based resolution not applicable (model backends have no capability model) — N/A by design

### Error Handling

- [x] Backend not registered → `BackendNotFoundError(Exception)` with `.backend_id` attribute
- [x] Backend (and all fallbacks) unhealthy → `BackendUnavailableError(Exception)` with detail string
- [x] Credential decrypt failure → `BackendDecryptError(ValueError)` raised from `KeyError` from secrets backend
- [x] Unknown provider in `_build_backend` → `ValueError("Unknown model backend provider: {provider!r}")`
- [x] Missing `api_key` in credentials dict → `ValueError` at provider construction time
- [x] Missing `project` in Vertex AI credentials → `ValueError("Missing 'project' in credentials for provider 'vertexai'")`
- [x] Missing `azure_endpoint` in Azure OpenAI credentials → `ValueError("Missing 'azure_endpoint' in credentials for provider 'azure_openai'")`
- [x] Missing `project_id` in WatsonX credentials → `ValueError("Missing 'project_id' in credentials for provider 'watsonx'")`
- [x] Health check on unregistered backend → `HealthResult(ok=False, detail="Backend not registered")`
- [x] Plugin provider fallback: registered provider → built via plugin registry
- [x] Plugin provider fallback: not registered → `ValueError("Unknown model backend provider: {provider!r}")`
- [x] Missing `aws_access_key_id`/`aws_secret_access_key` in Bedrock credentials → `ValueError("Missing 'aws_access_key_id' in credentials for provider 'bedrock'")` (confirmed in `_build_backend()`) @ 2026-07-05

### Resilience & Integration Robustness

- [x] Fallback rotation: primary unhealthy → configured fallbacks tried in order
- [x] Fallback rotation: no fallbacks configured → all registered backends scanned for any healthy instance
- [x] Fallback rotation: unregistered fallback ID skipped gracefully
- [x] Missing `fallback_backend_ids` attribute on ORM row → `getattr` with `None` default prevents crash
- [x] Health check exceptions caught and mapped to `HealthResult(ok=False)`
- [x] Hub declared not thread-safe; each run gets own instance — acceptable isolation
- [x] `del raw_str, creds` after backend construction — credential sanitisation
- [ ] No retry with backoff on health check failure — single attempt, no retry
- [ ] No mid-run monitoring — `mark_unhealthy()` exists but no automatic periodic re-check
- [x] Warning/error logging exists throughout Hub (invalid fallback, backend init failure, fallback skip, audit logger failure) — 11+ `logger.warning()`, `logger.error()`, `logger.exception()` calls
- [ ] Failover events not surfaced via `audit_logger` callback — `get_with_rotation()` has an `audit_logger` parameter but it's only called on the chosen backend, not on fallback rotation decisions
- [ ] Hub not yet wired into run execution pipeline — `node_runner.py` `_node()` is a stub

### Edge Cases

- [x] Zero backends registered → `backend_ids` returns empty frozenset; `get()` raises `BackendNotFoundError`
- [x] One backend → normal operation
- [x] Many backends → `backend_ids` property verified
- [x] Empty credentials → `ValueError` from `_build_backend` on missing `api_key`
- [x] Fallback list with unregistered IDs → skipped silently
- [x] Health check after `mark_unhealthy` then `health_check` passes → recovery works (tested)
- [x] Double-invoke of `__aexit__` → dict `.clear()` on already-cleared dicts is a no-op
- [x] `initialise([])` → no backends registered, no crash
- [x] Invalid UUID in `fallback_backend_ids` → `uuid.UUID()` raises `ValueError`, caught and logged as warning, fallback skipped gracefully
- [ ] Fallback list includes primary ID → primary already known unhealthy, would be skipped; no test
- [ ] Fallback scan includes unrelated backends (different provider/model) when no fallbacks configured — documented in `get_with_rotation()` docstring
- [ ] Concurrent unregistration during `get_with_rotation()` scan — iterates `_backends.items()` while another task could modify the dict; not thread-safe by design

## Known Gaps

- [ ] **No org-scoped caching**: Hub holds backends only for the lifetime of a run (cleared in `__aexit__`). No cross-run cache with TTL. Every run pays the decrypt + `_build_backend` cost.
- [ ] **No Hub API endpoint**: no `/api/v1/model-backend-hub/...` route. Backends are resolved internally; no REST interface for Hub operations.
- [ ] **No BDD tests**: no `.feature` files exist for the Hub specifically. The management-side BDD files (`backend_crud.feature`, etc.) cover the CRUD API but not Hub logic.
- [ ] **Hub not wired into run execution**: `node_runner.py` `_node()` is still a stub that records artifacts without calling any LLM. The Hub is used for schema inference (`routes/schemas.py`) but not for pipeline runs.
- [ ] **No typed error codes for health check failures**: graph validator returns `MODEL_BACKEND_UNHEALTHY` with a free-text `last_health_check_error` string instead of typed codes (`credential_expired`, `endpoint_unreachable`, `quota_exceeded`, `model_not_found`).
- [ ] **No health check result staleness bound**: health checks run each time; no 5-minute cache window.
- [ ] **No retry with backoff**: health check runs once per call. No retry logic for transient failures.
- [ ] **No mid-run monitoring**: no periodic health re-check during a run. `mark_unhealthy()` exists but is caller-driven; no automatic detection of unreachability.
- [ ] **Failover events not surfaced via `audit_logger` callback**: Hub has extensive `logger.warning()`, `logger.error()`, and `logger.exception()` calls (11+ sites), but `get_with_rotation()` only calls the `audit_logger` callback on the chosen backend, not on fallback rotation decisions.
- [ ] **No pricing config integration**: pinned `model_id` cost tracking not implemented.
- [ ] **`get_with_rotation()` fallback-scan returns unrelated backends**: when no fallbacks are configured and primary is unhealthy, any registered backend (different provider, different model) may be returned.

## QA History

- 2026-07-06 (qa-iterate prodmap model-backends): Code verification pass. Corrected exception base classes (`BackendNotFoundError(KeyError)`→`BackendNotFoundError(Exception)`, `BackendUnavailableError(RuntimeError)`→`BackendUnavailableError(Exception)`). Corrected `_build_backend` error message to `ValueError("Unknown model backend provider: {provider!r}")`. Corrected UUID fallback handling — caught via `try/except ValueError`, logged as warning, not uncaught. Corrected logging claim — Hub has 11+ `logger.warning/error/exception` calls; real gap is failover events not surfaced via `audit_logger` callback.
- 2026-07-05 (qa-iterate prodmap model-backends): Fixed duplicate code path (`base.py` listed twice in `code:` frontmatter). Corrected Bedrock credential validation claim from `[ ]` (uncaught `KeyError`) to `[x]` (handled `ValueError` in `_build_backend()`). Removed stale Known Gap "Bedrock credentials not validated".
- 2026-07-04 (improve-architecture index 173): Cross-cutting QA pass 2. Verified all unchecked behaviours against code: marked 13 behaviours [ ]→[x] (agent model_backend_id ref, run-start resolution, ConnectorHub parallel, model_id pinning, entity independence, operator-update-on-new-runs, pre-run health check, named failure, run blocking, non-existent ID validation, all-backends-pass, Hub registration pattern, one-decrypt balance). Added Error Handling (15 items), Resilience & Integration Robustness (10 items), and Edge Cases (10 items) sections. Updated Known Gaps: removed stale "no pre-run health check", "no model_backend_pins_json" (both implemented); added 7 new gaps (typed error codes, staleness bound, retry, mid-run monitoring, Hub logging, Bedrock credential validation, fallback-scan unrelated backends). Created website doc stub. **Note 2026-07-06**: The "Hub logging" gap actually overstates the issue — Hub has 11+ logger calls (warning/error/exception); the real gap is that `get_with_rotation()` doesn't surface failover events via `audit_logger` callback.
- 2026-07-02 (improve-architecture index 54): Cross-cutting QA pass 1. Updated frontmatter (added code path for Hub implementation, added 2 unit test file refs). Marked 7 stale [ ]→[x] behaviours (registration, decryption, run-scoped lifecycle, ABC pattern, stub test double). Removed 3 stale known gaps (No Hub implementation, No credential decryption, No unit tests). Updated ConnectorHub pattern gap description to reflect current state. All Hub unit tests pass.
