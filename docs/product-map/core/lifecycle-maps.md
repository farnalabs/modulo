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
  - backend/tests/unit/core/test_lifecycle_map_import_export.py
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
- [x] The export envelope is `format_version: 2` and carries the version history (`versions` array of each version's stages/edges/notes + metadata); import recreates the version chain deterministically (exported numbers preserved when contiguous 1..N, otherwise re-derived), and a `format_version: 1` payload (no `versions`) still imports as a single-version map (FAR-204)
- [x] Imported/exported lifecycle maps are registered as `lifecycle_map` library primitives and can be copied-to-adapt into a new map (POST /libraries/{id}/create-lifecycle-map)
- [x] Lifecycle maps can be contributed to the community library via the standard community-contribute flow (POST /libraries/community/contribute accepts `primitive_type: lifecycle_map`, FAR-204)

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
- [x] Invalid graph structure in map content is rejected with 422 on create, update, version save, import, and editor save — `normalize_content` (`core/lifecycle_map/validation.py`) rejects circular stage transitions (DFS back-edge detection naming the offending stage path), dangling edges (edge endpoint referencing an undefined stage), and duplicate stage/edge ids, each raising `LifecycleMapContentError` with a specific message
- [x] Audit writes are best-effort and fail-open — an audit failure never turns a committed map mutation into an error response

## Edge Cases

- [x] Empty map content (no stages or transitions) — stored and returned
- [x] Team visibility without owner_team_id — rejected on create
- [x] Map with single stage — displayed correctly
- [x] Map with circular stage transitions — rejected at save (cycle detection in `normalize_content` reports the offending stage path as a 422)
- [x] Map with a dangling edge (edge endpoint referencing an undefined stage) — rejected at save with a 422 naming the offending stage
- [x] Map with duplicate stage or edge ids — rejected at save with a 422 naming the duplicate id
- [x] Concurrent map content update while version history is being read — **decided (FAR-176): last-write-wins on the single active version; version-bumping write paths fetch the map row with `SELECT ... FOR UPDATE` so the counter is strictly increasing with no duplicates under concurrent saves; version-list reads always observe one consistent committed snapshot (never a half-written map).**

## Security

- [x] Auth required for all map endpoints
- [x] Org-scoped — cross-org access returns 404
- [x] Team visibility enforces team membership for access
- [x] Map CRUD operations are written to the org audit chain (`append_audit_event`, fail-open) — create/import, metadata update, editor version save (POST/PUT `/versions`), delete, and stage graduation each record `lifecycle_map.*` events keyed by map id and actor

## Known Gaps

- Immutable per-version snapshot history with browse-back is not retained — only the active version is served (GET of a non-current version returns 404); the version counter and version API surface exist. Concurrency semantics (FAR-176): concurrent saves are last-write-wins on the active version, serialised by a row lock (`SELECT ... FOR UPDATE`) so the version counter is atomic and never produces duplicates; version-list reads always see one consistent committed snapshot. The version-history EXPORT/IMPORT envelope carries a `versions` array, so history that exists is preserved across export/import even though the DB does not retain intermediate snapshots between saves.
- Importing a multi-version envelope replays each snapshot through the version-save path (chain recreated deterministically); because the DB does not persist intermediate snapshots, only the final version's graph is retained in the org — the full chain is preserved in the library primitive's `content_json.export.versions`.

## QA History

- 2026-08-14 — FAR-204: version-history export/import + community-contribute. `build_export_envelope` is now `format_version: 2` and carries a `versions` array (each version's canonical stages/edges/notes + version number/created_at); import accepts `format_version` 1 or 2 — a v1 payload (no `versions`) imports as a single-version map (backward compatible), a v2 payload replays each snapshot through `save_map_version` so the chain is recreated deterministically (exported numbers preserved when contiguous 1..N, otherwise re-derived 1..N; malformed version entries → 422). Copy-to-adapt (`materialize_map_from_primitive`) recreates the chain from a v2 primitive's `export.versions`. Community-contribute (`POST /libraries/community/contribute`) now accepts `primitive_type: lifecycle_map`. Added unit tests (v1 backward compat, single/multi-version import, deterministic ordering, malformed entries, primitive version-history) + 5 BDD scenarios.
- 2026-08-14 — FAR-175: extended `normalize_content` graph validation to reject dangling edges and duplicate stage/edge ids (in addition to the existing cycle detection) with 422, and added best-effort audit logging (`lifecycle_map.created/.updated/.deleted/.stage_graduated`) to the map mutation routes — including the editor's version-save endpoints (POST/PUT `/versions`). Added unit tests (duplicate ids, dangling edges, edge-without-stages) + route audit tests + 2 BDD scenarios; frontend `useApi` now throws `formatApiError`-formatted errors so FastAPI array-typed 422 `detail` reaches the editor's `saveError` as readable text instead of `[object Object]`.
- 2026-08-13 — FAR-174: marked export/import + `lifecycle_map` library primitive behaviour as implemented (active-version envelope export, import with editor-save validation, copy-to-adapt via /libraries/{id}/create-lifecycle-map).
- 2026-08-13 — improve-architecture: **RESOLVED** the "circular stage transitions" gap. `normalize_content` (`core/lifecycle_map/validation.py`) now runs DFS-based cycle detection over the normalised `edges` (back-edge detection with three-state colouring; self-loops reported as `[n, n]`); any cycle raises `LifecycleMapContentError` naming the offending stage path (`s1 -> s2 -> s1`), which the create/update/version-save routes map to 422. Added 7 unit tests (`test_normalize_content_*` in `test_lifecycle_map_versions.py`: acyclic chain accepted, 2-node cycle, self-loop, transitions-alias cycle, 3-node cycle path, cycle-path naming, unconnected/parallel edges) + 1 BDD scenario (`Circular stage transitions are rejected as invalid content` in `versioning.feature`) with a new step definition. 63/63 `test_lifecycle_map_versions.py` + 181 focused lifecycle-map unit/BDD tests pass, ruff clean, mypy --strict clean.
- 2026-08-12 — Product-state sync: added Journeys section (FAR-141–145), marked version persistence + graduation implemented, updated code paths.
