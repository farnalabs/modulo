---
id: feat-core-schema-union-types
prd: 8.3
delivery-tasks: [task-nv9-schema-union-types]
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
  - backend/tests/bdd/features/schemas/schema_inference.feature
  - backend/tests/bdd/features/schemas/schema_migration.feature
code:
  - backend/src/modulo/core/schema_registry/validation.py
  - backend/src/modulo/core/schema_registry/migration.py
  - backend/src/modulo/core/schema_registry/inference.py
  - backend/src/modulo/core/schema_registry/generation.py
unit-tests:
  - backend/tests/unit/core/test_schema_validation.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/core/test_schema_generation.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/api/test_schema_generate_endpoint.py
  - backend/tests/unit/api/test_schemas_endpoint.py
  - backend/tests/unit/db/test_schema.py
  - backend/tests/integration/crud/test_schema.py
  - backend/tests/integration/crud/test_schema_inference_integration.py

depends-on: [feat-core-schema-system, feat-core-db-abstraction-core]
status: partial
---

# Schema Union Types

Union type validation (oneOf/anyOf) and array schema validation for the Schema Registry.

## Behaviours

### Happy Path

- [x] Valid oneOf union schema passes validation
- [x] Valid anyOf union schema passes validation
- [x] Array schema with items object passes validation
- [x] Array schema with tuple items passes validation
- [x] Non-array type schemas pass array validation (no-op)
- [x] Combined union and array validation passes when both valid
- [x] Nested union in object properties passes validation
- [x] Array with union items passes validation
- [x] Array with contains clause passes validation
- [x] Array with prefixItems passes validation
- [x] Nested array inside union variant passes validation through combined validator
- [x] Schema with anyOf (no explicit type) passes array validation

### Request Validation

- [x] oneOf/anyOf not an array returns validation error
- [x] oneOf/anyOf empty array returns validation error
- [x] Union variant not a JSON Schema object returns validation error
- [x] Union variant missing type or composition keyword returns validation error
- [x] oneOf/anyOf alongside type at same level returns validation error
- [x] Array schema missing items returns validation error
- [x] Array items schema missing type, oneOf/anyOf, or $ref returns validation error
- [x] Tuple item not a JSON Schema object returns validation error

### State & Lifecycle

- [x] Schema migration detects field additions
- [x] Schema migration detects field removals
- [x] Schema migration detects type changes (including string→union transition)
- [x] Schema migration detects type changes (including string→array transition)
- [x] Schema migration detects renames when type matches
- [x] Schema migration does not match renames across different types
- [x] Migration with no changes produces empty plan
- [x] Migration handles schemas with missing properties gracefully
- [x] Apply migration adds new fields as null
- [x] Apply migration removes deleted fields
- [x] Apply migration renames fields preserving values
- [x] Apply migration is idempotent
- [x] Apply migration does not mutate the original data
- [x] Transform field applies transform function on existing field
- [x] Transform field is no-op on missing field

### Edge Cases

- [x] Deeply nested union reports correct error paths (e.g. `deep/oneOf/1/oneOf/0`)
- [x] Array items can be a dict or list (tuple) — both handled
- [x] contains and prefixItems are validated recursively for unions
- [x] Schema with mixed simple type and union at different levels validates correctly
- [x] empty properties or missing properties handled without crash in migration

### Concurrency

- [ ] Schema version creation is explicit action, not auto-save (UNTESTED)
- [ ] Schema versions pinned by snapshots cannot be deleted (UNTESTED)

### Error Handling

- [x] SchemaInferenceError raised on LLM timeout
- [x] SchemaInferenceError raised on LLM call failure
- [x] SchemaInferenceError raised on unparseable LLM response
- [x] SchemaGenerationError raised on LLM timeout
- [x] SchemaGenerationError raised on LLM call failure
- [x] SchemaGenerationError raised on unparseable LLM response
- [x] SchemaInferenceError raised on unexpected backend response type
- [x] SchemaGenerationError raised on unexpected backend response type
- [x] ProgrammingError on list schemas returns 501
- [x] ProgrammingError on create schema returns 501
- [x] ProgrammingError on get schema returns 501
- [x] ProgrammingError on update schema returns 501
- [x] ProgrammingError on deprecate schema returns 501
- [x] ProgrammingError on delete schema returns 501
- [x] ProgrammingError on list schema versions returns 501
- [x] ProgrammingError on create schema version returns 501
- [x] ProgrammingError on get schema version returns 501
- [x] ProgrammingError on list fields returns 501
- [x] ProgrammingError on infer schema returns 501
- [x] ProgrammingError on generate schema returns 501
- [x] ProgrammingError on migrate data returns 501
- [x] SQLAlchemyError on list schemas returns 503
- [x] SQLAlchemyError on create schema returns 503
- [x] SQLAlchemyError on get schema returns 503
- [x] SQLAlchemyError on update schema returns 503
- [x] SQLAlchemyError on deprecate schema returns 503
- [x] SQLAlchemyError on delete schema returns 503
- [x] SQLAlchemyError on list schema versions returns 503
- [x] SQLAlchemyError on create schema version returns 503
- [x] SQLAlchemyError on get schema version returns 503
- [x] SQLAlchemyError on list fields returns 503
- [x] SQLAlchemyError on infer schema returns 503
- [x] SQLAlchemyError on generate schema returns 503
- [x] SQLAlchemyError on migrate data returns 503
- [ ] IntegrityError on create schema (duplicate name per org) returns 409 — UNTESTED, add as unchecked
- [ ] Validate endpoint error for non-dict JSON parsed body returns 400 — UNTESTED, add as unchecked

### Backward Compatibility

- [ ] Minor schema bumps are backward-compatible; breaking changes require major bump (UNTESTED)
- [ ] Deprecated schema versions still selectable with warning badge (UNTESTED)
- [ ] Pipelines running against deprecated schema version succeed (no runtime error) (UNTESTED)

### Resilience & Integration Robustness

- [x] Connector sampling has 30s timeout (asyncio.timeout)
- [ ] No timeout on ModelBackendHub.initialise call
- [ ] No retry logic for connector sampling failures
- [ ] No retry logic for LLM inference/generation failures
- [ ] No connection pooling for model backend hub
- [ ] No circuit breaker for external connector sampling

## QA History

### 2026-07-04 — Cross-cutting QA (index 166)

**Behaviour corrections:**
- Marked 39+ [ ]→[x] implemented behaviours across Happy Path, Request Validation, State & Lifecycle, Edge Cases, and Error Handling sections
- Added Error Handling subsection for ProgrammingError/SQLAlchemyError catches on all 14 DB routes
- Added Resilience & Integration Robustness section

**Code fixes applied:**
- Added `SQLAlchemyError` catch → 503 to all 14 DB-accessing routes in `schemas.py` (previously only caught `ProgrammingError` → 501, allowing connection/deadlock failures to propagate as 500)

**Tests added:**
- Created `test_schema_programming_error.py` with 28 tests covering ProgrammingError→501 and SQLAlchemyError→503 for all 14 DB routes

**Known Gaps remaining (not fixed):**
- No concurrency tests for schema version creation race conditions
- No backward compatibility integration tests for deprecated schema versions
- No BDD coverage for union/array validation (feature files only cover inference and migration)
- `connectors/schema_inference.feature` is a placeholder with no real step definitions
- Schema version lifecycle (deprecation → hard delete) not tested
- No retry/backoff for LLM inference/generation failures
- No timeout on ModelBackendHub.initialise
- No IntegrityError catch on create_schema (duplicate name per org)
- `update_schema_endpoint` field filtering strips None values (cannot clear nullable fields via PATCH)

## Known Gaps
- PRD 8.3 states "No union/collection types in alpha" but validation code exists — spec needs updating to reflect implementation
- BDD feature files referenced by `test_alpha_schemas.py` do not exist (`features/schemas/create.feature`, `version.feature`, `deletion_protection.feature`)
- `schema_inference.feature` is a placeholder with no real scenarios
- No BDD coverage for union/array validation
- No concurrency tests for schema version creation race conditions
- No backward compatibility integration tests for deprecated schema versions
- Schema version lifecycle (deprecation → hard delete) not tested 