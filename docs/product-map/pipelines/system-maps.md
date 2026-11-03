---
id: feat-system-maps
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/system_maps.py
  - backend/src/modulo/api/schemas/system_maps.py
  - backend/src/modulo/api/dependencies.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
  - backend/src/modulo/api/team_scope.py
  - backend/src/modulo/core/system_maps_discovery.py
  - backend/src/modulo/core/system_map_persistence.py
  - backend/src/modulo/core/system_map_validation.py
  - backend/src/modulo/core/feature_flags.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/db/models/system_map.py
  - backend/src/modulo/db/repositories/system_map.py
  - backend/src/modulo/db/migrations/versions/0341_system_maps.py
  - backend/src/modulo/db/migrations/versions/0342_system_maps_integrity.py
  - backend/src/modulo/db/migrations/versions/0343_system_maps_revision_fk.py
  - backend/src/modulo/db/migrations/versions/0344_system_maps_tenant_integrity.py
  - backend/src/modulo/db/migrations/versions/0345_system_maps_name_integrity.py
  - backend/src/modulo/db/migrations/versions/0347_system_maps_version_aware.py
  - backend/src/modulo/cli/apply/system_map_apply.py
  - backend/src/modulo/cli/apply/plan.py
  - backend/src/modulo/cli/apply/models.py
  - frontend/src/views/SystemMapsView.vue
  - frontend/src/views/SystemMapDetailView.vue
  - frontend/src/views/DashboardView.vue
  - frontend/src/components/system-maps/SystemMapGraphEditor.vue
  - frontend/src/components/system-maps/SystemMapDiscoveryPanel.vue
  - frontend/src/components/system-maps/SystemMapDraftReview.vue
  - frontend/src/components/system-maps/SystemMapHeaderBar.vue
  - frontend/src/components/system-maps/SystemMapRevisionSidebar.vue
  - frontend/src/components/system-maps/discoveryMerge.ts
  - frontend/src/components/dashboard/DashboardSystemOverview.vue
  - frontend/src/stores/systemMaps.ts
  - frontend/src/manifest.yaml
  - frontend/src/lib/api/schema.ts
unit-tests:
  - backend/tests/unit/api/test_system_maps_routes.py
  - backend/tests/unit/api/test_system_maps_discovery_routes.py
  - backend/tests/unit/core/test_system_maps_discovery.py
  - backend/tests/unit/core/test_system_map_persistence.py
  - backend/tests/unit/core/test_system_map_validation.py
  - backend/tests/unit/db/test_system_map_repository.py
  - backend/tests/unit/db/test_system_map_models.py
  - backend/tests/unit/db/test_migration_0347_system_maps_version_aware.py
  - backend/tests/unit/cli/apply/test_system_map_apply.py
  - backend/tests/integration/test_system_maps_discovery.py
  - backend/tests/integration/test_system_maps_version_aware.py
  - backend/tests/integration/test_system_maps_overview_metrics.py
  - backend/tests/integration/test_system_maps_publish_audit.py
  - backend/tests/integration/test_migration_0347_system_maps_version_aware.py
  - backend/tests/integration/db/test_system_map_migrations.py
  - frontend/src/__tests__/systemMapsStore.spec.ts
  - frontend/src/__tests__/systemMapsStoreCrud.spec.ts
  - frontend/src/__tests__/systemMapsAuthoring.spec.ts
  - frontend/src/__tests__/SystemMapsView.spec.ts
  - frontend/src/__tests__/SystemMapDetailView.spec.ts
  - frontend/src/__tests__/SystemMapGraphEditor.spec.ts
  - frontend/src/__tests__/SystemMapComponents.coverage.spec.ts
  - frontend/src/__tests__/SystemMapDraftReview.spec.ts
  - frontend/src/__tests__/DashboardSystemOverview.spec.ts
  - frontend/src/__tests__/DashboardView.spec.ts
bdd: []
status: partial
---

# System Maps (Beta, internal dogfood)

An internal, default-off preview that inspects an organisation's existing
inventory (pipelines, triggers, connector instances and pipeline folders),
materialises a deterministic, unpublished draft graph from it, and lets an
operator author, revise and publish that graph as a delivery System Map. It has
its own routed surface (`/system-maps` and `/system-maps/:id`, both
`private_preview`) and its own schema, registered in the manifest `features:`
registry as `feat-system-maps`.

Every route sits behind the internal-only `system_maps` gate: developer mode,
the dedicated `system_maps_access` grant (operator floor) and the
`system_maps` feature flag, all resolved server-side on each request. The
surface is deliberately invisible to ordinary customers and never publishes
automatically.

The map is stored in dedicated System Maps tables (migration
`0341_system_maps.py`: `system_maps`, `system_map_revisions`,
`system_map_nodes`, `system_map_relationships`, `system_map_loop_configs`,
`system_map_attribution_bindings`, `system_map_loop_admission_attempts` and
`system_map_loop_reservations`), not in `lifecycle_maps`. Migrations 0342 to
0345 close graph, tenant and live-name integrity gaps, and
`0347_system_maps_version_aware.py` adds the `effective_from` /
`effective_to` windows that make a revision readable at a point in time.

## Behaviours

- [x] The `system_maps` flag is a community-tier `FeatureFlag` registered in
      `INTERNAL_FLAGS` and `DEFAULT_OFF_FLAGS`, so it is off by default and
      filtered from customer-facing plan/catalogue responses
      (`effective_feature_flags`); it is visible in the admin feature-flag list
      only to a caller passing the internal System Maps gate, and the list's
      cache key is scoped by that visibility class so an internal response can
      never be served to an ordinary caller or vice versa
      (`core/feature_flags.py`, `api/routes/admin_feature_flags.py`)
- [x] Every System Maps request is gated three times and fails closed: the
      router-level `_require_internal_system_maps_access` (developer mode plus
      the dedicated `system_maps_access` grant through `require_in_dev_operator`),
      the `require_feature("system_maps")` entitlement gate (disabled -> 402),
      and the handler's `require_permission(...)` role floor; a caller without
      the grant, without dev mode, or below the operator floor is refused before
      any inventory read (`api/routes/system_maps.py`,
      `api/dependencies.py`; `test_system_maps_routes.py`)
- [x] The permission vocabulary is registered and enforced on every shipped
      route: `system_maps_access` is operator; `system_map.list`,
      `system_map.revision.get`, `system_map.revision.diff` and
      `system_map.overview` are viewer; map create / update / delete, revision
      create / publish / archive / delete, restore-as-new and attribution
      binding create / delete are operator. The `system_map.node.create`,
      `system_map.relationship.create` and `system_map.loop_config.create`
      keys are registered but have no route: nodes, relationships and loop
      configs are authored inside the revision payload
      (`auth/permissions.py`, `api/routes/system_maps.py`)
- [x] Every handler is organisation-scoped: it binds `set_rls_org` and
      `set_rls_user_context` inside the same `session.begin()` block as its
      reads and writes, and every repository call carries an explicit
      `organisation_id` (`api/routes/system_maps.py`,
      `db/repositories/system_map.py`; `test_system_maps_routes.py`)
- [x] Map visibility is team-aware: `system_maps` carries `visibility`
      (`org` / `team`) and `owner_team_id`, a team-visible map must name an
      owner team, create is validated by `validate_owner_team_for_create`,
      update by `validate_team_transition_for_update`, and the repository
      resolves the team scope (`api/team_scope.py`; `test_system_map_models.py`)
- [x] One published revision per map is a database invariant: a partial unique
      index on `(system_map_id)` where `status = 'published'`, a unique
      `(system_map_id, revision_number)` pair, composite tenant foreign keys,
      and a partial unique live-name index on `(organisation_id, name)` where
      `deleted_at IS NULL` (`db/models/system_map.py`;
      `0341_system_maps.py`, `0343_system_maps_revision_fk.py`,
      `0345_system_maps_name_integrity.py`;
      `test_system_map_migrations.py`)
- [x] `POST /api/v1/system-maps/discover` inspects only the caller's
      organisation: live pipelines (`deleted_at IS NULL`,
      `archived_at IS NULL`), live triggers, live connector instances and
      pipeline folders, then returns 201 with a discovery report (sources
      inspected, node/edge counts, inferred nodes and edges, skipped sources,
      warnings, draft map/revision ids, revision number, `published=false`)
      (`api/routes/system_maps.py`, `api/schemas/system_maps.py`;
      `test_system_maps_discovery_routes.py`)
- [x] Discovery is deterministic and explainable: sources are sorted by
      `(kind, identifier, name)`; nodes are built only for the supported kinds
      (pipeline, trigger, connector, folder); edges are emitted only for
      explicit relationships (trigger targets a pipeline, pipeline belongs to a
      folder); every edge carries `config_json.suggested = true` plus a reason;
      duplicate edges are de-duplicated in a stable sort order
      (`core/system_maps_discovery.py`; `test_system_maps_discovery.py`)
- [x] Unsupported or unresolvable sources are reported, never guessed: an
      unknown source kind, a trigger with no pipeline binding, a trigger whose
      pipeline is not in the inventory, a pipeline whose folder is not in the
      inventory, and an improvement-loop/tagged pipeline with no supported
      non-self relationship each appear in `skipped` with a reason, and a
      non-empty `skipped` list raises a warning (an empty inventory raises its
      own warning instead) (`core/system_maps_discovery.py`;
      `test_system_maps_discovery.py`)
- [x] The draft's lifecycle-map-shaped `stages` / `edges` compatibility
      projection is honest about provenance: a discovery pipeline node maps to
      `modulo`, a trigger node to `manual` (it carries no Modulo pipeline of its
      own), and connector/folder nodes to `placeholder` (external systems,
      never presented as Modulo) (`api/routes/system_maps.py::_stage_type`;
      `test_system_maps_routes.py`)
- [x] Discovery persists a draft in the System Maps tables, not `lifecycle_maps`:
      it creates the map (or refuses a name already used by a live map with
      409, with no silent reuse and no automatic suffix) and one draft revision;
      the revision id is a uuid5 over the map id and the discovery fingerprint,
      so the revision identity is stable for a given map and inventory
      (`core/system_map_persistence.py`;
      `test_system_map_persistence.py`, `test_system_maps_discovery.py`)
- [x] A draft revision is authored from a bounded payload: `POST
      /system-maps/{map_id}/revisions` accepts nodes, relationships and loop
      configs, validates every pipeline / connector / eval / owner-team /
      action-pipeline binding (invalid -> 422), assigns the next revision number
      server-side, and records a `system_map.revision.created` audit event
      (`api/routes/system_maps.py`, `db/repositories/system_map.py`;
      `test_system_maps_routes.py`)
- [x] Publishing validates before it publishes: relationship endpoints resolve
      against live nodes, `flow` edges are acyclic, every `improvement_loop`
      node has a loop config carrying all six required parameters, and every
      binding resolves in the organisation and team scope; an invalid revision
      returns the wire-contract `PublishResponse` carrying `validation_errors`
      while the audit dependency records `outcome="error"`; a valid publish sets
      `published_revision_id`, closes the previous revision's effective window
      and stamps `effective_from` (`db/repositories/system_map.py`,
      `api/routes/system_maps.py`; `test_system_maps_publish_audit.py`)
- [x] Archiving closes the revision's effective window and clears
      `published_revision_id` when the archived revision was the published one;
      a revision cannot be deleted while published (409, archive first), and a
      draft or archived revision is soft-deleted with an audit event
      (`db/repositories/system_map.py`, `api/routes/system_maps.py`)
- [x] Revision diff and restore ship: `POST .../revisions/{rev_id}/diff` reports
      node / relationship / loop-config additions, removals and modifications
      against another revision or the current draft, and
      `POST .../revisions/{rev_id}/restore-as-new` creates a new draft revision
      from a published or archived one and is audited; diff is a read
      (`api/routes/system_maps.py`, `api/schemas/system_maps.py`;
      `test_system_maps_version_aware.py`)
- [x] Version-aware reads let runs and metrics be interpreted against the map
      revision active at a point in time:
      - `GET .../revisions/by-time?at=<ISO8601>` returns the revision whose
        window covers the instant, including a superseded (archived) revision
        after a re-publish, where `effective_from <= at < effective_to` (or
        `effective_to IS NULL` for the current revision)
      - `GET .../revisions/current` returns the currently active (published)
        revision as a semantic alias
      - `GET .../revisions/history` lists all revisions with their effective
        time ranges, marking the current revision with `is_current=true`
      - `GET .../overview` accepts an optional `revision_id` or `at` timestamp
        (mutually exclusive; both -> 400) to show metrics for a specific
        historical revision's window
      (`api/routes/system_maps.py`, `db/repositories/system_map.py`,
      `0347_system_maps_version_aware.py`, `test_system_maps_version_aware.py`,
      `test_migration_0347_system_maps_version_aware.py`)
- [x] `GET /system-maps/{map_id}/overview` is a read model over the trailing
      seven UTC days: per-node health (`healthy` / `degraded` / `unhealthy` /
      `unknown`, where `unhealthy` is a failure rate of 50% or more, any
      non-zero rate below that is `degraded`, and no countable run is
      `unknown`, never `healthy`), throughput, queue depth and telemetry
      coverage, loop-config metrics, and detected bottlenecks; it dispatches
      nothing
      (`api/routes/system_maps.py`, `db/repositories/system_map.py`;
      `test_system_maps_overview_metrics.py`)
- [x] Attribution bindings attach live triggers and loop action pipelines to
      nodes in the published revision (409 while the map has no published
      revision): a `trigger` binding requires a
      `pipeline` node whose `pipeline_id` matches, an `action_pipeline` binding
      requires an `improvement_loop` node whose loop config names the same
      action pipeline, the pipeline (and optional trigger) must exist in the
      organisation, and delete is a fail-closed audited soft delete
      (`api/routes/system_maps.py`, `db/repositories/system_map.py`;
      `test_system_maps_routes.py`)
- [x] `modulo apply` treats a System Map as a name-keyed declarative entity
      (`KIND_SYSTEM_MAP`): it plans and applies `system_maps` entries, creates
      the map plus its declared draft revision on create, blocks an ambiguous
      name, validates graph references at load time, and keeps revision content
      create-only with publish as an explicit API action
      (`cli/apply/system_map_apply.py`, `cli/apply/plan.py`,
      `cli/apply/models.py`, `core/system_map_validation.py`;
      `test_system_map_apply.py`)
- [x] The routed UI is wired to the shipped backend: `/system-maps` lists maps
      with create, open, discover (per-row action), delete and pagination, and
      `/system-maps/:id` provides the bounded graph editor, save-draft, publish,
      archive, discovery review and a revision sidebar, all read-only on a
      published or archived revision (`frontend/src/views/SystemMapsView.vue`,
      `frontend/src/views/SystemMapDetailView.vue`,
      `frontend/src/components/system-maps/SystemMapHeaderBar.vue`;
      `SystemMapsView.spec.ts`, `SystemMapDetailView.spec.ts`)
- [x] "Discover My System" is a real operation in the routed UI: the store
      POSTs `/api/v1/system-maps/discover`, the detail view opens
      `SystemMapDiscoveryPanel`, and the panel renders sources inspected,
      inferred nodes and edges, skipped sources and warnings with per-item
      accept/reject decisions that are merged into the draft before save
      (`frontend/src/stores/systemMaps.ts`,
      `frontend/src/components/system-maps/SystemMapDiscoveryPanel.vue`,
      `frontend/src/components/system-maps/discoveryMerge.ts`;
      `systemMapsAuthoring.spec.ts`)
- [x] The graph editor is bounded to the backend vocabularies
      (`pipeline` / `agent` / `connector` / `improvement_loop` / `data_store` /
      `external_system`; `flow` / `data` / `trigger` / `loop_back`), mirrored
      from the API CHECK constraints with drag and keyboard repositioning
      (`frontend/src/stores/systemMaps.ts`,
      `frontend/src/components/system-maps/SystemMapGraphEditor.vue`;
      `SystemMapGraphEditor.spec.ts`)
- [x] The dashboard card renders the real overview aggregate for the default
      (or first) published map, with loading, empty, error and retry states,
      and only after the internal access probe settles with the flag enabled
      (`frontend/src/components/dashboard/DashboardSystemOverview.vue`,
      `frontend/src/views/DashboardView.vue`;
      `DashboardSystemOverview.spec.ts`, `systemMapsStore.spec.ts`)
- [x] The store's access probe fails closed and rejects stale principals: it
      revalidates on every access-token change, de-duplicates concurrent
      probes, drops responses from a previous generation or token, requires
      operator/system-admin, dev mode, `system_maps_access` and the
      `system.config.manage` probe permission, and bounds the flag and overview
      requests (15s) (`frontend/src/stores/systemMaps.ts`;
      `systemMapsStore.spec.ts`)
- [x] MCP callers only see the `system_maps` flag when they hold the same
      internal System Maps grant (developer mode, operator role floor, grant
      check through `grants_permit`); ordinary callers never see the internal
      flag in flag listings (`api/mcp_server.py::_mcp_flag_visible`;
      `test_system_maps_routes.py` flag-visibility cases)
- [ ] No MCP System Maps tool: the MCP layer only hides/shows the flag. There is
      no MCP equivalent of discovery, authoring, publishing or the overview read
      (`api/mcp_server.py`)
- [ ] The routed UI does not use the version-aware revision reads: the revision
      sidebar lists revisions loaded in the current session and switches with
      `GET /revisions/{rev_id}`; nothing calls `revisions/current`,
      `revisions/by-time` or `revisions/history`, and diff and restore-as-new
      have no UI affordance (`frontend/src/stores/systemMaps.ts`,
      `frontend/src/components/system-maps/SystemMapRevisionSidebar.vue`)
- [ ] Discovery cannot re-scan into an existing map: there is no re-scan
      endpoint and each successful discovery creates a map (a repeat with a
      colliding name is 409), so the added / removed / ambiguous
      re-scan reconciliation is not available
      (`core/system_map_persistence.py`, `api/routes/system_maps.py`)
- [ ] Loop execution is not wired: loop configs and the admission-attempt /
      reservation tables ship, but nothing dispatches a loop. There is no
      admission check, budget enforcement, cooldown scheduling or concurrency
      guard at runtime, and the admission / reservation repository methods have
      no production caller (`db/repositories/system_map.py`;
      `0341_system_maps.py`)
- [ ] The dashboard card's "Discover My System" button is still disabled and the
      card falls back to static copy when no published map exists; discovery is
      available from the routed list and detail pages instead
      (`frontend/src/components/dashboard/DashboardSystemOverview.vue`,
      `frontend/src/components/system-maps/SystemMapDraftReview.vue`)
- [ ] No BDD coverage: discovery, persistence, the revision lifecycle, the
      version-aware reads, apply and the frontend are covered by unit tests and
      Postgres integration tests, but there is no Gherkin feature file for the
      System Maps surface (`bdd:` is empty)

## Known Gaps

- **No MCP System Maps surface.** The MCP layer only hides or shows the
  `system_maps` flag; an agent cannot discover, author, publish or read a map
  through MCP.
- **Version-aware reads are backend-only.** `revisions/current`,
  `revisions/by-time`, `revisions/history`, `revisions/{rev_id}/diff` and
  `restore-as-new` all ship and are tested, but the routed UI still lists only
  the revisions the session has loaded and offers no diff or restore affordance.
- **Discovery is create-only.** Each discovery persists a new map plus a draft
  revision; a repeat with a colliding name is a 409. There is no re-scan into an
  existing draft, so the "added / removed / ambiguous" reconciliation described
  for a later milestone does not exist yet.
- **No loop dispatch runtime.** Loop configs are authorable, overview loop
  metrics are read, and the admission-attempt / reservation tables and
  repository methods exist, but no runtime consumes them: budgets, cooldowns and
  concurrency are not enforced (deferred to Beta 3).
- **The dashboard card is a metrics readout, not an authoring entry point.** Its
  discover button stays disabled and its no-overview fallback is static copy;
  the working discovery and authoring surfaces are the two routed pages.
- **No BDD coverage.** There is no Gherkin feature file for the surface, so
  end-to-end behaviour is pinned by unit and Postgres integration tests only.

## QA History

- 2026-10-31: **Improve Architecture product-map walk** - closed the feature-graph
  gap left by FAR-2244 (backend discovery) and FAR-2245 (dashboard draft-review
  card): the shipped System Maps preview had no behaviour-tracker entry in either
  product-map layer. Added this entry (code, unit/integration test and frontend
  spec citations, the internal-flag visibility rules, the triple access gate, the
  deterministic discovery contract, the collision-safe lifecycle-map persistence
  adapter, the audit event, the store's fail-closed revalidation, and the MCP
  flag-visibility gate) and linked it from the graph root. Recorded the real
  remaining gap honestly: the UI never calls the shipped discovery endpoint, so
  the entry stays `partial` with no BDD file. `_ORPHANED_BDD_FEATURES` stays
  empty.
- 2026-11-03: **Refresh against shipped reality (FAR-2288 follow-up)** -
  re-verified every claim in this entry against `origin/main` and corrected the
  ones FAR-2236 and its follow-ups had made false. Corrected: (1) "drafts
  persist as `lifecycle_maps` rows through a compatibility adapter" - they now
  persist in the dedicated System Maps tables via
  `core/system_map_persistence.py`, whose module contract explicitly does not
  use the legacy `LifecycleMap` tables; (2) "no dedicated System Maps schema
  until FAR-2236 lands" - migrations `0341` to `0345` and `0347` create the
  eight dedicated tables, the tenant/name/revision integrity constraints and the
  effective-time windows; (3) "the UI never calls the discovery endpoint and the
  review panel is static copy" - `systemMaps.discover()` POSTs
  `/api/v1/system-maps/discover` and `SystemMapDiscoveryPanel` is a real
  accept/reject merge panel; only the dashboard card's own button and its
  no-overview fallback remain static; (4) "no route of its own, only a dashboard
  card" - the manifest registers `/system-maps` and `/system-maps/:id` against
  `feat-system-maps` with real list, detail, graph-editor, revision-sidebar and
  discovery components; (5) "no publish flow" and the lifecycle-map write
  invariants (FOR UPDATE lock, pipeline uniqueness, normaliser bounds, rebuilt
  stage projection) - replaced by the System Maps repository and
  `validate_publish` / `validate_bindings`; (6) "an existing discovery-owned map
  with the same name is reused" - any live name collision is now a 409. Added
  the shipped-but-undocumented surface: the revision lifecycle (create,
  publish, archive, delete, diff, restore-as-new), the version-aware reads and
  overview revision selection, attribution bindings, team visibility, and
  `modulo apply` support. Replaced the stale code and test citations with the
  paths that actually exist. Status stays `partial`, with the remaining gaps
  re-verified rather than copied: MCP has no System Maps tool, the version-aware
  history UI is unwired, discovery is create-only, loop dispatch has no runtime,
  the dashboard discover button is disabled, and there is no BDD file.
