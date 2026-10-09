---
id: feat-environments
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/environment_profiles.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/migrations/versions/0289_pipelines_environment_profile.py
  - backend/src/modulo/core/runtime_provider
  - backend/src/modulo/core/team_visibility.py
  - backend/src/modulo/core/bundled_runner/runner_dispatch.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/cli/apply/models.py
  - backend/src/modulo/cli/apply/plan.py
  - backend/src/modulo/cli/apply/pipeline_apply.py
  - backend/src/modulo/api/routes/admin.py
  - frontend/src/views/runners
  - frontend/src/views/environment-profiles
  - frontend/src/views/PipelineEditorView.vue
  - frontend/src/stores/environmentProfiles.ts
unit-tests:
  - backend/tests/integration/crud/test_environment_profiles.py
  - backend/tests/unit/api/test_environment_profiles_routes.py
  - backend/tests/unit/api/test_pipeline_environment_profile_binding.py
  - backend/tests/unit/api/test_snapshot_environment_profile_coercion.py
  - backend/tests/unit/api/test_mcp_update_pipeline.py
  - backend/tests/unit/cli/test_apply_environment_profile.py
  - backend/tests/unit/core/bundled_runner/test_dispatch_route.py
  - backend/tests/integration/test_dispatch_team_scope_backstop.py
  - backend/tests/integration/test_environment_profile_scope_rls_guard.py
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
      declares a `provider_hint` – see the hub-resolution scenarios in
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
      `/admin/environments` deep links redirect to the profiles tab – testids
      enumerated in the product map
- [x] The environment-profile create/edit form offers the `kubernetes` provider
      as a first-class option (FAR-1559): the provider select renders the three
      manually-creatable provider types — `local_docker`, `e2b` and `kubernetes`
      (labelled "External Runner (Kubernetes)") — while `local` and
      `runner_docker` stay system-seeded and are deliberately NOT offered for
      manual creation; the form drives the single backend `provider_type`
      vocabulary (`PROVIDER_TYPES` = `{local_docker, e2b, local, runner_docker,
      kubernetes}`, FAR-595) with the provider-is-required validation and tier
      hint preserved (`frontend/src/views/environment-profiles/
      EnvironmentProfileForm.vue`, `frontend/src/__tests__/environment-profiles/
      EnvironmentProfileForm.spec.ts`, `backend/src/modulo/core/runtime_provider/
      k8s.py`)
- [x] The pipeline to environment-profile binding is settable per pipeline
      (FAR-1558): `PATCH /api/v1/pipelines/{pipeline_id}` accepts
      `environment_profile_id` (omit to leave unchanged, explicit `null` clears
      and restores the default route), validated in the route against the
      EFFECTIVE post-update owner team — the profile must exist in the caller's
      organisation and be either org-visible or a team profile owned by that
      team; a foreign or cross-team id is 422 (never 404, which would confirm it
      exists) and a team/visibility change re-validates a STORED binding so a
      move can never strand an ineligible profile. The binding is frozen onto
      every snapshot created afterwards
      (`db/crud/pipeline_snapshot.create_snapshot_from_live_graph`), which is the
      value dispatch reads; `NULL` keeps the historical default behaviour. The
      team rule is enforced at ALL THREE writers of the invariant (bind time, a
      pipeline scope change, and a profile scope change — the profile side
      refuses a `PUT` that would strand an existing binding with 422
      `environment_profile_binding_team_mismatch` BEFORE any write), through ONE
      shared predicate + shared wire-code constants in `core/team_visibility.py`
      so the three cannot drift; the profile-side bound-pipelines scan runs
      team-blind (`db.crud.team_scope.team_blind_org_scope`, the FAR-1515
      CRITICAL 1 mechanism) because a non-admin operator cannot see another
      team's team-private pipeline in their own RLS context. The editor binds or
      clears the profile from a labelled Environment profile select that renders
      only while the `environment_profiles` plan feature is on, labels each
      option with its provider tier, sends nothing for an untouched select, and
      reverts on a refused/failed write with an alert region
      (`backend/src/modulo/api/routes/pipelines.py`,
      `backend/src/modulo/api/routes/environment_profiles.py`,
      `backend/src/modulo/core/team_visibility.py`,
      `backend/src/modulo/db/migrations/versions/0289_pipelines_environment_profile.py`,
      `frontend/src/views/PipelineEditorView.vue`;
      `unit-tests: test_pipeline_environment_profile_binding.py,
      test_environment_profiles_routes.py,
      test_environment_profile_scope_rls_guard.py`)
- [x] The FAR-1558 team rule has a dispatch-time backstop as its last line of
      defence (FAR-1598): when the snapshot-bound environment profile is loaded
      for dispatch, `runner_dispatch` re-validates it with the SAME shared
      predicate (`core.team_visibility.environment_profile_team_mismatch`)
      against the pipeline's EFFECTIVE owner team, read team-blind through
      `set_rls_execution_context` + `include_soft_deleted` so a team-private
      pipeline or profile is never hidden from the internal execution context - a
      team-private profile not owned by that team raises the typed
      `SandboxDispatchUnboundError` carrying the named
      `environment_profile_team_mismatch` code BEFORE any provider or hub is
      selected, so a binding forged or drifted outside the three REST writers (a
      direct DB write, a race) can never silently fall back to another provider
      or the default route; org-visible profiles and NULL bindings resolve
      exactly as before, and an unresolvable owner team is fail-closed (treated
      as "no owner team", so a team-private profile mismatches every pipeline).
      Locked by `backend/tests/integration/test_dispatch_team_scope_backstop.py`,
      which exercises the PRODUCTION consumption chain (executor ctx seeding ->
      node_runner `_resolve_sandbox_dispatch_route_for_run` -> shared predicate)
      under real Postgres RLS: it asserts the premise first (an org-only session
      cannot see the team-private profile), then the typed refusal with no
      provider selected, then the same-team mirror resolving the e2b route - the
      test fails if the dispatch enforcement is removed
      (`backend/src/modulo/core/bundled_runner/runner_dispatch.py`,
      `backend/src/modulo/core/pipeline_engine/node_runner.py`;
      `unit-tests: test_dispatch_team_scope_backstop.py,
      test_dispatch_route.py`)
- [x] The binding is exposed on the read and non-REST surfaces (FAR-1599):
      `SnapshotResponse` carries `environment_profile_id` (additive and nullable
      - legacy and partial stand-in snapshots serialise as null) so a snapshot's
      frozen binding is readable through the API; the MCP `update_pipeline` tool
      accepts the field so MCP / org-API-key callers can set it; and
      `cli/apply` takes an `environment_profile_id` key on pipelines, managed
      ONLY when declared (an omitted key is neither hashed nor written so a
      UI/API-set binding survives untouched, a declared id is hashed in
      UUID-string id-space so `--diff` reports drift, and an explicit null clears
      back to the default route), with a created pipeline's binding riding the
      follow-up PATCH because `PipelineCreate` cannot carry the field
      (`backend/src/modulo/api/mcp_server.py`,
      `backend/src/modulo/cli/apply/{models,plan,pipeline_apply}.py`;
      `unit-tests: test_apply_environment_profile.py,
      test_mcp_update_pipeline.py,
      test_snapshot_environment_profile_coercion.py`)

## Known Gaps

- Provider catalogue is fixed at `local_docker` + `e2b` + `kubernetes` (the
  `kubernetes` provider is env-gated on the `kubernetes-asyncio` SDK, FAR-1051);
  hub registration is env-driven and no plugin surface exists for third-party
  runtime providers.
- The sandbox test endpoint is contract-level (echo/exec only); it does not run the
  actual agent graph inside the workspace before release.

## QA History
- 2026-10-09: **Improve Architecture product-map walk** – reconciled the two
  product-map layers for the environment-profile binding follow-ups. The
  FAR-1614 post-merge polish sweep (PR #1455) added the FAR-1598 (dispatch-time
  team-scope backstop) and FAR-1599 (read / non-REST surface exposure:
  `SnapshotResponse.environment_profile_id`, the MCP `update_pipeline` tool, and
  the `modulo apply` `environment_profile_id` key) behaviours to the manifest
  `feat-environments` registry, but this human-readable tracker still stopped at
  FAR-1558, so a reader of the feature graph got the opposite coverage answer
  from the machine layer Assistant indexes. Added the two checked behaviour
  lines mirroring the manifest, plus the `runner_dispatch.py` / `node_runner.py`
  / `mcp_server.py` / `cli/apply` code citations and the
  `test_dispatch_team_scope_backstop.py` / `test_dispatch_route.py` /
  `test_apply_environment_profile.py` / `test_mcp_update_pipeline.py` /
  `test_snapshot_environment_profile_coercion.py` test citations.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1558 sub-surface (per-pipeline environment-profile binding, slices 1 and 2
  merged in PRs #1396 and #1406): the binding shipped in the manifest
  `feat-environments` registry but this human-readable tracker never mirrored
  it, so a reader of the feature graph could not see the PATCH
  `environment_profile_id` contract, the three-writer team-scope invariant, the
  snapshot freeze, or the editor select. Added the checked behaviour line plus
  the `pipelines.py` / `environment_profiles.py` / `core/team_visibility.py` /
  migration `0289` / `PipelineEditorView.vue` and unit- and integration-test
  citations.
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1559 sub-surface (kubernetes provider type in the environment-profile
  form, merged in PR #1404): the form shipped the `kubernetes` provider while
  the manifest `feat-environments` registry and this tracker still listed the
  provider catalogue as "local_docker + e2b" only. Added the checked behaviour
  line (the three manually-creatable provider types, `local`/`runner_docker`
  excluded) and corrected the stale Known Gap to name the env-gated `kubernetes`
  provider instead of asserting a closed two-provider catalogue.
- 2026-09-12: **product-map review pass**: registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/runners/concurrency`, `/admin/runners/profiles`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass**: finished the
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
  a newly shipped Runners-page testid can no longer drift invisible to Assistant's
  docs indexer / `/api/v1/manifest`.
- 2026-09-11: **product-map review pass**: closed the
  remaining element-inventory drift on the Runners page: `runner-status-error`
  (the `AdminRunnersView.vue` reload-error surface) is now registered on
  `/admin/runners/concurrency` as well as `/admin/runners/profiles`, and the
  tier badge of the profile form (`envprofile-form-tier-badge`,
  `EnvironmentProfileForm.vue`) is now registered on `/admin/runners/profiles/new`
  and `/admin/runners/profiles/:id/edit`. Both whole-page views were added to the
  reverse testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`)
  so a newly shipped Runners-page testid can no longer drift invisible to Assistant's
  docs indexer / `/api/v1/manifest`.
- 2026-09-10: **product-map review pass**: registered the
  `runner-status-error` testid of the `AdminRunnersView.vue` layout on
  `/admin/runners/profiles` and added that whole-page view to the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`),
  so the Runners page's reload-error surface can no longer ship invisible to
  Assistant's docs indexer / `/api/v1/manifest`.
- 2026-09-10: **product-map review pass**: reconciled this entry
  and the graph-root registry index with the FAR-591 D5 Runners page: the
  canonical routes are `/admin/runners/profiles{,/new,/:id/edit}` and
  `/admin/runners/concurrency`, and the old `/environment-profiles*` /
  `/admin/environments` URLs are redirects. No shipped behaviour changed.
- 2026-09-02: **FAR-551**: collapsed the duplicate `/admin/environments` UI +
  `environments.py` router (`/api/v1/environments`) into `/environment-profiles`;
  ported the `POST /{id}/test` connectivity check onto the survivor with a dedicated
  `environment_profile.test` permission; added the API-layer `require_feature`
  gate the new router was missing; `/admin/environments` now redirects.
- 2026-09-01: **product-map review pass**: added this behaviour-tracker
  for the registered manifest feature `feat-environments`, which previously had no
  `docs/product-map/` entry. Behaviours verified against
  `api/routes/environment_profiles.py`, `api/routes/admin.py`,
  `core/runtime_provider`, `core/graph_validator` and the
  env unit/integration/BDD suites. Status: covered.
