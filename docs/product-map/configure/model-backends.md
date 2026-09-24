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
- [x] `POST /api/v1/model-backends/{backend_id}/health-check` re-runs the PRD 8.1 check on
      demand against the decrypted stored credential and persists the result — returning
      `healthy` / `unhealthy` (+ auth-failure detail) / `not_applicable` with `checked_at`,
      clearing a sticky `last_health_check_error`; a cross-org caller is 404'd before any
      check, and the deterministic stub provider always reports `healthy`
      (`model_backends/health_check.feature`, steps in `test_alpha_model_backends.py`)

## Known Gaps

- **Per-backend spend ceilings are not modelled here** — spend attribution and limits are
  owned by `feat-costs`; this entry tracks provider configuration and selection only.
- **Hub failover is per-process healthy-state** — a shared cross-worker health view of
  registered backends is not modelled; each worker re-reads backend state via the hub.

## QA History
- 2026-09-20: **product-map review pass** — closed the "no standalone
  model-backend health endpoint exists" `@awaiting-implementation` gap. The four
  `model_backends/health_check.feature` scenarios now execute against the REAL
  `POST /api/v1/model-backends/{id}/health-check` route (PRD 8.1 re-check) with only the
  DB lookup, secret-decryption and post-commit persist seams patched: healthy → `healthy`,
  invalid API key → `unhealthy` + auth-failure detail, other-org caller → 404, stub →
  `healthy`. The stale `@awaiting-implementation` comments in the feature (which claimed
  the endpoint does not exist) were replaced by the real contract. Removed from
  `PINNED_AWAITING_IMPLEMENTATION`.
- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/model-backends`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-12: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/setup/model-backend/:id`: the whole-page view setup/ModelBackendSetupView.vue now
  maps to its owning view so a newly shipped testid on the model-backend setup flow
  can no longer silently stay invisible to Assistant's docs indexer / `/api/v1/manifest`.

- 2026-09-10: **product-map review pass** — registered the preset
  picker/testids of `AdminModelBackendsView.vue` (`admin-model-backends-*` preset and
  manual-entry controls) in the manifest `elements:` inventory and added the view to the
  reverse testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`),
  so a newly shipped panel on the model-backends page can no longer ship invisible to
  Assistant's docs indexer / `/api/v1/manifest`.
- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-model-backends`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/model_backends.py`,
  `core/model_backend_hub`, `backend/src/modulo/model_backends/*` and the model-backends
  BDD/unit suites. Status: covered.
