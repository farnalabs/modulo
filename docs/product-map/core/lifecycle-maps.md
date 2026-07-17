---
id: feat-core-lifecycle-maps
prd: 8.31
bdd:
  - backend/tests/bdd/features/lifecycle_maps/crud.feature
  - backend/tests/bdd/features/lifecycle_maps/graduation.feature
  - backend/tests/bdd/features/lifecycle_maps/library.feature
  - backend/tests/bdd/features/lifecycle_maps/versioning.feature
code:
  - backend/src/modulo/api/routes/lifecycle_maps.py
  - backend/src/modulo/core/lifecycle_map/
  - backend/src/modulo/db/models/lifecycle_map.py
  - frontend/src/components/lifecycle-map/
  - frontend/src/stores/lifecycleMaps.ts
  - frontend/src/types/lifecycleMap.ts
  - frontend/src/views/lifecycle-map/
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
- [ ] Persisted version history can be listed and inspected
- [ ] Graduation changes a stage to a linked Modulo pipeline through a dedicated API operation
- [ ] Lifecycle maps can be exported, imported, and shared as library primitives

## Error Handling

- [x] Map CRUD routes catch `ProgrammingError` → 501
- [x] Map CRUD routes catch `SQLAlchemyError` → 503
- [x] Map CRUD routes catch `IntegrityError` → 409
- [x] Map CRUD routes catch `Exception` → 500 with logging
- [x] Missing map ID returns 404
- [ ] No error handling for invalid graph structure in map content

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
