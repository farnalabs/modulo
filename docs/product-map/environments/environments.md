---
id: feat-environments
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/environment_profiles.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/core/runtime_provider
  - backend/src/modulo/api/routes/admin.py
  - frontend/src/views/runners
  - frontend/src/views/environment-profiles
  - frontend/src/stores/environmentProfiles.ts
unit-tests:
  - backend/tests/integration/crud/test_environment_profiles.py
  - backend/tests/unit/api/test_environment_profiles_routes.py
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
network policy, persistence) served on the `/api/v1/environment-profiles` API
surface, with per-profile sandbox test (SSE), graph-validator capability
resolution, and org sandbox-concurrency control. The UI lives on the Runners
page: the profiles tab is `/admin/runners/profiles` (create/edit at
`/admin/runners/profiles/new` and `/admin/runners/profiles/:id/edit`) and the
concurrency tab is `/admin/runners/concurrency`. (FAR-551 collapsed the duplicate
`/api/v1/environments` router + `/admin/environments` table view into this one
surface; FAR-591 D5 folded `/environment-profiles*` and `/admin/environments`
into the Runners page as redirects.)

## Behaviours

- [x] A profile is createable with a name, description, provider_type, image_ref,
      capabilities, network_policy, initialisation_strategy, secret_refs,
      persistence_policy, owner_team_id and visibility
      (`backend/src/modulo/api/routes/environment_profiles.py#create_profile`,
      `backend/tests/integration/crud/test_environment_profiles.py`)
- [x] Profiles are listed paginated (page / page_size capped 1..100) and fetchable by
      id; a missing profile resolves 404 with message "Environment profile not found"
      (`list_profiles` / `get_profile`)
- [x] Update (`PUT /api/v1/environment-profiles/{id}`) is a partial merge: omitted
      fields are left untouched, `capabilities` / `secret_refs` are re-serialised into
      their JSON columns, and a name collision resolves 409
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
- [x] `POST /api/v1/environment-profiles/{id}/test` provisions a sandbox from the
      profile, runs a hello command and destroys it, streaming a Server-Sent Events
      lifecycle (provisioning / provisioned / command_start / command_complete /
      destroying / destroyed, and a terminal `failed` event with cleanup on error);
      gated on `environment_profile.test` (operator)
- [x] WorkspaceLease lifecycle follows pending → provisioning → active → completed as
      the run progresses, and Provider create/destroy transitions workspace status
      running ↔ terminated (BDD scenarios in `environment_profiles.feature`)
- [x] Admin org sandbox concurrency is viewable/updatable on
      `GET/PUT /api/v1/admin/org/sandbox-concurrency` (value clamped 1..100), writing
      an `org.sandbox_concurrency_updated` audit event on success
      (`backend/src/modulo/api/routes/admin.py`)
- [x] The frontend surfaces the whole lifecycle on the Runners page (FAR-591 D5):
      profiles tab (`/admin/runners/profiles`) with list + search + per-card
      "Test connection" (SSE) and new/edit form (`/admin/runners/profiles/new`,
      `/admin/runners/profiles/:id/edit`), plus the concurrency tab
      (`/admin/runners/concurrency`); the legacy `/environment-profiles*` and
      `/admin/environments` deep links redirect to the profiles tab — testids
      enumerated in the product map

## Known Gaps

- Provider catalogue is fixed at `local_docker` + `e2b`; hub registration is
  env-driven and no plugin surface exists for third-party runtime providers.
- The sandbox test endpoint is contract-level (echo/exec only); it does not run the
  actual agent graph inside the workspace before release.

## QA History
- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/runners/concurrency`, `/admin/runners/profiles`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **improve-architecture (product-map walk)** — finished the
  Runners-page element-inventory walk by registering the TAB-surface testids
  that lived in the route-per-tab leaf components rather than the layout: the
  profiles tab's tier badge, template detail box, drift banner and apply control
  (`envprofile-list-tier-badge`, `runner-profile-detail`, `runner-profile-drift`,
  `runners-profiles-apply` in `RunnersProfilesTab.vue`) and the concurrency
  tab's effective-cap + preflight panels (`runner-concurrency-effective`,
  `runner-concurrency-preflight` in `RunnersConcurrencyTab.vue`), plus the
  persistent runner status strip (`runner-status-strip`,
  `runner-status-strip-state`, `runner-status-strip-machines`,
  `RunnerStatusStrip.vue`) that renders above both tabs. All are now registered
  on `/admin/runners/profiles` / `/admin/runners/concurrency`, and the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`)
  now maps those two routes to the layout + tab + status-strip owning views, so
  a newly shipped Runners-page testid can no longer drift invisible to Remy's
  docs indexer / `/api/v1/manifest`.
- 2026-09-11: **improve-architecture (product-map walk)** — closed the
  remaining element-inventory drift on the Runners page: `runner-status-error`
  (the `AdminRunnersView.vue` reload-error surface) is now registered on
  `/admin/runners/concurrency` as well as `/admin/runners/profiles`, and the
  tier badge of the profile form (`envprofile-form-tier-badge`,
  `EnvironmentProfileForm.vue`) is now registered on `/admin/runners/profiles/new`
  and `/admin/runners/profiles/:id/edit`. Both whole-page views were added to the
  reverse testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`)
  so a newly shipped Runners-page testid can no longer drift invisible to Remy's
  docs indexer / `/api/v1/manifest`.
- 2026-09-10: **improve-architecture (product-map walk)** — registered the
  `runner-status-error` testid of the `AdminRunnersView.vue` layout on
  `/admin/runners/profiles` and added that whole-page view to the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`),
  so the Runners page's reload-error surface can no longer ship invisible to
  Remy's docs indexer / `/api/v1/manifest`.
- 2026-09-10: **improve-architecture (product-map walk)** — reconciled this entry
  and the graph-root registry index with the FAR-591 D5 Runners page: the
  canonical routes are `/admin/runners/profiles{,/new,/:id/edit}` and
  `/admin/runners/concurrency`, and the old `/environment-profiles*` /
  `/admin/environments` URLs are redirects. No shipped behaviour changed.
- 2026-09-02: **FAR-551** — collapsed the duplicate `/admin/environments` UI +
  `environments.py` router (`/api/v1/environments`) into `/environment-profiles`;
  ported the `POST /{id}/test` connectivity check onto the survivor with a dedicated
  `environment_profile.test` permission; added the API-layer `require_feature`
  gate the new router was missing; `/admin/environments` now redirects.
- 2026-09-01: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-environments`, which previously had no
  `docs/product-map/` entry. Behaviours verified against
  `api/routes/environment_profiles.py`, `api/routes/environments.py`,
  `api/routes/admin.py`, `core/runtime_provider`, `core/graph_validator` and the
  env unit/integration/BDD suites. Status: covered.
