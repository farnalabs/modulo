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

## Known Gaps

- Lifecycle Maps are specified after the roadmap sections in the PRD; the section number is valid but the PRD ordering should be normalised separately.
- The version-history, graduation, and library BDD scenarios currently cover only base map storage; their full named workflows are not implemented.
- The feature remains partial while the PRD's analytics and dogfooding delivery tasks are incomplete.
