---
id: feat-system-maps
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/system_maps.py
  - backend/src/modulo/api/schemas/system_maps.py
  - backend/src/modulo/api/dependencies.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
  - backend/src/modulo/core/system_maps_discovery.py
  - backend/src/modulo/core/system_map_persistence.py
  - backend/src/modulo/core/feature_flags.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/db/models/system_map.py
  - backend/src/modulo/db/repositories/system_map.py
  - backend/src/modulo/db/migrations/versions/0346_system_maps_version_aware.py
  - frontend/src/stores/systemMaps.ts
  - frontend/src/components/dashboard/DashboardSystemOverview.vue
  - frontend/src/components/system-maps/SystemMapDraftReview.vue
  - frontend/src/views/DashboardView.vue
  - frontend/src/lib/api/schema.ts
unit-tests:
  - backend/tests/unit/api/test_system_maps_routes.py
  - backend/tests/unit/core/test_system_maps_discovery.py
  - backend/tests/unit/core/test_system_map_persistence.py
  - backend/tests/unit/db/test_system_map_repository.py
  - backend/tests/integration/test_system_maps_discovery.py
  - frontend/src/__tests__/systemMapsStore.spec.ts
  - frontend/src/__tests__/DashboardSystemOverview.spec.ts
  - frontend/src/__tests__/SystemMapDraftReview.spec.ts
  - frontend/src/__tests__/DashboardView.spec.ts
bdd: []
depends-on:
  - feat-lifecycle-maps
status: partial
---

# System Maps (Beta, internal dogfood)

An internal, default-off preview that inspects an organisation's existing
inventory (pipelines, triggers, connector instances and pipeline folders) and
materialises a deterministic, unpublished draft lifecycle map from it. The
surface has no route of its own: the discovery endpoint is
`POST /api/v1/system-maps/discover`, and the only UI is a gated System Overview
card on the existing `/` dashboard route. Because it has no manifest route it is
tracked here rather than in the `frontend/src/manifest.yaml` `features:`
registry (the dashboard route keeps its `feat-dashboard` reference); the
`system_maps` flag itself is a registered internal flag (FAR-2244 / FAR-2245).

Drafts persist through a compatibility adapter into the organisation-scoped
`lifecycle_maps` document until a dedicated System Maps schema lands. The
surface is deliberately invisible to ordinary customers and never publishes
automatically.

## Behaviours

- [x] The `system_maps` flag is a community-tier `FeatureFlag` registered in
      `INTERNAL_FLAGS` and `DEFAULT_OFF_FLAGS`, so it is off by default and
      filtered from customer-facing plan/catalogue responses
      (`effective_feature_flags`); it is visible in the admin feature-flag list
      only to a caller passing the internal System Maps gate, and the list's
      cache key is scoped by that visibility class so an internal response can
      never be served to an ordinary caller or vice versa
      (`core/feature_flags.py`, `api/routes/admin_feature_flags.py`)
- [x] Every discovery request is gated three times and fails closed: the
      router-level `_require_internal_system_maps_access` (developer mode plus
      the dedicated `system_maps_access` grant through `require_in_dev_operator`),
      the `require_feature("system_maps")` entitlement gate (disabled -> 402),
      and the handler's `require_permission("system_maps_access")` role floor; a
      caller without the grant, without dev mode, or below the operator floor is
      refused before any inventory read (`api/routes/system_maps.py`,
      `api/dependencies.py`; `test_system_maps_routes.py`)
- [x] `POST /api/v1/system-maps/discover` inspects only the caller's
      organisation: live pipelines (`deleted_at IS NULL`, `archived_at IS NULL`),
      live triggers, live connector instances and pipeline folders, with an
      explicit `organisation_id` predicate on every inventory query. The RLS
      organisation and user context are bound inside the same
      `session.begin()` block as the reads and the draft write
      (`api/routes/system_maps.py`; `test_system_maps_routes.py` tenant-scoping
      case, `test_system_maps_discovery.py` RLS case)
- [x] Discovery is deterministic and explainable: sources are sorted by
      `(kind, identifier, name)`; nodes are built only for the supported kinds
      (pipeline, trigger, connector, folder); edges are emitted only for
      explicit relationships (trigger targets a pipeline, pipeline belongs to a
      folder), every edge carries `config_json.suggested = true` plus a reason,
      and duplicate edges are de-duplicated in a stable sort order
      (`core/system_maps_discovery.py`; `test_system_maps_discovery.py`)
- [x] Unsupported or unresolvable sources are reported, never guessed: an
      unknown source kind, a trigger with no pipeline binding, a trigger whose
      pipeline is not in the inventory, a pipeline whose folder is not in the
      inventory, and an improvement-loop/tagged pipeline with no supported
      non-self relationship each appear in `skipped` with a reason, and a
      non-empty `skipped` list raises a warning (an empty inventory raises its
      own warning instead) (`core/system_maps_discovery.py`;
      `test_system_maps_discovery.py`)
- [x] Stage types are honest about provenance: a discovery pipeline node maps
      to `modulo`, a trigger node to `manual` (it carries no Modulo pipeline of
      its own), and connector/folder nodes to `placeholder` (external systems,
      never presented as Modulo) before normalisation
      (`api/routes/system_maps.py::_stage_type`;
      `test_system_maps_routes.py`)
- [x] The endpoint returns 201 with a full discovery report (sources inspected,
      node/edge counts, inferred nodes and edges, skipped sources, warnings,
      draft map/revision ids, revision number, `published=false`) and persists
      exactly one draft revision from the same content; the report shape is
      idempotent for an unchanged inventory
      (`api/schemas/system_maps.py`, `api/routes/system_maps.py`;
      `test_system_maps_routes.py`)
- [x] Draft persistence is safe against name collisions with user-authored
      maps: a discovery draft is marked `system_maps_discovery_draft` in
      `content_json`, an existing discovery-owned map with the same name is
      reused, a user-authored map with the same name is refused with 409, and a
      save re-checks discovery ownership. A revision id is a uuid5 over the map
      id and the discovery fingerprint, so an unchanged inventory resolves to
      the same revision identity (`core/system_map_persistence.py`);
      `test_system_map_persistence.py`, `test_system_maps_discovery.py`
- [x] Draft writes reuse the shipped lifecycle-map write invariants: the row is
      locked `FOR UPDATE`, the pipeline-uniqueness check runs, and the derived
      stage projection is rebuilt in the same transaction; the content bounds
      are enforced by the lifecycle-map normaliser, so a draft that violates
      them fails 422 rather than 500, and a pipeline conflict or map-name
      conflict maps to 409 (`api/routes/system_maps.py::_persist_discovery_draft`,
      `core/system_map_persistence.py`)
- [x] Discovery emits a `system_map_discovery_drafted` audit event on resource
      `system_map` for both success and failure through the shared `audited()`
      dependency (`api/routes/system_maps.py`)
- [x] MCP callers only see the `system_maps` flag when they hold the same
      internal System Maps grant (developer mode, operator role floor, grant
      check through `grants_permit`); ordinary callers never see the internal
      flag in flag listings (`api/mcp_server.py::_mcp_flag_visible`)
- [x] The dashboard shows a System Overview card only after the internal access
      probe settles AND the flag resolves enabled for a caller holding both
      `system_maps_access` and `system.config.manage`; the store revalidates on
      every access-token change, rejects stale responses from a previous
      principal, de-duplicates concurrent probes, and fails closed on a missing
      token, a malformed flag payload or a probe error
      (`frontend/src/stores/systemMaps.ts`, `frontend/src/views/DashboardView.vue`;
      `systemMapsStore.spec.ts`, `DashboardSystemOverview.spec.ts`)
- [x] Version-aware revision endpoints (FAR-2288) allow runs and metrics to be
      interpreted against the map revision active at a point in time:
      - `GET /api/v1/system-maps/{map_id}/revisions/by-time?at=<ISO8601>` returns
        the published revision where `effective_from <= at < effective_to`
        (or `effective_to IS NULL` for the current revision)
      - `GET /api/v1/system-maps/{map_id}/revisions/current` returns the
        currently active (published) revision as a semantic alias
      - `GET /api/v1/system-maps/{map_id}/revisions/history` lists all revisions
        with their effective time ranges, marking the current revision with
        `is_current=true`
      - `GET /api/v1/system-maps/{map_id}/overview` accepts optional
        `revision_id` or `at` timestamp parameters to show metrics for a specific
        historical revision's time window
      All endpoints preserve the internal access gate (system_maps feature +
      developer mode + operator/admin + system_maps_access), tenant/RLS
      isolation, and use canonical failure vocabulary
      (`api/routes/system_maps.py`, `api/schemas/system_maps.py`,
      `db/models/system_map.py`, `db/repositories/system_map.py`,
      `test_system_maps_routes.py`, `test_system_map_repository.py`)
- [ ] The draft-review UI is still a placeholder: `fetchDiscovery()` is a
      deliberate no-op, the "Discover My System" button is disabled, and the
      review panel renders explanatory copy only. No page ever calls the
      shipped `POST /api/v1/system-maps/discover` endpoint, even though it is
      present in the generated contract (`frontend/src/lib/api/schema.ts`), and
      there is no System Maps publish flow
      (`frontend/src/stores/systemMaps.ts`, `SystemMapDraftReview.vue`;
      `systemMapsStore.spec.ts`)

## Known Gaps

- **No System Maps UI wires the discovery endpoint.** The backend discovery and
  draft persistence ship, and the request/response types are in the generated
  OpenAPI client, but the dashboard card never issues the request: the store's
  `fetchDiscovery()` only sets `loaded = true` / `discoveryAvailable = false`,
  the discovery button is permanently disabled, and the review panel is static
  copy. This is tracked as the unchecked behaviour above; it needs a UI wiring
  change (out of scope for a product-map walk).
- **No BDD coverage.** Discovery, persistence and gating are covered by unit
  tests and a Postgres integration test, but there is no Gherkin feature file
  for the System Maps surface.
- **No dedicated System Maps schema.** Drafts are stored as lifecycle-map rows
  with a discovery marker until the dedicated System Maps schema (FAR-2236)
  lands, so the surface inherits lifecycle-map content bounds and name
  uniqueness. A name collision with a user-authored lifecycle map is a 409, not
  an automatic suffix.
- **No MCP discovery tool.** The MCP layer only hides/shows the `system_maps`
  flag; there is no equivalent of the REST discovery endpoint exposed as an
  agent tool.

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
