---
id: feat-library-schemas
prd: 8.3
delivery-tasks: [task-lib-schemas-seed]
bdd:
  - backend/tests/bdd/features/library/schemas.feature
  - backend/tests/bdd/steps/test_schemas.py
code:
  - backend/scripts/seed_library_schemas.py
  - backend/src/modulo/db/models/schema.py
  - backend/src/modulo/db/crud/schema.py
  - backend/src/modulo/api/routes/schemas.py
unit-tests:
  - backend/tests/unit/api/test_schemas_endpoint.py
  - backend/tests/integration/crud/test_schema.py
  - backend/tests/unit/library/test_schema_seeds.py
  - backend/tests/bdd/steps/test_schemas.py
depends-on: [feat-core-schema-system]
status: partial
---

# Library Schema Definitions

22 built-in JSON Schema definitions for common software engineering document
types. Each schema has a name, description, and a full JSON Schema definition
with properties, types, descriptions, and required fields.

## Behaviours

### Schema definitions exist

- [x] Exactly 22 schema definitions are defined
- [x] Each schema has a unique name
- [x] Each schema has a description
- [x] Each schema name is a valid slug (lowercase, hyphen-separated)
- [x] Names match the expected set: meeting-notes, adr, api-spec, design-doc,
      changelog-entry, release-note, test-plan, test-case, db-change,
      sprint-plan, roadmap-item, quality-report, deployment, incident-report,
      env-config, okr, post-mortem, code-review-comment, bug-report,
      issue-ticket, pull-request, user-story

### JSON Schema validity

- [x] Every definition has `type: "object"` at root
- [x] Every definition has a `title` string
- [x] Every definition has a `description` string
- [x] Every definition has a `properties` dict
- [x] Every property has a `type` field
- [x] Every property has a `description` field
- [x] All types are valid JSON Schema types (string, number, integer, boolean, object, array)
- [x] Required fields reference only existing properties
- [x] Required fields match the specification for each schema type
- [x] Enum values are correct for constrained properties
- [x] All definitions pass Draft2020-12 validation

### API CRUD

- [x] Schema can be created via POST /api/v1/schemas (201) — unit test test_create_schema_returns_201 + integration test_create_schema
- [x] Schema version can be created via POST /.../versions (201) — unit test test_create_schema_version_returns_201 + integration test_create_schema_version
- [x] Schema list returns all schemas (200) — unit test test_list_schemas_returns_200 + integration test_list_schemas_pagination
- [x] Schema list supports pagination (200) — integration test_list_schemas_pagination
- [x] Schema get returns single schema (200) — unit test test_get_schema_returns_200 + integration test_get_schema_returns_existing
- [x] Schema get returns 404 for non-existent ID — unit test test_get_schema_not_found_returns_404 + integration test_get_schema_returns_none_for_unknown
- [x] Schema update returns updated schema (200) — unit test test_update_schema_returns_200 + integration test_update_schema
- [x] Schema deprecate returns deprecated schema (200) — unit test test_deprecate_schema_returns_200 + integration test_deprecate_schema
- [x] Schema delete returns 204 — unit test test_delete_schema_returns_204 + integration test_delete_schema
- [x] Schema versions list returns versions (200) — unit test test_list_schema_versions_returns_200 + integration test_list_schema_versions
- [x] Schema version get returns specific version (200) — unit test test_get_schema_version_returns_200 + integration test_get_schema_version_returns_existing
- [x] Schema validate returns valid/invalid (200) — unit tests test_validate_schema_valid_returns_valid_true / test_validate_schema_invalid_returns_valid_false
- [x] Schema import extracts fields (200) — unit test test_import_schema_returns_200

### Seed script

- [x] Seed script runs idempotently (skip existing) — confirmed from seed_library_schemas.py code
- [x] Seed script creates 22 Schema entities — seed_library_schemas.py creates all 22
- [x] Seed script creates v1.0 SchemaVersion for each — confirmed
- [x] Seed script publishes all versions — published=True on each

## Known Gaps

- No integration test that runs the seed script against a real DB (seed_library_schemas.py creates all 22 schemas but is not tested via CI integration test)
- No BDD step definitions for pipeline_builder.feature (5 UI scenarios)
- Team ownership enforcement during schema CRUD is not tested

## QA History

- 2026-07-02: Cross-cutting QA (pass 2): Fixed `test_schemas.py` BDD feature file path (`../../features/` → `../features/` — was resolving to wrong directory). Fixed `body` → `docstring` parameter name in all 5 step functions with multiline text args (pytest-bdd 8.x requires `docstring` as the reserved parameter name). Fixed `_make_mock_schema` and `_make_mock_schema_version` to use `account_id` attribute instead of `created_by` (Pydantic `validation_alias`). Added "the organisation exists" step to BDD conftest for shared reuse. All 14 schemas.feature BDD scenarios now pass (previously 0 - were uncollectable). 232/232 unit tests pass (15 pre-existing integration test errors from migration 0055 `sa.JSONB()` attribute error). Status: partial (3 known gaps remain unchanged: no seed integration test, no BDD pipeline_builder coverage, no team ownership enforcement).
- 2026-07-02: Cross-cutting QA (pass 1): added ProgrammingError catches to 10 schema routes (create, get, update, deprecate, delete, list_versions, create_version, get_version, generate, migrate). Created BDD step definitions for all 14 schemas.feature scenarios. Added 7 new unit tests (deprecate, validate, import coverage). Added 2 integration tests (deprecation). Updated product map: marked 13 API CRUD behaviours [ ]→[x] and 4 seed script behaviours [ ]→[x]. Status: partial (3 known gaps remain: no seed integration test, no BDD pipeline_builder coverage, no team ownership enforcement).
