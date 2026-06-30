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

status: partial
---
# Contribute Primitive

Users can create draft fixture contributions, submit them for review, and (as an admin/owner) publish them to the community library. Currently scoped to `test_fixture` primitive type only.

## Behaviours

- [ ] Create draft fixture contribution — POST /api/v1/library/contribute returns 201 with draft status
- [ ] Required fields: name, slug, fixture_map — missing either returns 422
- [ ] Optional fields: description, tags, source_run_id, source_pipeline_id, owner_team_id
- [ ] Submit draft for review — POST .../submit moves draft → review_queue, returns 200
- [ ] Submit non-existent contribution returns 404
- [ ] Submit already-published or already-in-review contribution returns 409
- [ ] Publish reviewed contribution — POST .../publish moves review_queue → published, visibility becomes community
- [ ] Publish by non-admin/non-owner returns 403
- [ ] Publish non-existent contribution returns 404
- [ ] Publish draft or already-published contribution returns 409
- [ ] List contributions — GET /api/v1/library/contribute returns paginated results, filtered to test_fixture type
- [ ] List with contribution_status filter — only matching items returned
- [ ] Submit new version of published contribution — POST .../versions returns 201, auto-increments version, starts as draft
- [ ] Submit version on non-existent contribution returns 404
- [ ] Submit version on non-published original returns 409
- [ ] List versions of a contribution — GET .../versions returns all versions
- [ ] List versions on non-existent contribution returns 404
- [ ] Built-in community "Example Test Fixture" exists with fixture_map containing 2 entries
- [ ] Draft contribution is visible only to submitting org (visibility="org")
- [ ] Contribution lifecycle: draft → review_queue → published
- [ ] Contribution status constants: "draft", "review_queue", "published"
- [ ] Contribution is stored as `primitive_type: test_fixture` with content_json containing fixture_map
- [ ] Contribution stores source provenance: source_run_id, source_pipeline_id
- [ ] Contribution supports owner_team_id for team-scoped visibility
- [ ] Contribution has contribution_status field for workflow state tracking
- [ ] Published contribution is reassigned to community sentinel org
- [ ] notify_importers_of_update is called on publish
- [ ] Non-test_fixture primitive types (schema, workflow, agent, integration) cannot be contributed ## Known Gaps - No BDD feature file exists for the contribution flow (only browse, copy-to-adapt, and ratings)
- Only `test_fixture` primitive type is supported — schema, workflow, agent, and integration contributions not yet implemented
- No trust tier / Ed25519 signing integration for published contributions
- No contribution rejection / feedback workflow
- No contributor attribution beyond author string
- No review queue UI or admin approval dashboard 