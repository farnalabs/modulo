---
id: feat-core-contribute-primitive
prd: 8.14
delivery-tasks: [task-nv8-contribute-primitive]
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/ratings.feature
code:
  - backend/src/modulo/core/library_service/__init__.py
  - backend/src/modulo/api/routes/contributions.py

depends-on: [feat-core-db-abstraction-core]
unit-tests:
  - backend/tests/unit/api/test_contributions.py
  - backend/tests/unit/core/library_service/test_contribute.py
status: partial
---

# Contribute Primitive

Users can create draft fixture contributions, submit them for review, and (as an admin/owner) publish them to the community library. Currently scoped to `test_fixture` primitive type only.

## Behaviours

- [x] Create draft fixture contribution — POST /api/v1/library/contribute returns 201 with draft status
- [x] Required fields: name, slug, fixture_map — missing either returns 422
- [x] Optional fields: description, tags, source_run_id, source_pipeline_id, owner_team_id
- [x] Submit draft for review — POST .../submit moves draft → review_queue, returns 200
- [x] Submit non-existent contribution returns 404
- [x] Submit already-published or already-in-review contribution returns 409
- [x] Publish reviewed contribution — POST .../publish moves review_queue → published, visibility becomes community
- [x] Publish by non-admin/non-owner returns 403
- [x] Publish non-existent contribution returns 404
- [x] Publish draft or already-published contribution returns 409
- [x] List contributions — GET /api/v1/library/contribute returns paginated results, filtered to test_fixture type
- [x] List with contribution_status filter — only matching items returned
- [x] Submit new version of published contribution — POST .../versions returns 201, auto-increments version, starts as draft
- [x] Submit version on non-existent contribution returns 404
- [x] Submit version on non-published original returns 409
- [x] List versions of a contribution — GET .../versions returns all versions
- [x] List versions on non-existent contribution returns 404
- [x] Built-in community "Example Test Fixture" exists with fixture_map containing 2 entries
- [x] Draft contribution is visible only to submitting org (visibility="org")
- [x] Contribution lifecycle: draft → review_queue → published
- [x] Contribution status constants: "draft", "review_queue", "published"
- [x] Contribution is stored as `primitive_type: test_fixture` with content_json containing fixture_map
- [x] Contribution stores source provenance: source_run_id, source_pipeline_id
- [x] Contribution supports owner_team_id for team-scoped visibility
- [x] Contribution has contribution_status field for workflow state tracking
- [x] Published contribution is reassigned to community sentinel org
- [x] notify_importers_of_update is called on publish
- [x] Non-test_fixture primitive types (schema, workflow, agent, integration) cannot be contributed
- [x] Update returning None after creation raises ContributionNotFoundError
- [x] Update returning None after submit for review raises ContributionNotFoundError
- [x] Update returning None after publish raises ContributionNotFoundError
- [x] Update returning None after version submission raises ContributionNotFoundError

## Error Handling

### Route-level (contributions.py, library.py)
All 6 contribution endpoints and 3 community contribution endpoints catch `ProgrammingError` and return **501 Not Implemented** with `"Run database migrations to enable it."`. This prevents raw 500 errors when the `contribution_status` column or related tables don't exist yet.

- `POST /api/v1/library/contribute` — wraps `session.begin()` + `contribute_fixture` in try/except ProgrammingError
- `POST /api/v1/library/contribute/{id}/submit` — catches ProgrammingError (501), ContributionNotFoundError (404), ContributionInvalidTransitionError (409)
- `POST /api/v1/library/contribute/{id}/publish` — pre-checks org_role for 403, then catches same three exceptions
- `POST /api/v1/library/contribute/{id}/versions` — wraps `session.begin()` + `submit_contribution_version` in try/except ProgrammingError + 404 + 409
- `GET /api/v1/library/contribute/{id}/versions` — catches ProgrammingError (501), ContributionNotFoundError (404)
- `GET /api/v1/library/contribute` — catches ProgrammingError (501) for the list query
- `POST /api/v1/libraries/community/contribute` — catches ProgrammingError (501)
- `GET /api/v1/libraries/community/contributions` — nested try/except for ProgrammingError (501) + outer try/except Exception (500)
- `POST /api/v1/libraries/admin/library/community/publish/{id}` — catches ContributionNotFoundError (404), ContributionInvalidTransitionError (400), ProgrammingError (501)

### Service-level (library_service/__init__.py)
- `ContributionNotFoundError(LookupError)` — raised when the contribution doesn't exist; routed to 404
- `ContributionInvalidTransitionError(ValueError)` — raised for invalid status transitions (draft→published without review, published→submit again); routed to 409/400
- `update_library_primitive` returning None after creation raises `ContributionNotFoundError` (defensive — catches race between create and update)
- `get_primitive` and `get_primitive_by_slug` catch ProgrammingError internally and return None (graceful degradation when table is missing)

### Exception visibility
- All HTTPException responses include a `detail` string with an actionable message
- `ContributionInvalidTransitionError` passes the error's `str()` as detail, e.g. `"expected 'draft', got 'published'"`
- 401/403 for auth/role failures are handled by FastAPI middleware (`get_current_user`), not route-level code

## Edge Cases

### Status transition validation
- Creating a contribution always sets `contribution_status="draft"` and `visibility="org"`
- `submit` only works from `"draft"`; calling it on `"review_queue"` or `"published"` returns 409
- `publish` only works from `"review_queue"`; calling it on `"draft"` or `"published"` returns 409
- `versions` only works when the original has `contribution_status="published"`; calling on draft/review_queue returns 409
- Status constants are validated at the service layer, not by DB CHECK constraints (DB constraint is a safety net)

### Concurrent operations
- No advisory locking or optimistic concurrency control on status transitions
- Two concurrent `submit` calls on the same draft could both pass the status check if they interleave between the SELECT and UPDATE — the second one would silently overwrite the first's transition
- Same race applies to `publish` — concurrent publishes from two admins could both succeed, with only the last write persisting

### Versioning edge cases
- Version string auto-increment (`_bump_version`) splits on `.` and increments the last segment — "1.0" → "1.1", "2.0.5" → "2.0.6". Malformed versions fall back to "1.0"
- `version_group_id` is seeded on legacy primitives (those created before the versioning feature) on the first `submit_contribution_version` call — the seed is the original primitive's own ID
- Version submission creates a new DB row (not an in-place update), so the original published row remains unchanged
- `list_contribution_versions` includes the seed (original published) primitive in the result set, sorted newest first
- `notify_importers_of_update` is called on publish but only updates `update_available_version_id` on forked copies that have `auto_update=True`

### Community sentinel org reassignment
- On publish, the contribution's `organisation_id` is reassigned to `MODULO_ORG_ID` (00000000-0000-0000-0000-000000000001) — the sentinel org
- After reassignment, the submitting org can no longer query it via org-scoped endpoints
- The published version is also added to the in-memory `_COMMUNITY_PRIMITIVES` cache

### Optional fields
- `owner_team_id` can be set at creation time for team-scoped visibility but is cleared on publish (reassigned to community visibility)
- `source_run_id` and `source_pipeline_id` are stored as UUID in `content_json` but accepted as strings in the request body
- `description`, `tags` are optional at creation; missing fields default to `None`/`[]`

## Resilience

### Database migration resilience
- All 9 contribution/community endpoints catch `ProgrammingError` and return structured 501 responses — the app does not crash when migrations haven't run
- `list_community_contributions_endpoint` has nested error handling: inner `try/except ProgrammingError` for the DB query, outer `try/except HTTPException`/`except Exception` for model validation failures
- Service-layer functions (`get_primitive`, `get_primitive_by_slug`) catch `ProgrammingError` internally and return `None`, allowing read-only fallback to in-memory primitives

### In-memory fallbacks
- Built-in "Example Test Fixture" is an in-memory `_make_modulo` primitive — always available even without the contributions table
- Community primitives published via `publish_contribution` are added to both the DB and the in-memory `_COMMUNITY_PRIMITIVES` cache
- `list_primitives` merges in-memory modulo/community items with DB items and deduplicates by ID
- `_fetch_published_community_from_db` supplements the in-memory cache after server restart (best-effort warm-start)

### Request validation resilience
- Pydantic models enforce field constraints (name 1-255 chars, slug 1-255 chars, fixture_map is `dict[str, str]`)
- Missing `name`, `slug`, or `fixture_map` returns 422 before any DB call
- Version submission reuses the same `ContributeFixtureRequest` model as creation (same validation)
- No silent fallback for missing required fields — the route rejects with 422

### Known gaps in resilience
- No advisory locks around status transitions — concurrent operations can race
- No retry logic for `notify_importers_of_update` — a fork with `auto_update=True` that fails to update is silently skipped
- In-memory community cache is lost on server restart; `_fetch_published_community_from_db` is best-effort only
- No timeout or circuit-breaker on the publish → notify_importers_of_update chain

## Known Gaps
- Only `test_fixture` primitive type is supported — schema, workflow, agent, and integration contributions not yet implemented
- No trust tier / Ed25519 signing integration for published contributions
- No contribution rejection / feedback workflow
- No contributor attribution beyond author string
- No review queue UI or admin approval dashboard

## QA History

- 2026-07-02: improve-architecture index 44 — marked 28 behaviours [x], added 4 missing update_returns_none behaviours, added service-layer unit tests for submit_contribution_version and list_contribution_versions, added BDD feature file for contribution flow, created website docs stub
- 2026-07-05: cross-cutting QA — verified all 30 behaviours against code, confirmed ProgrammingError catches on all 9 contribution/community endpoints, added Error Handling / Edge Cases / Resilience sections, created website docs stub
- 2026-07-12: R2 improve-architecture — verified B904 compliance (all `from None` correct), no CancelledError concerns (Python 3.12+), frontmatter clean, BDD paths exist, no stale known gaps, dead code check clean. All ruff checks pass.
