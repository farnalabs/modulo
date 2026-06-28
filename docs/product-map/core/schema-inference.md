---
id: feat-core-schema-inference
prd: §8.16
delivery-tasks: [task-nv5-schema-inference-service, task-nv5-schema-infer-endpoint]
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
unit-tests:
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_validation.py
code:
  - backend/src/modulo/core/schema_registry/inference.py
  - backend/src/modulo/core/schema_registry/generation.py
  - backend/src/modulo/core/schema_registry/validation.py
  - backend/src/modulo/core/schema_registry/migration.py
  - backend/src/modulo/api/routes/schemas.py
depends-on: []
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
- [ ] Accept configurable `max_sample_records` (constructor arg) — currently hard-coded at 50 in prompt builder
- [ ] Accept empty samples list gracefully — LLM sees "0 records", service allows it
- [ ] Raise `ValueError` when samples contains non-dict items
- [ ] Accept nested object structures in samples
- [ ] Accept fields with mixed presence across records (not required)
- [ ] Handle all-null records (fields omitted from schema)

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

### Known Gaps — PRD §8.16 requirements not yet implemented

- [ ] **Sampled record default (200)**: PRD says default 200 records, code caps at 50 in prompt builder, API default limit is 10, max is 100
- [ ] **Rare-field exclusion**: PRD says fields appearing in <10% of samples should be flagged and excluded from draft — not implemented anywhere
- [ ] **`abstract_name` inference**: PRD says inferred `abstract_name` suggestion per resource type — not implemented; only static string "Inferred from {name}"
- [ ] **SandboxedEnvironment for LLM prompt**: PRD requires `SandboxedEnvironment` with structural separators for prompt safety — not used
- [ ] **Sampled data not stored**: PRD says sampled data must not be persisted after inference — not verified/audited post-inference
- [ ] **Resource-type scope**: PRD says inference works on issue-tracker, git-host, and document-store connectors — no connector-type validation in endpoint
- [ ] **LLM prompt injection hardening**: PRD requires structural separators and no prompt interpolation of raw field values — current prompt interpolates samples directly via f-string
- [ ] **BDD feature file is placeholder**: `backend/tests/bdd/features/connectors/schema_inference.feature` has no real scenarios
- [ ] **No E2E integration test**: BDD is placeholder; no end-to-end test for the full sample→infer→review flow
- [ ] **`depends-on` was pointing to a delivery task ID** instead of a feature ID — corrected to `[]`
