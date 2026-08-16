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
- [ ] Category reordering (sort_order is a plain PATCHable field; no dedicated reorder endpoint)
- [ ] Default categories seeded on org creation

## Error Handling

- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [x] Duplicate name returns 409 Conflict (IntegrityError→409)
- [x] Invalid colour hex pattern returns 422
- [x] Name length exceeds limit returns 422
- [x] Delete non-existent returns 404
- [x] Catch-all Exception→500 with logger.exception
- [x] `asyncio.CancelledError` is explicitly re-raised before `except Exception` in all 6 route handlers (fixed 2026-08-15; verified by `TestCancelledErrorPropagation`)

## Edge Cases

- [x] Empty node category list returns empty paginated response
- [x] Duplicate name across different orgs allowed (org-scoped uniqueness)
- [x] Colour validation constrains values to `#RRGGBB` hex — a value outside the visible spectrum cannot be stored
- [x] Name length is bounded at 100 chars — a longer name is rejected with 422 (validated, not truncated); verified by `test_overlong_name_returns_422` / `test_name_at_max_length_accepted`
- [x] Deleting a category in use by nodes — refused with 409 naming the referencing pipelines
- [ ] Colour hex without a `#` prefix is rejected 422, NOT normalised — the create/update models require the `^#[0-9a-fA-F]{6}$` pattern (the earlier "with/without `#` prefix normalised" claim was inaccurate; verified by `test_color_without_hash_prefix_returns_422`)

## Security

- [x] Auth required (401 for unauthenticated)
- [x] RLS org scoping on all CRUD operations
- [ ] Owner team scoping not implemented (visible to all org members)

## Known Gaps

- [x] ~~**Deleting a category in use by nodes**~~ — **RESOLVED (2026-08-14)**: `node_category_id` references live inside each pipeline's `graph_nodes_json` JSON column, so there was no relational FK to enforce integrity. `soft_delete_node_category` now scans the org's active pipelines via the new `node_category_in_use()` helper and raises `NodeCategoryInUseError` when a node still references the category; the delete route maps it to 409 naming the referencing pipelines. Soft-deleted pipelines and non-dict JSON entries are ignored; other-org references never block the delete.
- **Category reordering** — `sort_order` is a plain column updated via PATCH, but there is no dedicated reorder endpoint or bulk sort operation; the list orders by `sort_order, name`.
- **No default categories seeded on org creation** — a fresh org starts with an empty `node_categories` table; no seed data is inserted at org creation.
- **Colour without a `#` prefix is rejected, not normalised** — the API models require `^#[0-9a-fA-F]{6}$`, so a bare `6366f1` returns 422 rather than being auto-prefixed.
- **Owner-team scoping not implemented** — node categories are org-scoped but visible to all org members; there is no per-team owner/visibility model.

## QA History

### 2026-08-14 — improve-architecture: referential-integrity guard on category deletion

**RESOLVED the "Deleting a category in use by nodes — no referential integrity check" known gap** (`db/crud/node_category.py` + `api/routes/node_categories.py`). `node_category_id` references live per-node inside each pipeline's `graph_nodes_json` JSON column (not a relational FK), so soft-deleting a category could leave pipeline graphs dangling a reference to a deleted category. (1) New `node_category_in_use(session, category_id, *, org_id)` scans the org's active (non-soft-deleted) pipelines for graph nodes whose `node_category_id` matches, returning the referencing pipelines; non-dict JSON entries are ignored and other-org pipelines are never considered. (2) `soft_delete_node_category` now calls the check first and raises the new `NodeCategoryInUseError` (carries `category_id` + referencing pipelines, `ValueError`-compatible) instead of deleting. (3) The delete route maps the error to 409 with a detail naming the referencing pipelines (`Cannot delete: the category is referenced by N pipeline(s): <names>`) — the previous 409 branch only ever fired on `IntegrityError`, which this check (lacking any FK) could never produce. **Tests** — 10 new CRUD tests in `test_node_category_referential_integrity.py` (empty scan, referencing pipeline reported with id/name, only-matching-category, other-org ignored, soft-deleted-pipeline ignored, malformed entries ignored, delete blocked + error fields, delete succeeds unreferenced, delete succeeds after reference removed, other-org delete unaffected) + 2 new endpoint tests in `test_node_category_endpoint.py` (409 with the pipeline named, 409 lists all referencing pipelines). **BDD** — new `node-categories.feature` (3 scenarios: unreferenced delete → 204, referenced delete → 409 naming the pipeline, viewer delete → 403) with co-located step definitions in `test_node_categories.py`. Updated product map (`bdd:` + `unit-tests:` frontmatter populated, Edge Case behaviour `[ ]`→`[x]`, Known Gap → RESOLVED, QA History). 10/10 referential-integrity + 20/20 node-category endpoint + 20/20 org-scoping + 3/3 BDD scenarios + 16/16 category-validator unit tests pass, ruff check + format clean, mypy --strict clean. Status: partial (category reordering, default category seeding, `asyncio.CancelledError` re-raise, owner-team scoping remain).

### 2026-08-15 — distribute (partial→covered sweep)

- **Implemented (`asyncio.CancelledError` guards):** all 6 `node_categories.py` route handlers now re-raise `asyncio.CancelledError` before the `except Exception` catch, so a cancelled request is never mislabelled as an HTTP 500 (guards matter on Python < 3.12 where CancelledError subclasses Exception). Verified by 3 new direct-call tests in `TestCancelledErrorPropagation` (list/create/delete).
- **Marked [x]:** name-length and colour-scope edge cases — names are bounded at 100 (422 beyond, not truncated) and colours are constrained to `#RRGGBB` hex (an out-of-spectrum value cannot be stored). Added `test_overlong_name_returns_422`, `test_name_at_max_length_accepted`, `test_color_without_hash_prefix_returns_422`.
- **Corrected an inaccurate [x] claim:** "Colour hex with/without `#` prefix normalised" was false — the create/update models require the `#` prefix (bare `6366f1` → 422). Marked unchecked with a Known Gap note.
- **Confirmed genuine gaps** (left unchecked): category reordering endpoint, default-category seeding at org creation, owner-team scoping.
