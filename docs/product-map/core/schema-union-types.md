---
id: feat-core-schema-union-types
prd: 8.3
delivery-tasks: [task-nv9-schema-union-types]
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
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

status: partial
---
# Schema Union Types Union type validation (oneOf/anyOf) and array schema validation for the Schema Registry. ## Behaviours ### Happy Path
- [ ] Valid oneOf union schema passes validation
- [ ] Valid anyOf union schema passes validation
- [ ] Array schema with items object passes validation
- [ ] Array schema with tuple items passes validation
- [ ] Non-array type schemas pass array validation (no-op)
- [ ] Combined union and array validation passes when both valid
- [ ] Nested union in object properties passes validation
- [ ] Array with union items passes validation
- [ ] Array with contains clause passes validation
- [ ] Array with prefixItems passes validation
- [ ] Nested array inside union variant passes validation through combined validator
- [ ] Schema with anyOf (no explicit type) passes array validation ### Request Validation
- [ ] oneOf/anyOf not an array returns validation error (UNTESTED)
- [ ] oneOf/anyOf empty array returns validation error
- [ ] Union variant not a JSON Schema object returns validation error
- [ ] Union variant missing type or composition keyword returns validation error
- [ ] oneOf/anyOf alongside type at same level returns validation error
- [ ] Array schema missing items returns validation error
- [ ] Array items schema missing type, oneOf/anyOf, or $ref returns validation error
- [ ] Tuple item not a JSON Schema object returns validation error ### State & Lifecycle
- [ ] Schema migration detects field additions
- [ ] Schema migration detects field removals
- [ ] Schema migration detects type changes (including string→union transition)
- [ ] Schema migration detects type changes (including string→array transition)
- [ ] Schema migration detects renames when type matches
- [ ] Schema migration does not match renames across different types
- [ ] Migration with no changes produces empty plan
- [ ] Migration handles schemas with missing properties gracefully
- [ ] Apply migration adds new fields as null
- [ ] Apply migration removes deleted fields
- [ ] Apply migration renames fields preserving values
- [ ] Apply migration is idempotent
- [ ] Apply migration does not mutate the original data
- [ ] Transform field applies transform function on existing field
- [ ] Transform field is no-op on missing field ### Edge Cases
- [ ] Deeply nested union reports correct error paths (e.g. `deep/oneOf/1/oneOf/0`)
- [ ] Array items can be a dict or list (tuple) — both handled
- [ ] contains and prefixItems are validated recursively for unions
- [ ] Schema with mixed simple type and union at different levels validates correctly
- [ ] empty properties or missing properties handled without crash in migration ### Concurrency
- [ ] Schema version creation is explicit action, not auto-save (UNTESTED)
- [ ] Schema versions pinned by snapshots cannot be deleted (UNTESTED) ### Error Handling
- [ ] SchemaInferenceError raised on LLM timeout
- [ ] SchemaInferenceError raised on LLM call failure
- [ ] SchemaInferenceError raised on unparseable LLM response
- [ ] SchemaGenerationError raised on LLM timeout
- [ ] SchemaGenerationError raised on LLM call failure
- [ ] SchemaGenerationError raised on unparseable LLM response
- [ ] SchemaInferenceError raised on unexpected backend response type
- [ ] SchemaGenerationError raised on unexpected backend response type ### Backward Compatibility
- [ ] Minor schema bumps are backward-compatible; breaking changes require major bump (UNTESTED)
- [ ] Deprecated schema versions still selectable with warning badge (UNTESTED)
- [ ] Pipelines running against deprecated schema version succeed (no runtime error) (UNTESTED) ## Known Gaps
- PRD 8.3 states "No union/collection types in alpha" but validation code exists — spec needs updating to reflect implementation
- BDD feature files referenced by `test_alpha_schemas.py` do not exist (`features/schemas/create.feature`, `version.feature`, `deletion_protection.feature`)
- `schema_inference.feature` is a placeholder with no real scenarios
- No BDD coverage for union/array validation
- No concurrency tests for schema version creation race conditions
- No backward compatibility integration tests for deprecated schema versions
- Schema version lifecycle (deprecation → hard delete) not tested 