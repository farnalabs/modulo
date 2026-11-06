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
  - backend/tests/unit/mcp/test_mcp_system_map_discovery.py
  - backend/tests/unit/core/test_system_maps_discovery.py
  - backend/tests/unit/core/test_system_map_persistence.py
  - backend/tests/unit/core/test_system_map_validation.py
  - backend/tests/unit/db/test_system_map_repository.py
  - backend/tests/unit/db/test_system_map_models.py
  - backend/tests/unit/db/test_migration_0347_system_maps_version_aware.py
  - backend/tests/unit/cli/apply/test_system_map_apply.py
  - backend/tests/unit/mcp/test_mcp_system_map_discovery.py
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
bdd:
  - backend/tests/bdd/features/system_maps/access_and_discovery.feature
  - backend/tests/bdd/features/system_maps/revisions.feature
  - backend/tests/bdd/steps/test_system_maps.py
status: partial
---

# System Maps (Beta, published reads + internal authoring)

System Maps inspects an organization's existing inventory (pipelines, triggers,
connector instances and pipeline folders), materializes a deterministic,
unpublished draft graph from it, and lets an operator author, revise and
publish that graph as a delivery System Map. Since FAR-2363 the surface has two
access classes: every principal with the viewer-level `system_maps.read`
permission can read the **published** maps, revisions, overview, history,
by-time and diff views (routes `/system-maps` and `/system-maps/:id`, visible in
the manifest as `public` and gated on `required_permissions:
[system_maps.read]`), while authoring — discovery, create, update, delete,
publish, archive, restore-as-new, apply, loop dispatch and MCP discovery —
remains behind the internal-only `system_maps` gate. Both classes are registered
in the manifest `features:` registry as `feat-system-maps`.

The published-read routes sit on their own router
(`published_router` in `api/routes/system_maps.py`) with
`_require_published_system_maps_read` (viewer floor + API-key grant check) and
`require_feature("system_maps_read")`, and the repository is asked for
`published_only=True` maps/revisions; a draft or an unpublished map is a 404 to
a viewer. The authoring routes keep the internal gate: developer mode, the
operator role floor, the `system_maps_access` grant for grant-bearing API keys,
and the `system_maps` feature flag, all resolved server-side on each request.
Legacy JWTs and keys with no grant set keep the role-based behavior rather
than being refused for the missing grant. Nothing publishes automatically.

The map is stored in dedicated System Maps tables (migration
`0341_system_maps.py`: `system_maps`, `system_map_revisions`,
`system_map_nodes`, `system_map_relationships`, `system_map_loop_configs`,
`system_map_attribution_bindings`, `system_map_loop_admission_attempts` and
`system_map_loop_reservations`), not in `lifecycle_maps`. Migrations 0342 to
0345 close graph, tenant and live-name integrity gaps, and
`0347_system_maps_version_aware.py` adds the `effective_from` /
`effective_to` windows that make a revision readable at a point in time.

## Behaviors

- [x] The `system_maps` flag is a community-tier `FeatureFlag` registered in
      `INTERNAL_FLAGS` and `DEFAULT_OFF_FLAGS`, so it is off by default and
      filtered from customer-facing plan/catalog responses
      (`effective_feature_flags`); it is visible in the admin feature-flag list
      only to a caller passing the internal System Maps gate, and the list's
      cache key is scoped by that visibility class so an internal response can
      never be served to an ordinary caller or vice versa
      (`core/feature_flags.py`, `api/routes/admin_feature_flags.py`)
- [x] Every System Maps request passes three gates and fails closed: the
      router-level `_require_internal_system_maps_access`, which enforces
      developer mode and the operator role floor through
      `require_in_dev_operator`, the `require_feature("system_maps")`
      entitlement gate (disabled -> 402), and the handler's
      `require_permission(...)` role floor, all before any inventory read. The
      dedicated `system_maps_access` grant is an additional leg that binds only
      grant-bearing API keys: `_enforce_key_grant` treats `key_grants is None`
      as a no-op because `grants_permit(None, ...)` returns `True`, so a legacy
      JWT or an ungranted key is judged on developer mode and role alone, as
      the dependency's own docstring states (`api/routes/system_maps.py`,
      `api/dependencies.py`, `auth/permissions.py`). The unit cases pin a
      viewer denied, an operator denied with developer mode off, and an
      operator key denied without the grant and allowed with it; the
      grant-denial case presents a grant-bearing key, so the ungranted-JWT path
      is not covered there (`test_system_maps_routes.py`)
- [x] Published reads are a distinct, viewer-level surface (FAR-2363):
      `system_maps.read` is a viewer permission and `system_maps_read` is a
      community-tier entitlement flag that is NOT internal/default-OFF, so a
      viewer (and the read-only demo principal, whose demo org role is
      `viewer`) can list/get published maps, the current/published revision,
      by-time, history, overview and diff. Drafts and unpublished maps are
      filtered in the repository (`published_only=True`) and re-checked in the
      route (404), never merely hidden in the UI. The authoring gate is
      unchanged: `system_maps_access` (operator, developer mode,
      `system_maps` flag) still guards create/update/delete, revision
      create/publish/archive/delete, restore-as-new, attribution bindings,
      discovery, apply, loop dispatch and MCP discovery. The two route groups
      are registered as separate routers with the published router included
      first so a GET resolves to the viewer gate rather than the internal one
      (`auth/permissions.py`, `api/dependencies.py`, `api/routes/system_maps.py`,
      `core/feature_flags.py`, `api/main.py`;
      `test_system_maps_routes.py`, `test_permissions.py`,
      `test_feature_flag_registry.py`). The demo path needs no developer mode
      and no elevated role: the seeded demo viewer reaches the published read
      model through the same permission, while writes still 403
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
- [x] Every handler is organization-scoped: it binds `set_rls_org` and
      `set_rls_user_context` inside the same `session.begin()` block as its
      reads and writes, and every repository call carries an explicit
      `organisation_id` (`api/routes/system_maps.py`,
      `db/repositories/system_map.py`; `test_system_maps_routes.py`)
- [x] Map visibility is team-aware: `system_maps` carries `visibility`
      (`org` / `team`) and `owner_team_id`, and the model's
      `ck_system_maps_team_owner` CHECK constraint rejects a `team` visibility
      with no owner team. Create is validated by
      `validate_owner_team_for_create` and update by
      `validate_team_transition_for_update`: a newly named owner team must
      exist in the caller's organization and a non-admin caller must belong to
      it, a team-private map's current owner team gates the transition, and an
      update cannot set `team` visibility without an owner team (422)
      (`db/models/system_map.py`, `api/routes/system_maps.py`;
      `test_system_maps_routes.py`). Isolation itself is enforced in the
      database: `0341_system_maps.py` creates the `rls_team_isolation` policy
      on `system_maps` (the org check plus the visibility / owner-team
      membership matrix). `resolve_system_map_team_scope` is registered in
      `TEAM_SCOPED_RESOLVERS` for registry completeness but deliberately never
      attached to a route dependency - the same DB-RLS-only posture as the core
      tables (`api/team_scope.py`;
      `tests/architecture/test_team_scope_wiring.py`,
      `tests/integration/test_rls_isolation.py`) - and `list_maps_by_org`
      applies only the caller-supplied `visibility` / `owner_team_id` / `name`
      filters, resolving no caller team scope of its own
      (`db/repositories/system_map.py`)
- [x] One published revision per map is a database invariant: a partial unique
      index on `(system_map_id)` where `status = 'published'`, a unique
      `(system_map_id, revision_number)` pair, composite tenant foreign keys,
      and a partial unique live-name index on `(organisation_id, name)` where
      `deleted_at IS NULL` (`db/models/system_map.py`;
      `0341_system_maps.py`, `0343_system_maps_revision_fk.py`,
      `0345_system_maps_name_integrity.py`;
      `test_system_map_migrations.py`)
- [x] `POST /api/v1/system-maps/discover` inspects only the caller's
      organization: live pipelines (`deleted_at IS NULL`,
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
      binding resolves in the organization and team scope; an invalid revision
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
      organization, and delete is a fail-closed audited soft delete
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
- [x] MCP flag listings hide `system_maps` unless the caller passes the
      internal access predicate: `_mcp_internal_system_maps_access` requires
      developer mode, the operator role floor and `grants_permit` over the
      caller's grant set, and `_mcp_flag_visible` keys the `system_maps` flag
      on that result. Ordinary callers (no developer mode, or below operator)
      never see it. As on REST, the grant leg only narrows a grant-bearing
      credential: a JWT session sets the MCP grant context to `None`,
      `grants_permit` treats that as "do not narrow", so an ungranted JWT
      operator does see the flag (`api/mcp_server.py`;
      `tests/unit/mcp/test_mcp_config_tools.py` -
      `TestSystemMapsInternalFlagMCP` and
      `TestSystemMapsAccessUsesSharedGrantCheck::test_ungranted_jwt_caller_keeps_role_behaviour`)
- [x] Discovery is exposed over MCP: the `discover_system_map` tool drives the
      SAME `POST /api/v1/system-maps/discover` handler the REST surface uses, so
      the inventory read, deterministic inference and persistence stay one
      implementation. It runs the reused handler in a fresh session, re-applies
      the internal gate server-side (developer mode, operator/admin role floor,
      `system_maps_access` grant and the `system_maps` feature, each before any
      inventory read), refuses team- and user-scoped credentials because the
      scan covers the whole organization, and returns the discovery report as
      JSON or a branchable error code. It calls the route's undecorated handler
      and classifies DB failures itself (session contract, lock timeout,
      conflict, migration required, unavailable) instead of inheriting the REST
      wrapper's generic mapping (`api/mcp_server.py::discover_system_map`,
      `core/mcp/scope_validator.py`;
      `tests/unit/mcp/test_mcp_system_map_discovery.py`)
- [ ] No MCP authoring, publish, diff, restore or overview surface beyond
      discovery: `discover_system_map` is the only System Maps MCP tool. An
      agent cannot create or update a map, author a revision, publish / archive
      / diff / restore a revision, read a revision or its history, or read the
      overview through MCP (`api/mcp_server.py`)
- [x] The revision sidebar switches between the current and a historical
      revision through the version-aware read model: "View current" calls
      `GET /revisions/current`, and a `datetime-local` picker calls
      `GET /revisions/by-time?at=<ISO8601>` after converting the local value to
      an absolute instant client-side; the matched revision loads through the
      shared per-revision read path and becomes the active revision, the
      currently published revision is badged Current, and the 404 that
      `by-time` raises for an unmatched instant surfaces as a distinct
      "No revision was active at that time" error instead of silently leaving
      the previous graph on screen (`frontend/src/stores/systemMaps.ts`,
      `frontend/src/views/SystemMapDetailView.vue`,
      `frontend/src/components/system-maps/SystemMapRevisionSidebar.vue`;
      `systemMapsStoreCrud.spec.ts`, `SystemMapDetailView.spec.ts`,
      `SystemMapComponents.coverage.spec.ts`)
- [x] The revision sidebar reads `GET /revisions/history` when a map opens and
      replaces its session list with every tenant-scoped published, draft and
      archived revision, ordered newest first; selecting an entry loads its
      graph through `GET /revisions/{rev_id}`. A re-read of an already cached
      revision replaces that entry instead of keeping the stale object, so the
      status chip reflects a publish or archive without a full page reload
      (`frontend/src/stores/systemMaps.ts`,
      `frontend/src/views/SystemMapDetailView.vue`,
      `frontend/src/components/system-maps/SystemMapRevisionSidebar.vue`;
      `frontend/src/__tests__/systemMapsStoreCrud.spec.ts`)
- [x] The revision sidebar exposes the existing revision diff and restore-as-new
      actions: authors can compare a historical revision with the published revision
      and see counts for changed nodes, relationships, and loop configurations,
      while restore creates a new draft from the selected revision
      (`frontend/src/components/system-maps/SystemMapRevisionSidebar.vue`,
      `frontend/src/stores/systemMaps.ts`, `frontend/src/views/SystemMapDetailView.vue`)
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
- [x] The dashboard card's "Discover My System" control runs the real
      discovery operation and deep-links to the created draft with
      `?discovery=1`, where the detail view opens the review panel on arrival;
      a failure is surfaced in place while the overview states stay visible
      (`frontend/src/components/dashboard/DashboardSystemOverview.vue`,
      `frontend/src/views/SystemMapDetailView.vue`,
      `frontend/src/components/system-maps/SystemMapDraftReview.vue`;
      `DashboardSystemOverview.spec.ts`, `SystemMapDetailView.spec.ts`)
- [x] BDD coverage ships for the access gate and the revision lifecycle:
      `access_and_discovery.feature` drives the real router through
      `TestClient` for an authorized discovery (201, the report names the
      unpublished draft, infers suggested edges, records honest stage types and
      reads tenant-scoped) plus the four refusals (viewer 403, grantless key
      403, developer mode off 403, feature off 402);
      `revisions.feature` drives an invalid publish returning validation errors
      without publishing, a diff naming the added node, restore-as-new from an
      archived revision (and the draft 409), and the `by-time` read including
      its 404-before-first-publish case
      (`backend/tests/bdd/steps/test_system_maps.py`;
      `backend/tests/bdd/features/system_maps/access_and_discovery.feature`,
      `backend/tests/bdd/features/system_maps/revisions.feature`)

## Known Gaps

- **MCP exposes discovery only.** `discover_system_map` lets an agent run the
  inventory scan and persist an unpublished draft, but there is no MCP tool to
  author a map, publish / archive / diff / restore a revision, read a revision
  or its history, or read the overview; those remain REST-only.
- **Discovery is create-only.** Each discovery persists a new map plus a draft
  revision; a repeat with a colliding name is a 409. There is no re-scan into an
  existing draft, so the "added / removed / ambiguous" reconciliation described
  for a later milestone does not exist yet.
- **No loop dispatch runtime.** Loop configs are authorable, overview loop
  metrics are read, and the admission-attempt / reservation tables and
  repository methods exist, but no runtime consumes them: budgets, cooldowns and
  concurrency are not enforced (deferred to Beta 3).

## QA History

- 2026-11-05: **Improve Architecture product-map walk** - wired the existing
  revision diff endpoint into the System Maps UI. Operators can now compare a
  historical revision with the published revision from the revision sidebar,
  with the result showing changed node, relationship, loop-config, and content
  counts. Added store regression coverage for current-draft and explicit-target
  requests, and kept the MCP-only and re-scan gaps unchanged.

- 2026-11-04: **Improve Architecture product-map walk** - fixed stale revision
  metadata in the history sidebar. `loadRevision` only appended an unseen
  revision, so publishing or archiving (which re-read the revision via
  `loadMap`) left the previously cached object in place and the sidebar kept
  showing the old status chip until a full reload. A re-read now replaces the
  cached entry by id; added regression tests for the direct re-read and for the
  publish path in `systemMapsStoreCrud.spec.ts`. Also reconciled the entry's
  stale Known Gap, which still claimed `revisions/history` was unread after PR
  #2848 wired it into the sidebar.

- 2026-11-03: **Improve Architecture product-map walk (gap freshness)** -
  re-verified the remaining UI gap against `origin/main` after PR #2803 wired
  the revision sidebar's "View current" and by-time switching. The unchecked
  bullet and Known Gap that claimed `revisions/current` and `revisions/by-time`
  were uncalled were false: both are now read from the store and exercised by
  the sidebar, so the behavior is checked and the remaining gap is narrowed to
  the unread `revisions/history` endpoint and the missing diff / restore-as-new
  affordances. Also fixed a stale-error bug in `SystemMapDetailView.selectRevision`:
  it did not clear a previous action error before a revision switch, unlike the
  current and by-time selectors, so a successful selection could leave the prior
  failure banner over a healthy graph. Added a regression test that selects a
  revision after a failed action and asserts the error clears.

- 2026-11-03: **Improve Architecture product-map walk** - re-verified this entry
  against `origin/main` after FAR-2288's MCP-discovery + BDD PR (#2761) and the
  product-map refresh (#2762). Corrected the now-false remaining gaps: MCP has
  a `discover_system_map` tool (discovery only, not authoring/publish/overview),
  so that behavior is checked and the gap narrowed; the two BDD feature files
  (`access_and_discovery.feature`, `revisions.feature`) now execute, so the
  frontmatter cites them instead of leaving `bdd: []`. Wired the one real
  remaining UI gap: the dashboard "Discover My System" button is enabled, runs
  the shipped discovery endpoint and deep-links to the draft so the review panel
  opens on arrival, with the stale "discovery is not available" copy replaced.
  The version-aware-history UI, create-only re-scan and loop runtime stay
  unchecked. `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-10-31: **Improve Architecture product-map walk** - closed the feature-graph
  gap left by FAR-2244 (backend discovery) and FAR-2245 (dashboard draft-review
  card): the shipped System Maps preview had no behavior-tracker entry in either
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
  no-overview fallback stayed static at that point (both were wired on
  2026-11-03 in PR #2768 - see the latest entry); (4) "no route of its own, only
  a dashboard card" - the manifest registers `/system-maps` and `/system-maps/:id`
  against `feat-system-maps` with real list, detail, graph-editor,
  revision-sidebar and discovery components; (5) "no publish flow" and the
  lifecycle-map write invariants (FOR UPDATE lock, pipeline uniqueness,
  normalizer bounds, rebuilt stage projection) - replaced by the System Maps
  repository and `validate_publish` / `validate_bindings`; (6) "an existing
  discovery-owned map with the same name is reused" - any live name collision is
  now a 409. Added the shipped-but-undocumented surface: the revision lifecycle
  (create, publish, archive, delete, diff, restore-as-new), the version-aware
  reads and overview revision selection, attribution bindings, team visibility,
  and `modulo apply` support. Replaced the stale code and test citations with
  the paths that actually exist. Status stays `partial`, with the remaining gaps
  re-verified rather than copied at that date: MCP had no System Maps tool, the
  version-aware history UI was unwired, discovery was create-only, loop dispatch
  had no runtime, the dashboard discover button was disabled, and there was no
  BDD file. Each of those has since been re-checked and superseded by the later
  entries below.
- 2026-11-03: **Access and team-scope accuracy pass** - three verified findings
  corrected against the code, each after reading the cited implementation
  rather than the previous wording. (1) "a caller without the grant is refused
  before any inventory read" was false for legacy JWTs and ungranted keys:
  `_enforce_key_grant` treats `key_grants is None` as a no-op and
  `grants_permit(None, ...)` returns `True`, so the `system_maps_access` grant
  only binds grant-bearing API keys. The bullet now states that contract and
  records that the cited grant-denial test case presents a grant-bearing key,
  so the ungranted-JWT path is untested there. (2) "MCP callers only see the
  `system_maps` flag when they hold the same grant" was false for JWT callers:
  a JWT session sets the MCP grant context to `None`, which `grants_permit`
  does not narrow, so an ungranted JWT operator does see the flag. The claim is
  corrected and the citation now names `tests/unit/mcp/test_mcp_config_tools.py`
  (`TestSystemMapsInternalFlagMCP`,
  `TestSystemMapsAccessUsesSharedGrantCheck::test_ungranted_jwt_caller_keeps_role_behaviour`);
  the previous citation, `test_system_maps_routes.py`, has no flag-visibility
  test. (3) "the repository resolves the team scope" was false:
  `resolve_system_map_team_scope` is registered for registry completeness and
  never route-wired, isolation is the `rls_team_isolation` policy created in
  migration `0341`, `list_maps_by_org` applies only query filters, and
  `test_system_map_models.py` contains no team test. The bullet now names the
  real mechanism and evidence (`tests/architecture/test_team_scope_wiring.py`,
  `tests/integration/test_rls_isolation.py`) and describes the team validators
  as implemented. The intro gate sentence carried the same
  grant-for-everyone assumption and was reworded with it. Status stays
  `partial`; the remaining-gaps list is unchanged.
- 2026-11-03: **MCP discovery and BDD coverage landed after the refresh** -
  PR #2761 merged three seconds after the doc refresh in #2762, so two gap
  claims written here had already gone stale. Verified against `origin/main`
  and corrected: (1) "No MCP System Maps tool" - `api/mcp_server.py` now
  registers `discover_system_map`, a thin transport wrapper over the same
  `POST /api/v1/system-maps/discover` handler (it re-applies the internal gate,
  refuses team- and user-scoped credentials, and classifies DB failures itself),
  pinned by `tests/unit/mcp/test_mcp_system_map_discovery.py`; the remaining
  gap is the absence of an MCP authoring / publish / revision-read / overview
  tool. (2) "No BDD coverage" / `bdd: []` -
  `tests/bdd/features/system_maps/access_and_discovery.feature` and
  `revisions.feature` execute through `steps/test_system_maps.py`, so both are
  now cited in `bdd:` and the coverage is a ticked behavior. Re-verified and
  kept the gaps that genuinely remain: version-aware revision reads are still
  backend-only, discovery is still create-only, and loop dispatch still has no
  runtime. The dashboard discover button was still disabled when this pass ran;
  PR #2768 has since wired it, so that gap is closed on `origin/main`. Status
  stays `partial` (matching the manifest registry) because those gaps are real.
