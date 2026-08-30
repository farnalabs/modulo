---
id: feat-model-backends
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/model_backends.py
  - backend/src/modulo/core/model_backend_hub
  - backend/src/modulo/model_backends/base.py
  - backend/src/modulo/model_backends/openai
  - backend/src/modulo/model_backends/anthropic
  - backend/src/modulo/db/models/model_backend.py
unit-tests:
  - backend/tests/unit/model_backend_hub/test_hub.py
  - backend/tests/unit/model_backends/test_base.py
  - backend/tests/unit/model_backends/test_openai.py
  - backend/tests/unit/model_backends/test_anthropic.py
  - backend/tests/unit/model_backends/test_shared.py
  - backend/tests/unit/api/test_model_backends_endpoint.py
  - backend/tests/unit/api/test_model_backends_pipeline_refs.py
bdd:
  - backend/tests/bdd/features/model_backends/backend_crud.feature
  - backend/tests/bdd/features/model_backends/backend_selection.feature
  - backend/tests/bdd/features/model_backends/backend_error_handling.feature
  - backend/tests/bdd/features/model_backends/backend_health_check.feature
  - backend/tests/bdd/features/model_backends/health_check.feature
  - backend/tests/bdd/features/model_backends/configure.feature
  - backend/tests/bdd/features/model_backends/hub.feature
  - backend/tests/bdd/features/model_backends/rate_limiting.feature
  - backend/tests/bdd/features/model_backends/rotation.feature
  - backend/tests/bdd/steps/test_model_backends.py
  - backend/tests/bdd/steps/test_model_backend_hub.py
  - backend/tests/bdd/steps/test_alpha_model_backends.py
depends-on:
  - feat-auth
status: covered
---

# Model Backend Management

Model backends configure AI providers (`/admin/model-backends`, `/setup/model-backend/:id`).
Credentials are stored encrypted and never exposed in API responses (`has_credentials`
true with the key itself redacted). The `ModelBackendHub` (`core/model_backend_hub`) is the
runtime registry that resolves, health-checks and fails over registered backends per run,
selecting a healthy configured fallback or rotating across the org's healthy backends,
and provider adapters under `backend/src/modulo/model_backends/*` implement the
`BaseChatModel` contract with per-provider configuration.

## Behaviours

- [x] Model-backend CRUD under `/api/v1/model-backends`: create (201) with provider
      validation, list org-scoped backends, get, PATCH (name/model id/API key), delete
      (204); non-existent id 404, duplicate name 409, invalid provider / missing required
      fields / unknown fallback id 422 (`backend_crud.feature`)
- [x] The API key is never echoed in responses — `has_credentials: true` with the secret
      redacted (`backend_crud.feature`)
- [x] A backend referenced as another's fallback cannot be deleted (409)
      (`backend_crud.feature`)
- [x] Hub resolution: a healthy primary is served; an unhealthy primary fails over to its
      configured fallback; with no healthy candidate an unavailable error is raised; an
      unregistered backend raises a not-found error; a `model_failover` audit event records
      primary/fallback (`hub.feature`)
- [x] With no configured fallback, the hub rotates across the org's registered backends
      and emits the failover audit event; encrypted credentials are decrypted exactly once
      per backend per hub initialisation (`hub.feature`)
- [x] Rotation semantics at run time: a health check before each run selects the primary
      when healthy, the fallback otherwise, and an all-unhealthy assignment fails the run
      with `no_healthy_backend` (`rotation.feature`)
- [x] Per-provider adapters (openai, anthropic, and peers) share the `BaseChatModel`
      contract with configuration validation, health checks, error handling and rate
      limiting (`test_model_backends/*`, `model_backends/*.feature`)

## Known Gaps

- **Per-backend spend ceilings are not modelled here** — spend attribution and limits are
  owned by `feat-costs`; this entry tracks provider configuration and selection only.
- **Hub failover is per-process healthy-state** — a shared cross-worker health view of
  registered backends is not modelled; each worker re-reads backend state via the hub.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-model-backends`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/model_backends.py`,
  `core/model_backend_hub`, `backend/src/modulo/model_backends/*` and the model-backends
  BDD/unit suites. Status: covered.
