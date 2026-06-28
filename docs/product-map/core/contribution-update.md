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
status: partial
---
# Contribution Update Submitting new versions of published community contributions, listing version history, and notifying downstream copies of available updates — the update side of the contribution workflow. ## Behaviours ### Contribution versioning
- [ ] Submit new version of published contribution creates draft row with auto-incremented minor version
- [ ] New version row linked to original via version_group_id
- [ ] Submitting version of non-published contribution raises ContributionInvalidTransitionError
- [ ] Version parse failure (non-numeric) falls back to 1.0
- [ ] Non-existent contribution raises ContributionNotFoundError
- [ ] Malformed content_json in version submission raises validation error ### Version listing
- [ ] List all versions for a contribution, newest first
- [ ] Primitives without version_group_id return themselves as sole version in list_contribution_versions
- [ ] Existing primitives created before version_group_id feature get seeded on first version submission with their own id as group_id
- [ ] fork_copies list uses subquery to find all versions in group, not just the current row ### Update notifications for forked copies
- [ ] Published version triggers notify_importers_of_update which sets update_available_version_id on forked copies
- [ ] notify_importers_of_update is no-op for primitives without version_group_id
- [ ] update_available_version_id field is nullable — existing copies have null until first publish
- [ ] notify_importers_of_update traces all forked_from references across the entire version group, not only the newly published row ### API endpoints
- [ ] POST /api/v1/library/contribute/{id}/versions returns 201 with new version's contribution_status
- [ ] POST /api/v1/library/contribute/{id}/versions returns 404 for non-existent contribution
- [ ] POST /api/v1/library/contribute/{id}/versions returns 409 for non-published source
- [ ] GET /api/v1/library/contribute/{id}/versions returns version list with total
- [ ] GET /api/v1/library/contribute/{id}/versions returns 404 for non-existent contribution ## Known Gaps - No BDD feature files exist for contribution versioning — all test coverage is in unit tests
- No BDD tests for the notify_importers_of_update workflow
- No BDD tests for version auto-increment logic
- No unit tests for version auto-increment edge cases (non-numeric, overflow, single-part)
- No integration test verifying that publish_contribution triggers notify_importers_of_update across version group forks 