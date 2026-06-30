---
id: feat-library-schemas
prd: 8.3
delivery-tasks: [task-lib-schemas-seed]
bdd:
  - backend/tests/bdd/features/library/schemas.feature
code:
  - backend/scripts/seed_library_schemas.py
  - backend/src/modulo/db/models/schema.py
  - backend/src/modulo/db/crud/schema.py
  - backend/src/modulo/api/routes/schemas.py
unit-tests:
  - backend/tests/unit/library/test_schema_seeds.py
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

- [ ] Schema can be created via POST /api/v1/schemas (201)
- [ ] Schema version can be created via POST /.../versions (201)
- [ ] Schema list returns all schemas (200)
- [ ] Schema list supports pagination (200)
- [ ] Schema get returns single schema (200)
- [ ] Schema get returns 404 for non-existent ID
- [ ] Schema update returns updated schema (200)
- [ ] Schema deprecate returns deprecated schema (200)
- [ ] Schema delete returns 204
- [ ] Schema versions list returns versions (200)
- [ ] Schema version get returns specific version (200)
- [ ] Schema validate returns valid/invalid (200)
- [ ] Schema import extracts fields (200)

### Seed script

- [ ] Seed script runs idempotently (skip existing)
- [ ] Seed script creates 22 Schema entities
- [ ] Seed script creates v1.0 SchemaVersion for each
- [ ] Seed script publishes all versions

## Known Gaps

- No integration test that runs the seed script against a real DB
- No test that seed script output matches test expectations
