---
id: feat-core-schema-deletion
prd: 8.3
bdd:
  - backend/tests/bdd/features/schemas/deletion_protection.feature
unit-tests:
  - backend/tests/unit/api/test_schema_programming_error.py
  - backend/tests/unit/api/test_schemas_endpoint.py
code:
  - backend/src/modulo/api/routes/schemas.py
  - backend/src/modulo/db/crud/schema.py
  - backend/src/modulo/db/models/schema.py
delivery-tasks: []
depends-on: [feat-core-schema-system]
status: partial
---

# Schema Deletion Protection

Prevent accidental deletion of schemas that are in use by agents, pipeline snapshots, or library primitives.

## Behaviours

### Deletion Protection

- [x] Delete schema with no references → 204
- [x] Delete non-existent schema → 404
- [x] Delete schema with Agent references → 409 (SchemaDeletionProtectedError)
- [x] Delete schema with PipelineSnapshot schema_pins_json references → 409
- [x] Delete schema with LibraryPrimitive content_json references → 409
- [x] Force-delete schema (force=true skips all reference checks) → 204

### Error Handling

- [x] ProgrammingError on delete returns 501
- [x] SQLAlchemyError on delete returns 503
- [x] Exception returns 500 with logging
- [x] SchemaDeletionProtectedError returns 409 with structured detail

## Known Gaps

- **No force=true BDD scenario** — unit test exists but no Gherkin `.feature` scenario
- **Delete only checks 3 reference types** — other entities may reference schemas (e.g. environment profiles, run templates)
- **No integration test** verifying that deletion protection works across actual DB constraints
- **No concurrency test** for force-delete racing with reference creation

## QA History

### 2026-07-12 — Round 3 (systemic sweep: B904, exc_info, dead code)
- No code issues found in entry code paths (schemas.py clean from earlier passes)
- Frontmatter valid; Known Gaps remain accurate
