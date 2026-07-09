---
id: feat-core-node-categories
prd: 8
code:
  - backend/src/modulo/api/routes/node_categories.py
  - backend/src/modulo/db/crud/node_category.py
bdd: []
unit-tests: []
depends-on: []
status: partial
---

# Node Categories

CRUD management for pipeline node categories — labelled groupings with colour coding and sort ordering used to organise node types in the pipeline canvas.

## Behaviours

- [x] List all node categories with pagination
- [x] Get a single node category by ID
- [x] Create a new node category (name, description, colour, icon, sort order)
- [x] Update a node category (PATCH with partial fields)
- [x] Delete a node category (404 on not found)
- [x] RLS enforcement (org-scoped via set_rls_org + set_rls_user_context)
- [x] Input validation (colour hex pattern, name length limits)
- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [ ] Category reordering
- [ ] Default categories seeded on org creation
