---
id: feat-environments
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/environment_profiles.py
  - backend/src/modulo/api/routes/environments.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/core/runtime_provider
  - backend/src/modulo/api/routes/admin.py
unit-tests:
  - backend/tests/integration/crud/test_environment_profiles.py
  - backend/tests/unit/api/test_environments.py
  - backend/tests/unit/graph_validator/test_environment_capabilities.py
bdd:
  - backend/tests/bdd/features/environments/environment_profiles.feature
depends-on:
  - feat-runtime
  - feat-pipelines
status: covered
---

# Environment Profiles

Reusable, org-scoped run-environment definitions (image, provider, capabilities,
network policy, persistence) served on the `/environments` **and**
`/environment-profiles` API surfaces, with per-run sandbox test (SSE), graph-validator
capability resolution, and org sandbox-concurrency control on `/admin/environments`
and `/admin/sandbox-concurrency`.

## Behaviours

- [x] A profile is createable with a name, description, provider_type, image_ref,
      capabilities, network_policy, initialisation_strategy, secret_refs,
      persistence_policy, owner_team_id and visibility
      (`backend/src/modulo/api/routes/environment_profiles.py#create_profile`,
      `backend/tests/integration/crud/test_environment_profiles.py`)
- [x] Profiles are listed paginated (page / page_size capped 1..100) and fetchable by
      id; a missing profile resolves 404 with message "Environment profile not found"
      (`list_profiles` / `get_profile`)
- [x] Update is a partial merge (PATCH semantics): omitted fields are left untouched,
      `capabilities` / `secret_refs` are re-serialised into their JSON columns, and a
      name collision resolves 409
- [x] Delete is a soft delete returning 204 (hard delete only via the immutable
      admin surface's reusable CRUD); restore is exposed on
      `/api/v1/environment-profiles/{id}/restore`
- [x] Every CRUD endpoint runs inside the RLS transaction (`set_rls_org` +
      `set_rls_user_context`) so profiles are org-isolated: a cross-org read or list
      sees nothing (404 / empty list, never an enumeration)
- [x] The GraphValidator resolves a snapshot's `environment_profile_id` into the
      profile's capabilities and fails closed with code `ENV_MISSING_CAPABILITIES`
      naming the missing capability (e.g. `egress:github.com`) when the profile does
      not cover every capability the agent requires
      (`backend/tests/unit/graph_validator/test_environment_capabilities.py`)
- [x] Profiles resolve against the RuntimeProviderHub by capabilities / provider hint;
      local is the default provider (only `local_docker` auto-registers and stays
      authoritative when no provider hint is set), and `e2b` resolves when the profile
      declares a `provider_hint` — see the hub-resolution scenarios in
      `backend/tests/bdd/features/environments/environment_profiles.feature`
- [x] `POST /api/v1/environments/{id}/test` provisions a sandbox from the profile,
      runs a hello command and destroys it, streaming a Server-Sent Events lifecycle
      (provisioning / provisioned / command_start / command_complete / destroying /
      destroyed, and a terminal `failed` event with cleanup on error)
- [x] WorkspaceLease lifecycle follows pending → provisioning → active → completed as
      the run progresses, and Provider create/destroy transitions workspace status
      running ↔ terminated (BDD scenarios in `environment_profiles.feature`)
- [x] Admin org sandbox concurrency is viewable/updatable on
      `GET/PUT /api/v1/admin/org/sandbox-concurrency` (value clamped 1..100), writing
      an `org.sandbox_concurrency_updated` audit event on success
      (`backend/src/modulo/api/routes/admin.py`)
- [x] The frontend surfaces the whole lifecycle: list + search + new/edit form
      (`/environment-profiles`, `/environment-profiles/new`,
      `/environment-profiles/:id/edit`), the admin environments page
      (`/admin/environments`) and admin sandbox-concurrency control
      (`/admin/sandbox-concurrency`) — testids enumerated in the product map

## Known Gaps

- Provider catalogue is fixed at `local_docker` + `e2b`; hub registration is
  env-driven and no plugin surface exists for third-party runtime providers.
- The sandbox test endpoint is contract-level (echo/exec only); it does not run the
  actual agent graph inside the workspace before release.

## QA History

- 2026-09-01: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-environments`, which previously had no
  `docs/product-map/` entry. Behaviours verified against
  `api/routes/environment_profiles.py`, `api/routes/environments.py`,
  `api/routes/admin.py`, `core/runtime_provider`, `core/graph_validator` and the
  env unit/integration/BDD suites. Status: covered.
