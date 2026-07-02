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

depends-on: []
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

## Known Gaps
- Only `test_fixture` primitive type is supported — schema, workflow, agent, and integration contributions not yet implemented
- No trust tier / Ed25519 signing integration for published contributions
- No contribution rejection / feedback workflow
- No contributor attribution beyond author string
- No review queue UI or admin approval dashboard

## QA History

- 2026-07-02: improve-architecture index 44 — marked 28 behaviours [x], added 4 missing update_returns_none behaviours, added service-layer unit tests for submit_contribution_version and list_contribution_versions, added BDD feature file for contribution flow, created website docs stub 