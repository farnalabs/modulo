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
  - backend/tests/unit/core/test_lifecycle_map_versions.py
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
- [ ] Lifecycle maps can be exported, imported, and shared as library primitives

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
- [x] Invalid graph structure in map content — circular stage transitions rejected (validation raises `LifecycleMapContentError` → 422)

## Edge Cases

- [x] Empty map content (no stages or transitions) — stored and returned
- [x] Team visibility without owner_team_id — rejected on create
- [x] Map with single stage — displayed correctly
- [x] Map with circular stage transitions — rejected at save (cycle detection in `normalize_content` reports the offending stage path as a 422)
- [x] Concurrent map content update while version history is being read — **decided (FAR-176): last-write-wins on the single active version; version-bumping write paths fetch the map row with `SELECT ... FOR UPDATE` so the counter is strictly increasing with no duplicates under concurrent saves; version-list reads always observe one consistent committed snapshot (never a half-written map).**

## Security

- [x] Auth required for all map endpoints
- [x] Org-scoped — cross-org access returns 404
- [x] Team visibility enforces team membership for access
- [ ] No audit logging for map operations

## Known Gaps

- Immutable per-version snapshot history with browse-back is not retained — only the active version is served (GET of a non-current version returns 404); the version counter and version API surface exist. Concurrency semantics (FAR-176): concurrent saves are last-write-wins on the active version, serialised by a row lock (`SELECT ... FOR UPDATE`) so the version counter is atomic and never produces duplicates; version-list reads always see one consistent committed snapshot.
- Lifecycle maps cannot yet be exported / imported / shared as library primitives.

## QA History

- 2026-08-13 — improve-architecture: **RESOLVED** the "circular stage transitions" gap. `normalize_content` (`core/lifecycle_map/validation.py`) now runs DFS-based cycle detection over the normalised `edges` (back-edge detection with three-state colouring; self-loops reported as `[n, n]`); any cycle raises `LifecycleMapContentError` naming the offending stage path (`s1 -> s2 -> s1`), which the create/update/version-save routes map to 422. Added 7 unit tests (`test_normalize_content_*` in `test_lifecycle_map_versions.py`: acyclic chain accepted, 2-node cycle, self-loop, transitions-alias cycle, 3-node cycle path, cycle-path naming, unconnected/parallel edges) + 1 BDD scenario (`Circular stage transitions are rejected as invalid content` in `versioning.feature`) with a new step definition. 63/63 `test_lifecycle_map_versions.py` + 181 focused lifecycle-map unit/BDD tests pass, ruff clean, mypy --strict clean.
- 2026-08-12 — Product-state sync: added Journeys section (FAR-141–145), marked version persistence + graduation implemented, updated code paths.
