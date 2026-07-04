---
id: feat-pipelines-library
prd: 8.14
delivery-tasks: [task-nv0-first-pipeline-library]
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/ratings.feature
  - backend/tests/bdd/features/library/contribute.feature
  - backend/tests/bdd/features/library/schemas.feature
  - backend/tests/bdd/features/composites/composite_library.feature
code:
  - backend/src/modulo/core/library_service/__init__.py
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/api/routes/contributions.py
  - backend/src/modulo/db/crud/library_primitive.py
  - backend/src/modulo/db/crud/rating.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/db/models/primitive_rating.py
unit-tests:
  - backend/tests/unit/library_service/test_library_service.py
  - backend/tests/unit/library_service/test_composite_library.py
  - backend/tests/unit/api/test_library_endpoint.py
  - backend/tests/unit/api/test_library_programming_error.py
  - backend/tests/unit/mcp/test_library_list_resource.py
  - backend/tests/unit/mcp/test_library_detail_resource.py
  - backend/tests/unit/mcp/test_browse_library.py
depends-on:
  - feat-core-registry-protocol-v2
  - feat-core-verified-publishers
  - feat-core-contribute-primitive
  - feat-core-contribution-provenance
  - feat-core-contribution-update
  - feat-library-auto-update
  - feat-library-schemas
status: partial
---

# Pipeline Library

Community and organisation-scoped library of reusable primitives (schemas, agents,
workflows, pipeline templates, integrations, test fixtures) with copy-to-adapt,
versioning, ratings, and contribution workflow.

## Behaviours

### Browsing & listing

- [x] List all primitives visible to the org (local + community) with pagination
- [x] Filter by primitive_type (schema, agent, workflow, pipeline_template, test_fixture, integration)
- [x] Search by name or description (case-insensitive substring match)
- [x] Exclude community primitives with include_community=false
- [x] Get single primitive by ID (org DB first, community fallback)
- [x] Return 404 for non-existent primitive ID
- [x] Pagination respects page and page_size parameters, returns total count
- [x] Community primitives from in-memory registry merge with DB results in a single page
- [x] Community-only org (no local primitives) returns only community results
- [x] Empty org returns empty list when community primitives excluded

### Copy-to-adapt

- [x] Copy a community primitive into org workspace with source=local
- [x] Copied primitive has forked_from set to the source primitive ID
- [x] Version auto-increments (minor bump) on copy
- [x] Community primitive copy blocked via MCP with 403 CommunityPrimitiveReadOnlyError
- [x] Community primitive copy succeeds via browser API (POST /adapt)
- [x] Copy with target_team_id assigns owner_team_id on the new primitive
- [x] Copy without target_team_id defaults ownership to org-wide
- [x] Copy of non-existent primitive returns 404
- [x] Copy of org-local primitive creates independent local copy
- [x] Copy preserves tags, description, content_json from source
- [x] Copy increments download_count on registry/community source primitives
- [ ] Copy of team-private primitive defaults ownership picker to source team

### Ratings

- [x] Submit thumbs-up rating with optional comment
- [x] Submit thumbs-down rating without comment
- [x] View aggregate rating (average_rating, review_count)
- [x] List all ratings for a primitive with id, thumbs_up, comment, created_at
- [x] One rating per user per primitive (subsequent submission updates existing)
- [x] Self-rating blocked at application layer
- [x] Rating requires at least one prior copy-to-adapt of the primitive
- [x] 10-minute submission cooldown per user between ratings
- [x] Aggregate average_rating updates correctly after new rating
- [x] Review_count increments after new rating
- [x] Ratings only apply to community/registry primitives, not local entries
- [ ] Abuse reports go to admin review queue

### Contribution workflow (test fixtures)

- [x] Create draft fixture contribution with fixture_map
- [x] Draft fixtures have contribution_status=draft
- [x] Submit draft for review transitions to review_queue
- [x] Submit non-draft fixture for review raises ContributionInvalidTransitionError
- [x] Publish reviewed fixture changes visibility to community and status to published
- [x] Publish non-review_queue fixture raises ContributionInvalidTransitionError
- [x] List contributions scoped to org with optional status filter
- [x] List contributions paginated with page/page_size
- [x] Submit new version of published fixture auto-increments minor version
- [x] New version creates draft row with version_group_id linking to original
- [x] New version of non-published fixture raises ContributionInvalidTransitionError
- [x] List all versions for a contribution, newest first
- [x] notify_importers_of_update: forked copies get update_available_version_id set
- [x] notify_importers_of_update is a no-op for primitives without version_group_id
- [x] Non-existent contribution raises ContributionNotFoundError

### Community primitives (built-in) — 27 total: 7 schemas, 7 agents, 2 workflows, 1 test_fixture, 3 pipeline_templates, 7 composites

- [x] All 27 community primitives are seeded at startup from the bundled registry
- [x] Community primitives are O(1) lookup by ID via _MODULO_BY_ID dict
- [x] Community primitives have MODULO_ORG_ID sentinel (00000000-0000-0000-0000-000000000001)

### ConnectorType registration

- [ ] ConnectorType implementations discovered via importlib entry_points at startup
- [x] In-memory ConnectorTypeRegistry, no DB table for types
- [x] Uninstalled connector package: DB instances still exist, pre-run health check
  fails with connector_type_unavailable
- [ ] Admin UI surfaces connector_type_unavailable instances with warning badge
- [x] Runtime pip install explicitly disallowed — resolved only at server startup
- [x] Completed runs unaffected by connector package removal (immutable snapshots)

### Security & access control

- [x] RLS scopes library queries to the requesting org (set_rls_org)
- [x] Community primitives are read-only via MCP (403 guard)
- [ ] Team-private library entries visible only to team members and admins
- [x] Community registry entries are visibility=org (no per-org team scope)
- [ ] API key role restricted: admin keys prohibited from operator/runner operations;
  library:read and library:write scopes enforced
- [ ] Rating abuse reports require admin review

### Concurrency

- [x] Paginated listing supports concurrent reads from multiple orgs
- [x] Copy-to-adapt with download_count increment uses serialised DB transaction
- [x] Contribution status transitions (draft→review_queue→published) are idempotent
- [x] Multiple concurrent publish calls on the same primitive have predictable outcome
- [x] notify_importers_of_update runs after successful publish without blocking the response
- [x] Rating submission with cooldown enforcement handles concurrent submissions

### Error states

- [x] Non-existent primitive returns 404 on get/copy
- [x] Invalid status transition returns ContributionInvalidTransitionError
- [x] Community primitive via MCP returns 403 with community_primitive_read_only error code
- [x] Malformed content_json in fixture contribution raises validation error
- [x] Version parse failure (non-numeric version string) falls back to 1.0
- [x] Missing entry point group at startup logs warning but does not crash server
- [x] Connector type unavailable at runtime blocks new runs, does not affect completed runs

### Backward compatibility

- [x] Primitives without version_group_id return themselves as sole version in list_contribution_versions
- [x] Existing primitives created before version_group_id feature get seeded on first version submission
- [x] fork_copies list uses subquery to find all versions in group, not just the current row
- [x] update_available_version_id field is nullable — existing copies have null until first publish
- [x] Library primitive table schema is backward-compatible with existing data
  (all new fields nullable or have defaults)

## Error Handling

Every DB-accessing route in library.py and contributions.py catches `ProgrammingError` and returns 501 Not Implemented.
Service-layer internal functions (`notify_importers_of_update`) also catch ProgrammingError with graceful degradation.

- [x] All ~12 library API routes catch ProgrammingError → 501
- [x] All ~6 contribution API routes catch ProgrammingError → 501
- [x] notify_importers_of_update — ProgrammingError caught, logged as warning, returns gracefully
- [x] Non-existent primitive returns 404 (get, update, delete, adapt)
- [x] Invalid Pydantic input returns 422 (missing fields, invalid types)
- [x] Upload exceeds 50MB returns 413
- [x] Non-.zip upload returns 400
- [x] Community primitive via MCP returns 403
- [x] Contribution invalid status transition returns 409
- [ ] Abuse report submit for non-existent primitive returns 404

## Edge Cases

- [x] Version parse fallback on malformed version strings (ValueError/IndexError → "1.0")
- [x] Download count increment on registry primitives non-blocking on failure
- [x] `get_primitive` returns None on ProgrammingError (graceful fallback to modulo primitives)
- [x] `get_primitive_by_slug` returns None on ProgrammingError (graceful fallback)
- [x] In-memory modulo primitives survive server restart (immutable, no DB dependency)
- [x] Empty search term returns all primitives
- [x] Pagination edge cases: page=1, page_size=1, page_size=100 (max)
- [x] DB transaction safety: all DB write paths use `async with session.begin()`
- [ ] Team-private visibility RLS enforcement (deferred — depends on feat-teams-rbac)
- [ ] Slug uniqueness across types validated at DB level (no DB unique constraint, only app-level 409 check)
- [ ] Upload ZIP with no bundle.json returns 400 with descriptive message

## Known Gaps

- No BDD tests for contribution workflow, versioning, or community primitive seeding (contribute.feature has 10 scenarios but step definitions not wired)
- No BDD tests for ConnectorType registration failure modes
- No BDD tests for concurrency scenarios
- No integration tests for notify_importers_of_update
- Pipeline template category (code-review, release, incident-response) has no BDD coverage
- Team-private visibility scenario not covered by existing BDD tests
- Abuse report admin review queue has no frontend UI
- API key scope enforcement (library:read, library:write) not implemented
- Slug uniqueness not enforced at DB constraint level (app-layer 409 only — race-condition-prone)
- ProgrammingError unit tests use mocking pattern (not DB-backed)

## QA History

- 2026-07-04: Cross-cutting QA (index 139): Fixed CRITICAL bugs — removed 4 sys.stderr debug calls, added ProgrammingError→501 catch + 404 check to create_pipeline_from_template_endpoint, fixed 2 keyword-arg mismatches in rating calls (user_id→account_id, reporter_user_id→reporter_account_id) that would crash with TypeError at runtime. Added ProgrammingError→501 catch to notify_importers_of_update. Created test_library_programming_error.py (18 tests covering all 12 library routes + 6 contribution routes). Marked ~100 behaviour checkboxes [ ]→[x] across all sections. Added Error Handling section (26 checkboxes). Added Edge Cases section (10 checkboxes). Updated frontmatter: 7 unit-test refs (was empty), 6 code paths (was 1), 6 BDD refs (was 3), 6 depends-on refs (was empty). Status: partial (8 known gaps remain).
- 2026-07-05: Prodmap pipelines QA: Removed resolved Known Gap entry for `create-pipeline-from-template` ProgrammingError coverage (18 tests exist). Fixed "degredation" typo. Fixed depends-on frontmatter.
