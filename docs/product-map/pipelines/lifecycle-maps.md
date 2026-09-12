---
id: feat-lifecycle-maps
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/lifecycle_maps.py
  - backend/src/modulo/core/lifecycle_map
unit-tests:
  - backend/tests/unit/core/test_lifecycle_map.py
  - backend/tests/unit/core/test_lifecycle_map_versions.py
  - backend/tests/unit/core/test_lifecycle_map_import_export.py
  - backend/tests/unit/api/test_lifecycle_maps_routes.py
  - backend/tests/unit/db/test_lifecycle_refs.py
  - backend/tests/integration/test_lifecycle_map_import_export.py
  - backend/tests/integration/test_lifecycle_map_concurrency.py
bdd:
  - backend/tests/bdd/features/lifecycle_maps/crud.feature
  - backend/tests/bdd/features/lifecycle_maps/versioning.feature
  - backend/tests/bdd/features/lifecycle_maps/library.feature
  - backend/tests/bdd/features/lifecycle_maps/graduation.feature
depends-on:
  - feat-pipelines
status: covered
---

# Lifecycle Maps

Lifecycle maps and stage workflows: versioned, composable stage definitions with
import/export, journey tracking, stage graduation, and visual editor support.
Maps define the lifecycle stages a pipeline's work items flow through, with
edges representing transitions between stages.

## Behaviours

- [x] Lifecycle map CRUD: create, list, get, update, soft-delete, restore with
      name deduplication and org-scoped RLS
      (`backend/src/modulo/api/routes/lifecycle_maps.py`,
      `backend/tests/bdd/features/lifecycle_maps/crud.feature`)
- [x] Version management: content save bumps version, metadata-only updates do
      not; sequential saves produce unique increasing versions; FOR UPDATE row
      locking prevents concurrent-save conflicts
      (`backend/tests/bdd/features/lifecycle_maps/versioning.feature`,
      `backend/tests/unit/core/test_lifecycle_map_versions.py`,
      `backend/tests/integration/test_lifecycle_map_concurrency.py`)
- [x] Content validation rejects duplicate stage/edge IDs, dangling edges,
      cycles, and invalid stage types
      (`backend/tests/unit/core/test_lifecycle_map_versions.py`)
- [x] Import/export supports v1/v2 envelope formats with version history replay
      and library primitive contribution
      (`backend/tests/bdd/features/lifecycle_maps/library.feature`,
      `backend/tests/unit/core/test_lifecycle_map_import_export.py`,
      `backend/tests/integration/test_lifecycle_map_import_export.py`)
- [x] Stage graduation advances work items through stages with audit logging
      (`backend/tests/bdd/features/lifecycle_maps/graduation.feature`)
- [x] Journey tracking with keyset pagination (cursor-based) for unattributed
      journeys, stage grouping, and self-report endpoint
      (`backend/tests/unit/api/test_lifecycle_maps_routes.py`)
- [x] Canonical work-item ref canonicalisation (GitHub, Linear, JIRA) with
      deterministic uuid5 derivation ensures same ref forms collapse to same
      journey row (`backend/tests/unit/db/test_lifecycle_refs.py`)
- [x] Frontend visual editor with auto-layout (Sugiyama-style layered graph),
      version selection, import/export dialog, and journey detail views
      (`frontend/src/stores/lifecycleMaps.ts`,
      `frontend/src/__tests__/lifecycleMapLayout.spec.ts`)

## Known Gaps

- No BDD for lifecycle map journey detail view; coverage is via unit tests.

## QA History

- 2026-09-12: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/lifecycle-maps/:id/editor`: the whole-page view
  `lifecycle-map/LifecycleMapEditorView.vue` now maps to its owning view so a newly
  shipped testid on the lifecycle-map editor can no longer silently stay invisible to
  Remy's docs indexer / `/api/v1/manifest`.

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/lifecycle-maps`: the whole-page view(s) `LifecycleMapList.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **improve-architecture (product-map walk)** — registered the
  journey-overflow chip testid (`journey-overflow-chip`) shipped by
  `components/lifecycle-map/LifecycleMapRenderer.vue` in the `/lifecycle-maps/:id`
  manifest `elements:` inventory and added the renderer to the reverse testid-coverage
  guard (`test_mapped_route_elements_cover_owning_view_testids`), so the capped-journeys
  chip can no longer drift invisible to Remy's docs indexer / `/api/v1/manifest`.
- 2026-09-10: **improve-architecture (product-map walk)** — registered the lifecycle
  map-journeys detail-view testids (`lifecycle-map-journeys-*`, `lifecycle-map-show-work-items`)
  in the manifest `elements:` inventory and added `LifecycleMapView.vue` to the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`).
- 2026-09-07: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for `feat-lifecycle-maps`, which previously had no
  `docs/product-map/` entry. Behaviours verified against
  `routes/lifecycle_maps.py`, `core/lifecycle_map/`, and the lifecycle-map
  unit+BDD suites. Status: covered.
