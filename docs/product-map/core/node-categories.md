---
id: feat-core-node-categories
prd: 8
code:
  - backend/src/modulo/api/routes/node_categories.py
  - backend/src/modulo/db/crud/node_category.py
  - backend/src/modulo/db/models/node_category.py
bdd: []
unit-tests:
  - backend/tests/unit/api/test_node_category_endpoint.py
  - backend/tests/unit/db/crud/test_org_scoping.py
depends-on:
  - feat-core-db-abstraction-core
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
- [x] Duplicate name returns 409 Conflict (IntegrityError→409)
- [ ] Category reordering
- [ ] Default categories seeded on org creation

## Known Gaps

- No BDD feature file exists for node categories CRUD workflows
- Category reordering API not implemented (no `PUT /reorder` or `sort_order` batch update endpoint)
- Default categories are not seeded on org creation — users start with an empty category list
- No `owner_team_id` field on the model for team-scoped categories (visible to all org members regardless)

## QA History

- 2026-07-11: Cross-cutting QA — added IntegrityError→409 handling, logger.warning on error paths, populated frontmatter (unit-tests, depends-on, bdd reference), documented known gaps
- 2026-07-12: Round 2 improve-architecture QA — verified B904, CancelledError, dead code (all clean). Removed stale known gap about `created_by` tracking: the field IS populated from `principal.account_id` in the create route handler (correct architecture — server-set, not client-supplied). Updated frontmatter note: `prd: 8` confirmed as best available (no specific PRD subsection for node categories). No code changes needed.
