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
  - backend/tests/unit/core/test_schema_sanitize.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_validation.py
  - backend/tests/unit/api/test_error_handling.py
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
- [x] Sanitise sample records before serialisation — sensitive values masked, control chars stripped, strings/lists/depth bounded
- [x] Wrap sample data in structural separators (`<<<SAMPLE_DATA>>>` / `<<<END_SAMPLE_DATA>>>`) — never interpolate raw field values into prompt instructions
- [x] System prompt instructs the model that the sample-data block is untrusted input
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
- [ ] Connector sampling timeout → 504 (endpoint raises 504, but the shared `problem_from_http_exception` lookup has no 504 mapping so it surfaces as 500 — see Known Gaps)
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
- [x] `create_migration()` failure → 500 with structured detail
- [x] `apply_migration()` failure → 500 with structured detail

### Schema Validation

- [x] Invalid JSON Schema (Draft 2020-12) → 422 with ValidationError details
- [x] Non-object JSON → 400
- [x] Response path includes all segments (prev `popleft()` path mutation bug fixed)

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
- [x] **Dry-run migration**: `?dry_run=true` verified at endpoint level (`test_schemas_endpoint.py::test_migrate_data_dry_run_records_audit_event` asserts `plan["dry_run"] is True` and unmodified `migrated_data`); a full testcontainers integration test for the dry-run path is not present
- [x] **PATCH can clear nullable fields**: `update_schema_endpoint` uses `model_dump(exclude_unset=True)`, so an explicit `null` is applied as a set value (NOT a no-op); regression test added 2026-08-15 (`test_update_schema_patch_can_clear_nullable_field` in `test_error_handling.py`)

## Resilience & Integration Robustness

- [x] LLM call timeout: configurable via constructor arg, default 60s, caught as `SchemaInferenceError`
- [x] Connector sampling timeout: separate 30s `asyncio.timeout` → raises 504 (mapped to `urn:problem:modulo:gateway_timeout` via `problem_from_http_exception` — see QA History)
- [x] DB connection failure: `ProgrammingError` → 501, `SQLAlchemyError` → 503 on all DB routes
- [x] LLM invocation exception caught → `SchemaInferenceError("LLM call failed")`
- [x] Audit event failure silently logged (non-critical path, does not fail the request)
- [x] **Retry/backoff on LLM call**: `_common.invoke_and_parse` retries up to 3 attempts with exponential backoff (`asyncio.sleep(2**attempt)` on transient errors; the timeout path retries without sleeping); proven by `test_infer_retries_transient_failures_then_succeeds`
- [ ] **No retry/backoff on connector sample**: connector sampling has timeout but no retry
- [ ] **No retry/backoff on ConnectorHub/MBHub initialise**: init failures are terminal
- [ ] **No schema validation on stored definitions**: definition_json is stored as-is without structural validation on create/update
- [ ] **No circuit breaker on ModelBackendHub**: repeated failures hit the same backend

## Known Gaps — PRD 8.16 requirements not yet implemented

- [ ] **Sampled record default (200)**: PRD says default 200 records, code caps at 50 in prompt builder, API default limit is 10, max is 100 — no sample count displayed in UI
- [ ] **Rare-field exclusion**: PRD says fields appearing in <10% of samples should be flagged and excluded from draft — not implemented anywhere
- [ ] **`abstract_name` inference**: PRD says inferred `abstract_name` suggestion per resource type — not implemented; only static string "Inferred from {name}"
- [x] ~~**SandboxedEnvironment for LLM prompt**: PRD requires `SandboxedEnvironment` with structural separators for prompt safety — not used~~ **RESOLVED 2026-08-12**: sample/example data is now scrubbed by `schema_registry/sanitize.py` (credential masking, control-char stripping, length/cardinality/depth bounds) and wrapped in `<<<SAMPLE_DATA>>>` structural separators; the system prompt explicitly declares the block untrusted input
- [x] ~~**Sampled data not stored**~~ **RESOLVED 2026-08-15**: regression test added (`test_infer_schema_response_does_not_contain_or_persist_sample_records`) asserting the infer response carries no raw sample records and only the documented response keys; samples live in memory for the request and are never written to the DB
- [x] ~~**Resource-type scope / connector-type validation**~~ **RESOLVED 2026-08-15**: the endpoint validates `connector_type_id` against the `supported_inference_types` whitelist (github, gitlab, jira, linear, slack, notion, confluence — covering git-host, issue-tracker, document-store, and chat) and returns 400 with the supported-type list for anything else (BDD scenario "Schema inference rejects unsupported connector types")
- [x] ~~**LLM prompt injection hardening**: PRD requires structural separators and no prompt interpolation of raw field values — current prompt interpolates samples directly via f-string~~ **RESOLVED 2026-08-12**: sample data is sanitised before serialisation and rendered between explicit structural separators; the model is told to treat the block as opaque data
- [ ] **No DB-backed endpoint E2E**: `test_schema_inference_integration.py` covers the sample→infer service path end-to-end (realistic records → stub LLM → parsed schema) against testcontainers, and BDD covers the infer + publish flow against mocked backends; a single full-stack test from HTTP endpoint through real DB to schema version is still missing
- [x] ~~**No connector-type validation**~~ **RESOLVED 2026-08-15**: duplicate of the resource-type-scope gap above — the endpoint does validate connector type against `supported_inference_types` and rejects unsupported types with 400 (see above)
- [ ] **OnboardingWizard schema steps lack spec coverage**: `SchemaInferenceView.spec.ts` exists and gained 9 interaction/API-mock tests on 2026-08-15 (connector load, infer call body, draft render, publish envelope+version, navigation, errors); the OnboardingWizard schema steps (2–3) still have no spec

- [x] ~~**504 status mapped to 500**~~ **RESOLVED 2026-08-16**: added `ProblemType.GATEWAY_TIMEOUT` (status 504, title `Gateway Timeout`) plus the `504` lookup entry in `modulo/api/models/problem.py::problem_from_http_exception`, so the sampling-timeout path now surfaces as a structured `urn:problem:modulo:gateway_timeout` problem instead of a 500 (fixes the observability PUT timeout path too).

## QA History

- 2026-08-16: improve-architecture (index 192): **RESOLVED the "504 status mapped to 500" cross-cutting known gap** (`api/models/problem.py`). `problem_from_http_exception` had no 504 mapping, so every plain `HTTPException(504)` surfaced as `internal_error`/500 — the `/schemas/infer` connector sampling timeout and the `/settings/observability` PUT DB timeout both lied about their status. Added `ProblemType.GATEWAY_TIMEOUT` (`urn:problem:modulo:gateway_timeout`, status 504, title `Gateway Timeout`) + `_PROBLEM_METADATA` entry + `504` lookup row. Tests: `test_problem.py` metadata matrix + status-mapping table updated; new `test_infer_schema_sampling_timeout_returns_504_problem` endpoint test (TimeoutError during `ConnectorHub.sample` → 504 problem body, not 500); `test_observability_routes.py::test_put_reraises_on_db_timeout` updated from the stale `500` assertion to the corrected 504 problem shape. Verification: 38/38 `test_problem.py` + 14/14 `test_schema_infer_endpoint.py` + 33/33 `test_observability_routes.py` + 206 focused api/models/schemas/exception-handler tests pass, ruff check + format clean.

- 2026-08-15: improve-architecture coverage drive (FAR-234). Verified and checked off: dry-run migration (endpoint-level coverage in `test_schemas_endpoint.py`), PATCH-clears-nullable-fields (endpoint uses `exclude_unset=True`; new regression test in `test_error_handling.py`), LLM retry/backoff (`_common.invoke_and_parse` 3 attempts + exponential backoff, proven by `test_infer_retries_transient_failures_then_succeeds`). Resolved stale Known Gaps: sampled-data-not-stored (new no-sample-persistence endpoint test), resource-type scope + connector-type validation (endpoint validates `supported_inference_types` whitelist → 400, BDD-covered). Corrected the "Connector sampling timeout → 504" error-handling checkbox: the endpoint raises 504 but `problem_from_http_exception` has no 504 mapping, so it surfaces as 500 — logged as a new cross-cutting Known Gap in `problem.py`. Softened the E2E-integration and frontend-unit gaps to reflect actual coverage. Added ProgrammingError→501 + SQLAlchemyError→503 tests for the infer and generate endpoints. Status: partial.

- 2026-08-12: improve-architecture — RESOLVED the "SandboxedEnvironment / LLM prompt injection hardening" known gaps (PRD §8.16: sampled records treated as untrusted input, structural separators, no prompt interpolation of raw field values). New `schema_registry/sanitize.py`: `is_sensitive_key` (segment/suffix matching incl. plural/collection forms — flags `access_token`, `api_key`, `client_secret`, `tokens`, `api_keys`, `passwords`, `secrets` but not `monkey`/`author`/`key_name`), `sanitise_sample_records` (deep defensive copy: sensitive-keyed values masked — strings, non-string scalars, and list/dict contents under a sensitive key regardless of nested key name; control chars stripped, strings capped at 2000 chars, arrays at 100, nesting at depth 8, non-list passthrough). `_build_infer_prompt` + `_build_generate_prompt` now sanitise before serialising and render sample data between `<<<SAMPLE_DATA>>>` / `<<<END_SAMPLE_DATA>>>` structural separators (replacing bare markdown code fences; injected delimiter markers in values are escaped so the block cannot be terminated early); both system prompts declare the block untrusted input and instruct the model never to follow instructions inside it. Added 48 unit-test cases in `test_schema_sanitize.py` (sensitive-key matrix 31, recursive masking, no input mutation, control-char stripping, length/cardinality/depth caps, structural-separator rendering + delimiter escaping, no-secret-leak assertions for both inference and generation prompts, serialisability). 91 targeted schema unit tests pass, ruff check + format clean, mypy --strict clean, import-linter 7/7 contracts kept. Status: partial.
- 2026-07-01: Cross-cutting QA — fixed `_build_infer_prompt` to respect configurable `max_sample_records`, added `ProgrammingError` catch to infer endpoint (501 on missing DB table), added BDD step definitions for all 5 scenarios, added unit test for configurable max_sample_records. 23/23 tests pass.
- 2026-07-04: Cross-cutting QA (index 167) — fixed `SchemaInferenceService.infer()` and `SchemaGenerationService.generate()` to catch `ValueError` from non-serializable samples → `SchemaInferenceError`/`SchemaGenerationError` (previously raw 500). Added `IntegrityError` → 409 to create_schema and create_schema_version endpoints (previously misleading 503). Added structured Error Handling, Edge Cases, and Resilience & Integration Robustness sections to product map. Created error-path unit tests.
- 2026-07-08: Cross-cutting QA (index 250) — Fixed CRITICAL bug in `validate_schema_endpoint`: `exc.path.popleft()` mutated the deque, causing the response `path` field to omit the first segment. Copied path to `list()` before reading, preserving all segments. Fixed MAJOR bug in `migrate_data_endpoint`: `create_migration()`/`apply_migration()` calls ran outside any try/except — Python errors (ValueError, TypeError) from schema processing propagated as raw 500. Wrapped in `except Exception` → 500 with structured detail. Added 18 new tests covering validate endpoint (5 tests: valid, invalid type, path regression, empty, union), generate endpoint (3 tests: no backends→400, ProgrammingError→501, SQLAlchemyError→503), infer endpoint (2 tests: ProgrammingError→501, SQLAlchemyError→503), import endpoint (4 tests: invalid JSON→400, non-object→400, valid→200, invalid schema→422), fields endpoint (2 tests: ProgrammingError→501, SQLAlchemyError→503), and migrate endpoint (2 tests: create_plan→500, apply→500). All 39 schema error-handling tests pass.
