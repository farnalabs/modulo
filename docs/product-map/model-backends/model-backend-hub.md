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

The runtime registry and resolution layer for model backends — parallel to ConnectorHub. Manages backend instantiation, credential decryption (one-decrypt-per-run), and runtime resolution of `model_backend_id` references. The Hub is the runtime counterpart of the ModelBackend entity management described in `feat-model-backends-management`.

## Behaviours

### Registry — backend instantiation and lifecycle

- [x] All model backends for an org registered in the ModelBackendHub — via hub.initialise()
- [ ] Agents reference a `model_backend_id` instead of embedding provider/model config
- [ ] `model_backend_id` resolved to a concrete `ModelBackendBase` instance at run-start
- [ ] Same resolution pattern as ConnectorHub (ConnectorHub and ModelBackendHub are parallel)
- [ ] Backend instances cached per org with configurable TTL (default: 5 minutes)
- [ ] Cache invalidation on credential update or status change

### Credential Decryption — one-decrypt-per-run

- [x] Credentials decrypted once at run-start during ModelBackend initialisation for that run
- [x] Decrypted backend instance held in run-scoped context object
- [x] One Fernet decrypt call per ModelBackend per run — not per node invocation
- [x] Run-scoped context never enters LangGraph state, checkpoint blobs, OTel spans, or logs (§6.13 credential-in-state rule)
- [x] Decrypted backend discarded at run end
- [ ] Balances performance (one decrypt) with shortest practical credential lifetime

### Runtime Resolution — model_id pinning

- [ ] `model_id` resolved from `PipelineSnapshot.model_backend_pins_json`
- [ ] Not from current ModelBackend entity — ensures consistency across pauses/resumes
- [ ] Operator updates take effect only on future runs (new snapshots)
- [ ] Pinned `model_id` that no longer exists in pricing config → cost falls back to zero with logged warning

### Pre-run Health Check — gate run start on backend health

- [ ] All referenced model backends health-checked before run start
- [ ] Failed check surfaces as pre-run error with named failure
- [ ] Error types: `credential_expired`, `endpoint_unreachable`, `quota_exceeded`, `model_not_found`
- [ ] Run blocked until resolved
- [ ] Health check respects 5-minute staleness bound

### ConnectorHub Parallel — shared architecture

- [x] `ModelBackendBase` is the ABC parallel to `ConnectorBase`
- [x] Both use the ABC pattern with `invoke()`/`stream()` (model) vs `query()`/`write()` (connector)
- [x] Stub test double exists for both subsystems (`StubModelBackend` parallel to `_GitHubActionsTestDouble`)
- [ ] Hub registration/discovery pattern matches ConnectorHub — not yet implemented
- [ ] Capability-based resolution not applicable (model backends have no capability model)

### Edge Cases and Error States

- [ ] Referencing a non-existent `model_backend_id` in a pipeline returns validation error at save time
- [ ] Referencing a deprecated backend in a new agent definition returns validation error
- [x] Decrypt failure at run-start — Fernet key mismatch, corrupted ciphertext
- [ ] All referenced backends must pass health check before run start — single failure blocks entire run
- [ ] Backend becomes unreachable mid-run — current run continues, error logged, subsequent runs blocked
- [ ] Concurrent credential rotation and Hub cache — stale cache invalidated on rotation

## Known Gaps

- [ ] **No pre-run health check**: no gate before run start that validates model backend health
- [ ] **No `model_backend_pins_json`**: PipelineSnapshot has no field for pinned model backend resolution
- [ ] **No caching**: no org-scoped backend instance cache
- [ ] **No Hub API endpoint**: no route for listing available backends per org
- [ ] **No BDD tests**: no feature file exists for the Hub
- **ConnectorHub pattern not replicated**: ModelBackendHub exists but is NOT yet wired into the run execution pipeline (node_runner.py has a TODO comment). The Hub is used for schema inference (routes/schemas.py) but not for pipeline runs.

## QA History

- 2026-07-02 (improve-architecture index 54): Cross-cutting QA pass 1. Updated frontmatter (added code path for Hub implementation, added 2 unit test file refs). Marked 7 stale [ ]→[x] behaviours (registration, decryption, run-scoped lifecycle, ABC pattern, stub test double). Removed 3 stale known gaps (No Hub implementation, No credential decryption, No unit tests). Updated ConnectorHub pattern gap description to reflect current state. All Hub unit tests pass.
