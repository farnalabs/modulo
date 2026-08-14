---
id: feat-core-node-categories
prd: 8
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/node_categories.py
  - backend/src/modulo/db/crud/node_category.py
  - backend/src/modulo/db/models/node_category.py
bdd:
  - backend/tests/bdd/features/admin/node-categories.feature
unit-tests:
  - backend/tests/unit/api/test_node_category_endpoint.py
  - backend/tests/unit/db/crud/test_org_scoping.py
  - backend/tests/unit/db/crud/test_node_category_referential_integrity.py
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

## Error Handling

- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [x] Duplicate name returns 409 Conflict (IntegrityError→409)
- [x] Invalid colour hex pattern returns 422
- [x] Name length exceeds limit returns 422
- [x] Delete non-existent returns 404
- [x] Catch-all Exception→500 with logger.exception
- [ ] `asyncio.CancelledError` not explicitly re-raised — caught by `except Exception`

## Edge Cases

- [x] Empty node category list returns empty paginated response
- [x] Duplicate name across different orgs allowed (org-scoped uniqueness)
- [x] Colour hex with/without `#` prefix normalised
- [ ] Very long name truncation behaviour not defined
- [ ] Colour value outside visible spectrum stored but not displayed correctly
- [x] Deleting a category in use by nodes — refused with 409 naming the referencing pipelines

## Security

- [x] Auth required (401 for unauthenticated)
- [x] RLS org scoping on all CRUD operations
- [ ] Owner team scoping not implemented (visible to all org members)

## Known Gaps

- [x] ~~**Deleting a category in use by nodes**~~ — **RESOLVED (2026-08-14)**: `node_category_id` references live inside each pipeline's `graph_nodes_json` JSON column, so there was no relational FK to enforce integrity. `soft_delete_node_category` now scans the org's active pipelines via the new `node_category_in_use()` helper and raises `NodeCategoryInUseError` when a node still references the category; the delete route maps it to 409 naming the referencing pipelines. Soft-deleted pipelines and non-dict JSON entries are ignored; other-org references never block the delete.

## QA History

### 2026-08-14 — improve-architecture: referential-integrity guard on category deletion

**RESOLVED the "Deleting a category in use by nodes — no referential integrity check" known gap** (`db/crud/node_category.py` + `api/routes/node_categories.py`). `node_category_id` references live per-node inside each pipeline's `graph_nodes_json` JSON column (not a relational FK), so soft-deleting a category could leave pipeline graphs dangling a reference to a deleted category. (1) New `node_category_in_use(session, category_id, *, org_id)` scans the org's active (non-soft-deleted) pipelines for graph nodes whose `node_category_id` matches, returning the referencing pipelines; non-dict JSON entries are ignored and other-org pipelines are never considered. (2) `soft_delete_node_category` now calls the check first and raises the new `NodeCategoryInUseError` (carries `category_id` + referencing pipelines, `ValueError`-compatible) instead of deleting. (3) The delete route maps the error to 409 with a detail naming the referencing pipelines (`Cannot delete: the category is referenced by N pipeline(s): <names>`) — the previous 409 branch only ever fired on `IntegrityError`, which this check (lacking any FK) could never produce. **Tests** — 10 new CRUD tests in `test_node_category_referential_integrity.py` (empty scan, referencing pipeline reported with id/name, only-matching-category, other-org ignored, soft-deleted-pipeline ignored, malformed entries ignored, delete blocked + error fields, delete succeeds unreferenced, delete succeeds after reference removed, other-org delete unaffected) + 2 new endpoint tests in `test_node_category_endpoint.py` (409 with the pipeline named, 409 lists all referencing pipelines). **BDD** — new `node-categories.feature` (3 scenarios: unreferenced delete → 204, referenced delete → 409 naming the pipeline, viewer delete → 403) with co-located step definitions in `test_node_categories.py`. Updated product map (`bdd:` + `unit-tests:` frontmatter populated, Edge Case behaviour `[ ]`→`[x]`, Known Gap → RESOLVED, QA History). 10/10 referential-integrity + 20/20 node-category endpoint + 20/20 org-scoping + 3/3 BDD scenarios + 16/16 category-validator unit tests pass, ruff check + format clean, mypy --strict clean. Status: partial (category reordering, default category seeding, `asyncio.CancelledError` re-raise, owner-team scoping remain).
