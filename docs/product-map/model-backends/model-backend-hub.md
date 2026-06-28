---
id: feat-model-backends-hub
prd: 8.1
delivery-tasks: []
bdd: []
unit-tests: []
code:
  - backend/src/modulo/model_backends/__init__.py
  - backend/src/modulo/model_backends/base.py
depends-on:
  - feat-model-backends-management
status: partial
---

# Model Backend Hub

The runtime registry and resolution layer for model backends — parallel to ConnectorHub. Manages backend instantiation, credential decryption (one-decrypt-per-run), and runtime resolution of `model_backend_id` references. The Hub is the runtime counterpart of the ModelBackend entity management described in `feat-model-backends-management`.

## Behaviours

### Registry — backend instantiation and lifecycle

- [ ] All model backends for an org registered in the ModelBackendHub — not yet implemented
- [ ] Agents reference a `model_backend_id` instead of embedding provider/model config
- [ ] `model_backend_id` resolved to a concrete `ModelBackendBase` instance at run-start
- [ ] Same resolution pattern as ConnectorHub (ConnectorHub and ModelBackendHub are parallel)
- [ ] Backend instances cached per org with configurable TTL (default: 5 minutes)
- [ ] Cache invalidation on credential update or status change

### Credential Decryption — one-decrypt-per-run

- [ ] Credentials decrypted once at run-start during ModelBackend initialisation for that run
- [ ] Decrypted backend instance held in run-scoped context object
- [ ] One Fernet decrypt call per ModelBackend per run — not per node invocation
- [ ] Run-scoped context never enters LangGraph state, checkpoint blobs, OTel spans, or logs (§6.13 credential-in-state rule)
- [ ] Decrypted backend discarded at run end
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
- [ ] Decrypt failure at run-start — Fernet key mismatch, corrupted ciphertext
- [ ] All referenced backends must pass health check before run start — single failure blocks entire run
- [ ] Backend becomes unreachable mid-run — current run continues, error logged, subsequent runs blocked
- [ ] Concurrent credential rotation and Hub cache — stale cache invalidated on rotation

## Known Gaps

- [ ] **No Hub implementation exists**: ModelBackendHub is a design concept with no code — no registry, no decryption logic, no run-scoped context wiring
- [ ] **No credential decryption at run-start**: all backends are currently instantiated with plaintext credentials passed at construction time
- [ ] **No pre-run health check**: no gate before run start that validates model backend health
- [ ] **No `model_backend_pins_json`**: PipelineSnapshot has no field for pinned model backend resolution
- [ ] **No caching**: no org-scoped backend instance cache
- [ ] **No Hub API endpoint**: no route for listing available backends per org
- [ ] **No BDD tests**: no feature file exists for the Hub
- [ ] **No unit tests**: no Hub test file exists
- [ ] **ConnectorHub pattern not replicated**: ModelBackendHub was deferred; ConnectorHub credential lifetime was specified first
