---
id: feat-core-contribution-update
prd: 8.14
delivery-tasks: [task-nv8-contribution-update]
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/ratings.feature
code:
  - backend/src/modulo/core/library_service/
  - backend/src/modulo/api/routes/contributions.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/db/crud/library_primitive.py
depends-on: [feat-core-contribution-provenance]
unit-tests:
  - backend/tests/unit/api/test_contributions.py
  - backend/tests/unit/api/test_contributions_programming_error.py
status: covered
---

# Contribution Update

Submitting new versions of published community contributions, listing version history, and notifying downstream copies of available updates — the update side of the contribution workflow.

## Behaviours

### Contribution versioning

- [x] Submit new version of published contribution creates draft row with auto-incremented minor version
- [x] New version row linked to original via version_group_id
- [x] Submitting version of non-published contribution raises ContributionInvalidTransitionError
- [x] Version parse failure (non-numeric) falls back to 1.0
- [x] Non-existent contribution raises ContributionNotFoundError
- [x] Malformed content_json in version submission raises validation error (Pydantic ContributeFixtureRequest validates fixture_map type)

### Version listing

- [x] List all versions for a contribution, newest first
- [x] Primitives without version_group_id return themselves as sole version in list_contribution_versions
- [x] Existing primitives created before version_group_id feature get seeded on first version submission with their own id as group_id
- [x] fork_copies list uses subquery to find all versions in group, not just the current row

### Update notifications for forked copies

- [x] Published version triggers notify_importers_of_update which sets update_available_version_id on forked copies
- [x] notify_importers_of_update is no-op for primitives without version_group_id
- [x] update_available_version_id field is nullable — existing copies have null until first publish
- [x] notify_importers_of_update traces all forked_from references across the entire version group, not only the newly published row

### API endpoints

- [x] POST /api/v1/library/contribute/{id}/versions returns 201 with new version's contribution_status
- [x] POST /api/v1/library/contribute/{id}/versions returns 404 for non-existent contribution
- [x] POST /api/v1/library/contribute/{id}/versions returns 409 for non-published source
- [x] GET /api/v1/library/contribute/{id}/versions returns version list with total
- [x] GET /api/v1/library/contribute/{id}/versions returns 404 for non-existent contribution

### Error handling (ProgrammingError)

- [x] POST /api/v1/library/contribute returns 501 on ProgrammingError with migration hint
- [x] POST /api/v1/library/contribute/{id}/submit returns 501 on ProgrammingError with migration hint
- [x] POST /api/v1/library/contribute/{id}/publish returns 501 on ProgrammingError with migration hint
- [x] POST /api/v1/library/contribute/{id}/versions returns 501 on ProgrammingError with migration hint
- [x] GET /api/v1/library/contribute/{id}/versions returns 501 on ProgrammingError with migration hint
- [x] GET /api/v1/library/contribute returns 501 on ProgrammingError with migration hint

### Edge cases

- [x] Empty version string falls back to 1.0 via IndexError caught in try/except
- [x] Non-numeric version parts fall back to 1.0 via ValueError caught in try/except
- [ ] Version with single part ("1") — split gives ["1"], minor incremented to "2"
- [x] Very long version string handled by split on any length
- [x] Same version number submitted twice allowed since version_group_id differs
- [x] notify_importers_of_update with None version_group_id returns early (no-op)
- [x] notify_importers_of_update when forked_from points to non-existent primitive — subquery returns empty list
- [x] Fork copies with auto_update=False are skipped in notify_importers_of_update

## Known Gaps
- No BDD feature files exist for contribution versioning — all test coverage is in unit tests (test_contributions.py, test_contributions_programming_error.py)
- No BDD tests for the notify_importers_of_update workflow
- No BDD tests for version auto-increment logic
- No integration test verifying that publish_contribution triggers notify_importers_of_update across version group forks
- Unit tests for version auto-increment edge cases (non-numeric, overflow, single-part) exist only implicitly through submit_contribution_version behaviour tests — no dedicated test cases exercise the try/except fallback at lines 1714-1718

## QA History

### 2026-07-04 — Cross-cutting QA pass (index 138)
- Added ProgrammingError→501 catches to all 6 DB-accessing routes in contributions.py
- Created test_contributions_programming_error.py with 6 test cases covering all routes
- Updated unit-tests frontmatter to include both test files
- Marked all existing behaviour checkboxes as [x] (verified against code)
- Added Error Handling section with 6 checkboxes (all [x])
- Added Edge Cases section with 8 checkboxes documenting boundary behaviour
- Updated Known Gaps to reflect current state — test coverage gap remains for BDD and integration tests