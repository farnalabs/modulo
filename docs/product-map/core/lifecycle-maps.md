---
id: feat-core-lifecycle-maps
prd: 8.31
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/lifecycle_maps/crud.feature
  - backend/tests/bdd/features/lifecycle_maps/graduation.feature
  - backend/tests/bdd/features/lifecycle_maps/library.feature
  - backend/tests/bdd/features/lifecycle_maps/versioning.feature
code:
  - backend/src/modulo/api/routes/lifecycle_maps.py
  - backend/src/modulo/core/lifecycle_map/
  - backend/src/modulo/db/models/lifecycle_map.py
  - backend/src/modulo/db/models/journey.py
  - backend/src/modulo/db/lifecycle_refs.py
  - frontend/src/components/lifecycle-map/
  - frontend/src/stores/lifecycleMaps.ts
  - frontend/src/types/lifecycleMap.ts
  - frontend/src/views/lifecycle-map/
unit-tests:
  - backend/tests/unit/core/test_lifecycle_map.py
depends-on: [feat-core-pipeline-execution]
status: partial
---

# Lifecycle Maps

Declarative, versioned maps of an organisation's delivery lifecycle. A map
documents stages and transitions across pipelines, external systems, and manual
work without acting as a pipeline execution engine.

## Behaviours

- [x] Organisation-scoped lifecycle maps can be created, listed, read, updated, and archived through the REST API
- [x] Maps support organisation or team visibility, with a team owner required for team visibility
- [x] Map content is stored as a graph of stages and transitions
- [x] The frontend provides list, detail, and visual editor routes
- [x] Content updates increment the map version while metadata-only updates preserve it
- [x] Stage content can represent manual, external, placeholder, and Modulo-managed stages
- [x] Persisted version history can be listed and inspected (version list/GET endpoints; only the active version is served)
- [x] Graduation changes a stage to a linked Modulo pipeline through a dedicated API operation (PATCH .../versions/{version_id}/stages/{stage_id}/graduate)
- [x] Lifecycle maps can be exported as a portable JSON envelope (GET .../export)
- [x] Lifecycle maps can be imported from that envelope to create a new map, validated with editor-save rules (POST /import)
- [x] Imported/exported lifecycle maps are registered as `lifecycle_map` library primitives and can be copied-to-adapt into a new map (POST /libraries/{id}/create-lifecycle-map)

## Journeys

- [x] Journeys are minted from a run's work_item_refs (canonical kind/ref) at run create time
- [x] Map-scoped journey list endpoint with keyset pagination and optional kind/ref filter
- [x] Journey detail returns run history with status, provenance, and completion date
- [x] Map canvas renders provenance badges for journey runs
- [x] Advancement service moves journeys between stages with compare-and-set semantics and stage resolution
- [x] Self-report parse extracts stage identity from run outputs
- [x] Reconciliation sweep detects and fixes journey/stage drift
- [x] Journey reads are org-scoped and gated by run.list permission

## Error Handling

- [x] Map CRUD routes catch `ProgrammingError` → 501
- [x] Map CRUD routes catch `SQLAlchemyError` → 503
- [x] Map CRUD routes catch `IntegrityError` → 409
- [x] Map CRUD routes catch `Exception` → 500 with logging
- [x] Missing map ID returns 404
- [x] Invalid graph structure in map content is rejected with 422 on import (and editor save)

## Edge Cases

- [x] Empty map content (no stages or transitions) — stored and returned
- [x] Team visibility without owner_team_id — rejected on create
- [x] Map with single stage — displayed correctly
- [ ] Map with circular stage transitions — no cycle detection
- [ ] Concurrent map content update while version history is being read

## Security

- [x] Auth required for all map endpoints
- [x] Org-scoped — cross-org access returns 404
- [x] Team visibility enforces team membership for access
- [ ] No audit logging for map operations

## Known Gaps

- Immutable per-version snapshot history with browse-back is not retained — only the active version is served (GET of a non-current version returns 404); the version counter and version API surface exist.
- Export is active-version-only: the envelope carries the current stages/edges/notes graph, not full version history. Importing an envelope creates a NEW map; there is no in-place version restore or version-history import.
- The library browser exposes lifecycle maps via the `lifecycle_map` primitive type and copy-to-adapt, but there is no community contribution path for maps yet (the community-contribute primitive_type regex does not include `lifecycle_map`).

## QA History

- 2026-08-13 — FAR-174: marked export/import + `lifecycle_map` library primitive behaviour as implemented (active-version envelope export, import with editor-save validation, copy-to-adapt via /libraries/{id}/create-lifecycle-map).
- 2026-08-12 — Product-state sync: added Journeys section (FAR-141–145), marked version persistence + graduation implemented, updated code paths.
