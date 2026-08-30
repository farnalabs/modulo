---
id: feat-model-backends
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/model_backends.py
  - backend/src/modulo/core/model_backend_hub/
  - backend/src/modulo/model_backends/
  - backend/src/modulo/db/crud/model_backend.py
  - backend/src/modulo/db/models/model_backend.py
  - frontend/src/views/AdminModelBackendsView.vue
  - frontend/src/views/setup/
unit-tests:
  - backend/tests/unit/api/test_model_backends_endpoint.py
  - backend/tests/unit/api/test_model_backends_pipeline_refs.py
  - backend/tests/unit/core/model_backend_hub/test_failover.py
  - backend/tests/unit/model_backend_hub/test_hub.py
  - backend/tests/unit/model_backends/
bdd:
  - backend/tests/bdd/features/model_backends/
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
  - feat-environments
  - feat-auth
status: covered
---

# Model Backend Management

Administer the "bring your own model" runtime surface (core principle §7 — "We
dispatch, we don't run agents"). Each model backend binds a provider (OpenAI,
Anthropic, Gemini, Bedrock, Vertex, local runtimes like Ollama/vLLM, and ~30
more in `model_backends/`), a model id, optional params, a base URL and an
encrypted API key. Backends are org-scoped, health-checked, selectable per node
with an org default and fallback chains, and served through the
`ModelBackendHub` resolver that fails over to healthy members. Surfaces:
`/admin/model-backends` and `/setup/model-backend/:id` (`feat-model-backends`).

## Behaviours

- [x] CRUD: `POST /api/v1/model-backends` creates (201) with provider/model/id
      config and an encrypted API key; list scoped to the org, detail by id, `GET
      /{id}/pipeline-references` lists referencing pipelines, `PATCH` updates
      (name/model/api key — updating the API key re-runs the health check on
      save), and `DELETE` removes (204). Unknown provider / missing fields /
      unknown fallback id are 422, duplicate names 409, missing ids 404, and a
      backend referenced as a fallback is refused at delete (409)
      (`backend_crud.feature`, `routes/model_backends.py`)
- [x] Credential hygiene: API keys are fernet-encrypted at rest, `has_credentials`
      reflects presence without exposing the value, and responses never contain
      the key (`backend_crud.feature` "API key is not exposed",
      `configure.feature` "API key is encrypted at rest")
- [x] Save-time and run-time health gating: a pipeline referencing an unhealthy
      backend is blocked with `MODEL_BACKEND_UNHEALTHY` (including the backend
      name and health-check detail) at both graph validation and run-creation;
      never-checked backends pass; save persists the health-check result and its
      error detail (`backend_health_check.feature`,
      `routes/model_backends.py` `_run_health_check_on_save*`)
- [x] Hub resolution + failover rotation: `ModelBackendHub` resolves a backend
      per run, decrypts credentials exactly once per run, serves the healthy
      primary, fails over to the configured fallback when unhealthy, scans all
      registered backends when no fallback is configured, raises an unavailable
      error when every candidate is unhealthy, a not-found error for an
      unregistered id, and emits a `model_failover` audit event on rotation
      (`hub.feature`, `rotation.feature`,
      `core/model_backend_hub/`, `tests/unit/model_backend_hub/test_hub.py`)
- [x] Selection model: per-node backend override wins, org default applies when
      no override exists, fallback chains activate on primary failure, and an
      unknown backend reference raises a resolution error
      (`backend_selection.feature`)
- [x] Invoke error taxonomy: auth / network / rate-limit (with retry-after) /
      timeout / empty-response failures surface as typed, actionable errors with
      the provider detail included; an unsupported provider is a configuration
      error (`backend_error_handling.feature`, `model_backends/base.py`)
- [x] Pipeline references guard: `GET /{id}/pipeline-references` lists the
      pipelines pinning a backend so operators can assess impact before delete
      (`test_model_backends_pipeline_refs.py`)
- [x] Break-glass integrity: create/update/delete are denied under the
      break-glass mint (`deny_break_glass_mint`) (`routes/model_backends.py`)
- [x] Frontend surfaces: `/admin/model-backends` CRUD + preview
      (`AdminModelBackendsView.vue`) and the `/setup/model-backend/:id` setup
      wizard (`frontend/src/views/setup/`)

## Known Gaps

- **No standalone health-check endpoint** — health is enforced at pipeline
  validation/run time and persisted on save; the standalone
  `@awaiting-implementation` scenarios in `health_check.feature` are not shipped.
- **Failover is rotation, not weighted selection** — the hub picks the first
  healthy backend; there is no cost/load-aware or A/B-weighted assignment.
- **Platform rate limiting is co-located but independently governed** — the
  `rate_limiting.feature` in this directory pins the global `/api/v1/runs`,
  webhook and MCP rate limits; those belong to the platform rate limiter
  (`feat-runtime`) rather than model-backend selection.

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for the registered manifest feature `feat-model-backends`,
  which had no `docs/product-map/` entry. Behaviours verified against
  `api/routes/model_backends.py`, `core/model_backend_hub/`,
  `model_backends/base.py`, `db/crud/model_backend.py`, the nine
  `model_backends/` BDD feature files and the hub/failover/endpoint unit suites.
   Status: covered.
