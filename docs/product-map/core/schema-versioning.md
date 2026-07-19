---
id: feat-core-schema-versioning
prd: 8.3
bdd:
  - backend/tests/bdd/features/schemas/version.feature
unit-tests:
  - backend/tests/unit/api/test_schemas_endpoint.py
  - backend/tests/unit/api/test_schema_programming_error.py
code:
  - backend/src/modulo/api/routes/schemas.py
  - backend/src/modulo/db/crud/schema.py
  - backend/src/modulo/db/models/schema.py
delivery-tasks: []
depends-on: [feat-core-schema-system]
status: partial
---

# Schema Versioning

Manage schema version lifecycle: create, list, and retrieve specific versions of a JSON Schema.

## Behaviours

### SchemaVersion CRUD

- [x] Create schema version with version, version_number, definition_json, published → 201
- [x] Create schema version for non-existent schema → 404
- [x] List schema versions with pagination → 200
- [x] List versions for non-existent schema → 404
- [x] Get specific schema version by version string → 200
- [x] Get non-existent schema version → 404
- [x] Handle schemas with no versions → 404

### Feature Gate

- [x] List/create/get versions endpoints gated behind `schema_version_history` feature flag

### Error Handling

- [x] ProgrammingError on list schema versions returns 501
- [x] ProgrammingError on create schema version returns 501
- [x] ProgrammingError on get schema version returns 501
- [x] SQLAlchemyError on list schema versions returns 503
- [x] SQLAlchemyError on create schema version returns 503
- [x] SQLAlchemyError on get schema version returns 503
- [x] IntegrityError on create schema version (duplicate) returns 409
- [x] Exception returns 500 with logging

## Known Gaps

- **version.feature BDD scenarios use placeholder-style paths** (`/api/schemas/review-input/versions/1` instead of actual UUID-based API) — may not execute correctly
- **No deprecation version lifecycle** — versions cannot be individually deprecated (only the parent schema)
- **No concurrency tests** for schema version creation race conditions

## QA History
- 2026-07-12: Round 3 QA (improve-architecture batch 4). Fixed MINOR — removed unused `import logging` and `_log = logging.getLogger(__name__)` from `crud/schema.py` (dead code, never referenced). B904 audit: `crud/schema.py` clean (ProgrammingError handlers return PageResult, no bare re-raises). `schemas.py` version endpoints (list_versions, create_version, get_version) have proper B904 (`from None`), CancelledError guards, and HTTPException re-raise pattern. `models/schema.py` clean — no dead code. All frontmatter paths verified on disk. Status: partial.
