---
id: feat-core-schema-inference
prd: 8.16
delivery-tasks:
  - task-nv5-schema-inference-service
  - task-nv5-schema-infer-endpoint
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
unit-tests:
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_validation.py
  - backend/tests/unit/api/test_schema_programming_error.py
code:
  - backend/src/modulo/core/schema_registry/inference.py
  - backend/src/modulo/core/schema_registry/generation.py
  - backend/src/modulo/core/schema_registry/validation.py
  - backend/src/modulo/core/schema_registry/migration.py
  - backend/src/modulo/api/routes/schemas.py

depends-on: [feat-core-schema-system, feat-core-db-abstraction-core]
status: partial
---

# Schema Inference

LLM-assisted JSON Schema draft from sampled connector data. Entry point for SDLC onboarding.

## Behaviours

### LLM Inference — `SchemaInferenceService.infer()`

- [x] Accept a list of sample record dicts and return an inferred JSON Schema
- [x] Build prompt with system instructions and sample data as human message
- [x] Truncate samples to `_MAX_SAMPLE_RECORDS` (50) before sending to LLM
- [x] Send samples as formatted JSON block in the human message
- [x] Parse LLM response as plain JSON object
- [x] Strip markdown code fences from LLM response (with or without language hint)
- [x] Handle whitespace around JSON and around fences
- [x] Inject default `type: "object"` and `properties: {}` when LLM returns bare `{}`
- [x] Validate that response is a dict, not an array or primitive
- [x] Timeout LLM call after configurable duration (default: 60s) → raises `SchemaInferenceError`
- [x] Propagate LLM invocation errors as `SchemaInferenceError("LLM call failed")`
- [x] Reject non-string `response.content` from backend → `SchemaInferenceError`
- [x] Reject invalid JSON from LLM → `SchemaInferenceError("Failed to parse")`
- [x] Accept configurable `max_sample_records` (constructor arg)
- [x] Accept empty samples list gracefully — LLM sees "0 records", service allows it
- [x] Raise `ValueError` when samples contains non-dict items
- [x] Accept nested object structures in samples
- [x] Accept fields with mixed presence across records (not required)
- [x] Handle all-null records (fields omitted from schema)
- [x] Reject non-serializable samples (circular refs etc.) → `SchemaInferenceError`

### API Endpoint — `POST /api/v1/schemas/infer`

- [x] Accept `connector_instance_id` and `sample_query` (resource, filters, limit)
- [x] Default `filters` to `{}` and `limit` to 10 when omitted
- [x] Validate `resource` is non-empty (422 on empty string)
- [x] Reject `limit` < 1 or > 100 (422)
- [x] Look up connector instance by ID → return 404 if not found
- [x] List available model backends → return 400 if none configured
- [x] Set RLS organisation scope from authenticated principal
- [x] Require authentication → return 401/403 for unauthenticated requests
- [x] Sample records from the connector via `ConnectorHub.sample()`
- [x] Handle connector sampling failure → return 502 with "Failed to sample connector"
- [x] Instantiate `SchemaInferenceService` with first available model backend
- [x] Handle inference failure → return 502 with "Schema inference failed"
- [x] Return 200 with `definition_json`, `sample_count`, `suggestion_name`, `suggestion_description`
- [x] Include connector name in `suggestion_name` ("Inferred from {name}")
- [x] Include connector name, resource, and sample count in `suggestion_description`

### AI-Assisted Schema Generation — `POST /api/v1/schemas/generate`

- [x] Accept `description` (required) and `examples` (optional) 
- [x] List available model backends → return 400 if none configured
- [x] Instantiate `SchemaGenerationService` with first available backend
- [x] Handle generation failure → return 502 with "Schema generation failed"
- [x] Return 200 with `definition_json`

### Schema Validation — structural checks

- [x] Validate `oneOf`/`anyOf` is a non-empty array
- [x] Reject `oneOf`/`anyOf` alongside `type` at the same level — use wrapping object
- [x] Reject variant entries that aren't JSON Schema objects
- [x] Reject variants missing `type` or composition keywords
- [x] Recurse into nested properties for sub-schema validation
- [x] Validate array schemas have `items`, `contains`, or `prefixItems` (warn if missing)
- [x] Validate array `items` object specifies type, composition, or `$ref`
- [x] Validate tuple-style `items` (list) entries are valid schemas
- [x] Validate `contains` and `prefixItems` sub-schemas recursively
- [x] Handle `anyOf`/`oneOf` at root level (no `type`)

### Schema Migration — version diffs

- [x] Detect added fields between old and new schema
- [x] Detect removed fields between old and new schema
- [x] Detect type changes (string → integer, string → union, string → array, etc.)
- [x] Detect renames (same type, one removed + one added → linked)
- [x] Avoid false rename when types differ
- [x] Handle empty/missing `properties` gracefully
- [x] Detect `union`, `array`, `enum`, `ref`, `object`, `mixed` types via `_extract_type`
- [x] Apply migration: add missing fields (set to null), remove deleted, apply renames
- [x] Migration is idempotent
- [x] Migration does not mutate original data
- [x] `transform_field` applies a callable to a single field

## Error Handling

### Schema CRUD (list, create, get, update, deprecate, delete)

- [x] `ProgrammingError` (missing DB table) → 501 Not Implemented with descriptive message
- [x] `SQLAlchemyError` (connection/deadlock failure) → 503 Service Unavailable
- [x] `IntegrityError` on create schema/version (duplicate name/version) → 409 Conflict
- [x] Schema not found by ID → 404 Not Found
- [x] Delete with active references → `SchemaDeletionProtectedError` → 409 Conflict
- [x] 422 on invalid input (Pydantic validation)

### Schema Inference Endpoint

- [x] Connector instance not found → 404
- [x] No model backends configured → 400
- [x] Unsupported connector type → 400 with list of supported types
- [x] Connector sampling failure → 502
- [x] Connector sampling timeout → 504
- [x] Connector initialise failure → 502
- [x] Model backend initialise failure → 502
- [x] Model backend unavailable (get fails) → 502
- [x] Schema inference failure → 502 with `SchemaInferenceError` detail
- [x] Audit log failure → logged, not propagated (non-critical path)
- [x] Non-serializable sample data (circular refs etc.) → `SchemaInferenceError` → 502
- [x] `ProgrammingError` (missing DB table) → 501
- [x] `SQLAlchemyError` (connection/deadlock) → 503

### Schema Generation Endpoint

- [x] No model backends configured → 400
- [x] Model backend initialise failure → 502
- [x] Model backend unavailable → 502
- [x] Schema generation failure → 502
- [x] Audit log failure → logged, not propagated
- [x] Non-serializable example data → `SchemaGenerationError` → 502
- [x] `ProgrammingError` (missing DB table) → 501
- [x] `SQLAlchemyError` (connection/deadlock) → 503

### Schema Migration Endpoint

- [x] Source schema not found → 404
- [x] Target schema not found → 404
- [x] Source schema has no versions → 404
- [x] Target schema has no versions → 404
- [x] `ProgrammingError` → 501
- [x] `SQLAlchemyError` → 503

### Schema Validation

- [x] Invalid JSON Schema (Draft 2020-12) → 422 with ValidationError details
- [x] Non-object JSON → 400

### Schema Import

- [x] Invalid JSON → 400
- [x] Non-object JSON → 400
- [x] Invalid JSON Schema → 422

## Edge Cases

- [x] Empty samples list (0 records sent to LLM)
- [x] All-null records (fields omitted from schema by LLM)
- [x] Mixed field presence across records (not required)
- [x] Nested object structures in samples
- [x] Fields with non-dict values at boundaries
- [x] Very large samples (truncated to max_sample_records before prompt)
- [x] LLM returns markdown-fenced response with language hint
- [x] LLM returns markdown-fenced response without language hint
- [x] Whitespace around JSON and around fences
- [x] LLM returns bare `{}` (default type/properties injected)
- [x] LLM returns non-dict JSON (array, primitive)
- [x] LLM returns invalid/unparseable JSON
- [x] LLM returns non-string content
- [x] LLM call timed out
- [x] Non-serializable sample data (circular refs) caught as SchemaInferenceError
- [x] Non-dict items in samples (ValueError in infer())
- [x] Empty description in generate (ValueError)
- [x] No examples provided in generate (works with description only)
- [x] Concurrent create with same schema name → IntegrityError → 409
- [x] Concurrent create with same schema version → IntegrityError → 409
- [x] Delete schema that has versions → SchemaDeletionProtectedError → 409
- [x] Source/target schema same in migration → no-op
- [x] Empty/missing properties in migration schemas (empty plan)
- [x] Array-type detection in migration
- [x] Union-type detection in migration
- [x] Rename detection with matching types only
- [ ] **Dry-run migration**: plan is correct but no integration test verifies dry_run=true path
- [ ] **PATCH cannot clear nullable fields**: schema update cannot set a field to None/null

## Resilience & Integration Robustness

- [x] LLM call timeout: configurable via constructor arg, default 60s, caught as `SchemaInferenceError`
- [x] Connector sampling timeout: separate 30s `asyncio.timeout` → 504 Gateway Timeout
- [x] DB connection failure: `ProgrammingError` → 501, `SQLAlchemyError` → 503 on all DB routes
- [x] LLM invocation exception caught → `SchemaInferenceError("LLM call failed")`
- [x] Audit event failure silently logged (non-critical path, does not fail the request)
- [ ] **No retry/backoff on LLM call**: LLM invoke has timeout but no retry on transient errors
- [ ] **No retry/backoff on connector sample**: connector sampling has timeout but no retry
- [ ] **No retry/backoff on ConnectorHub/MBHub initialise**: init failures are terminal
- [ ] **No schema validation on stored definitions**: definition_json is stored as-is without structural validation on create/update
- [ ] **No circuit breaker on ModelBackendHub**: repeated failures hit the same backend

## Known Gaps — PRD 8.16 requirements not yet implemented

- [ ] **Sampled record default (200)**: PRD says default 200 records, code caps at 50 in prompt builder, API default limit is 10, max is 100 — no sample count displayed in UI
- [ ] **Rare-field exclusion**: PRD says fields appearing in <10% of samples should be flagged and excluded from draft — not implemented anywhere
- [ ] **`abstract_name` inference**: PRD says inferred `abstract_name` suggestion per resource type — not implemented; only static string "Inferred from {name}"
- [ ] **SandboxedEnvironment for LLM prompt**: PRD requires `SandboxedEnvironment` with structural separators for prompt safety — not used
- [ ] **Sampled data not stored**: PRD says sampled data must not be persisted after inference — not verified/audited post-inference
- [ ] **Resource-type scope**: PRD says inference works on issue-tracker, git-host, and document-store connectors — no connector-type validation in endpoint (only connector_type_id membership test)
- [ ] **LLM prompt injection hardening**: PRD requires structural separators and no prompt interpolation of raw field values — current prompt interpolates samples directly via f-string
- [ ] **No E2E integration test**: No end-to-end test for the full sample→infer→review flow; BDD scenarios have step definitions but run against mocked backends
- [ ] **No connector-type validation**: Endpoint does not validate that the connector instance belongs to a supported type (issue-tracker, git-host, document-store) — only checks connector_type_id membership
- [ ] **No frontend unit tests**: Zero spec files for SchemaInferenceView or OnboardingWizard schema steps

## QA History
- 2026-07-01: Cross-cutting QA — fixed `_build_infer_prompt` to respect configurable `max_sample_records`, added `ProgrammingError` catch to infer endpoint (501 on missing DB table), added BDD step definitions for all 5 scenarios, added unit test for configurable max_sample_records. 23/23 tests pass.
- 2026-07-04: Cross-cutting QA (index 167) — fixed `SchemaInferenceService.infer()` and `SchemaGenerationService.generate()` to catch `ValueError` from non-serializable samples → `SchemaInferenceError`/`SchemaGenerationError` (previously raw 500). Added `IntegrityError` → 409 to create_schema and create_schema_version endpoints (previously misleading 503). Added structured Error Handling, Edge Cases, and Resilience & Integration Robustness sections to product map. Created error-path unit tests.
