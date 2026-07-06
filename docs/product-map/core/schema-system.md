---
id: feat-core-schema-system
prd: 8.3
bdd:
  - backend/tests/bdd/features/library/schemas.feature
  - backend/tests/bdd/features/schemas/schema_migration.feature
  - backend/tests/bdd/features/schemas/schema_inference.feature
  - backend/tests/bdd/features/connectors/schema_inference.feature
unit-tests:
  - backend/tests/unit/api/test_schemas_endpoint.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/api/test_schema_generate_endpoint.py
  - backend/tests/unit/api/test_schema_inference_bdd.py
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/core/test_schema_generation.py
  - backend/tests/unit/core/test_schema_validation.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/db/test_schema.py
  - backend/tests/unit/mcp/test_schema_version_resource.py
  - backend/tests/unit/library/test_schema_seeds.py
  - backend/tests/unit/core/composite_engine/test_schema_mapping.py
code:
  - backend/src/modulo/api/routes/schemas.py
  - backend/src/modulo/db/crud/schema.py
  - backend/src/modulo/db/models/schema.py
  - backend/src/modulo/core/schema_registry/__init__.py
  - backend/src/modulo/core/schema_registry/inference.py
  - backend/src/modulo/core/schema_registry/generation.py
  - backend/src/modulo/core/schema_registry/validation.py
  - backend/src/modulo/core/schema_registry/migration.py
  - backend/src/modulo/core/schema_registry/_common.py
delivery-tasks: []
depends-on: []
status: partial
---

# Core Schema System

CRUD for Schema and SchemaVersion, JSON Schema validation, import, migration, and LLM-assisted inference/generation.

## Behaviours

### Schema CRUD

- [x] Create a schema with name, description, abstract_name → 201
- [x] Create a schema with only name (minimal) → 201
- [x] Create schema fails with empty name (422)
- [x] Get schema by ID → 200
- [x] Get non-existent schema → 404
- [x] List schemas with pagination (page, page_size) → 200
- [x] List schemas returns total count
- [x] Update schema name → 200
- [x] Update schema description → 200
- [x] Update schema abstract_name → 200
- [x] Update non-existent schema → 404
- [x] Delete schema with no references → 204
- [x] Delete non-existent schema → 404
- [x] Delete schema with Agent references → 409 (SchemaDeletionProtectedError)
- [x] Delete schema with PipelineSnapshot schema_pins_json references → 409
- [x] Delete schema with LibraryPrimitive content_json references → 409
- [x] Force-delete schema (force=true skips all reference checks) → 204
- [x] Deprecate schema → 200 (deprecated=true, deprecated_at set)
- [x] Deprecate non-existent schema → 404
- [x] Deprecated schema returned with deprecated flag

### SchemaVersion CRUD

- [x] Create schema version with version, version_number, definition_json, published → 201
- [x] Create schema version for non-existent schema → 404
- [x] List schema versions with pagination → 200
- [x] List versions for non-existent schema → 404
- [x] Get specific schema version by version string → 200
- [x] Get non-existent schema version → 404

### Schema Validation

- [x] Validate valid JSON Schema Draft 2020-12 → 200, valid=true
- [x] Validate invalid JSON Schema (wrong type) → 200, valid=false
- [x] Return structured error messages with line/column hints
- [x] Validate rejects oneOf/anyOf that is not a non-empty array
- [x] Validate rejects oneOf/anyOf alongside type at same level
- [x] Recurse into nested properties for sub-schema validation

### Schema Import

- [x] Parse raw JSON Schema content → 200 with name, description, fields
- [x] Reject invalid JSON → 400
- [x] Reject non-object input → 400
- [x] Reject invalid JSON Schema → 422
- [x] Extract fields from properties, mark required fields

### Schema Migration

- [x] Compute migration plan between two schema definitions
- [x] Detect field additions, removals, type changes, renames
- [x] Dry-run returns plan without modifying data
- [x] Dry-run does not alter original data
- [x] Apply migration transforms data between versions
- [x] Handle missing source/target schema → 404
- [x] Handle schemas with no versions → 404
- [x] Handle empty/missing properties gracefully
- [x] Migration is idempotent, does not mutate original data

### Schema Inference (POST /infer)

- [x] Accept connector_instance_id and sample_query → 200
- [x] Infer JSON Schema from sample records via LLM
- [x] Return definition_json, sample_count, suggestion_name, suggestion_description
- [x] Default limit to 10 when omitted
- [x] Reject connector instance not found → 404
- [x] Reject unsupported connector types → 400
- [x] Reject no model backends configured → 400
- [x] Handle connector sampling failure → 502
- [x] Handle inference failure → 502
- [x] Require authentication → 401/403
- [x] Set RLS org scope from authenticated principal
- [x] Reject limit < 1 or > 100 → 422

### Schema Generation (POST /generate)

- [x] Accept description and optional examples → 200
- [x] Generate JSON Schema via LLM from description
- [x] Reject no model backends configured → 400
- [x] Handle generation failure → 502

### Auth & RLS

- [x] List schemas (authenticated) → 200
- [x] List schemas (unauthenticated) → 401/403
- [x] RLS org scope enforced via set_rls_org on all routes
- [x] ProgrammingError caught on all DB-backed routes → 501

### Known Gaps

- **No `force=true` BDD scenario verified end-to-end** — unit test exists in `test_schema_programming_error.py` but no Gherkin `.feature` scenario
- **No graph validation warning for deprecated schemas** — no alert when a pipeline uses a deprecated schema
- **No admin UI listing pipelines pinned to deprecated schemas**
- **No pre-run compatibility check** between node input/output schemas before pipeline execution
- **No major/minor semver compatibility enforcement** — schema versions are not checked for breaking changes
- **No draft version editing** — unpublished versions cannot be edited freely (only "New version" pattern)
- **No frontend unit tests** for SchemaEditorView or SchemaListView
- **Unique constraint**: org + schema name enforced at DB level but no graceful duplicate-name error in API
- **Abstract schemas**: abstract_name field exists but no dedicated endpoint to list or filter by abstract schemas
- **Pinned-version edit block**: PRD §8.3 specifies that editing an existing version's fields is blocked if the version is pinned by any agent — no enforcement exists yet
- **Deprecation warning in schema picker**: PRD §8.3 specifies deprecated schema versions should show a deprecation badge in the picker — not yet implemented

### QA History

- 2026-07-02: Cross-cutting QA — enriched product map from stub to partial, expanded deletion protection to check PipelineSnapshot (schema_pins_json) and LibraryPrimitive (content_json) references, added force=true parameter to delete_schema and delete endpoint, added unit tests for force=true deletion scenario.
- 2026-07-06: Cross-cutting QA — verified behaviours match code (force delete, deprecation endpoint, ProgrammingError handling on all routes), cleaned up resolved known gaps, added missing PRD gaps (pinned-version edit block, deprecation badge), created website docs stub at `Website/modulo-website/src/docs/schemas/core-schema-system.md`.
