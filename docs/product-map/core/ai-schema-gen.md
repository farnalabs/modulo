---
id: feat-core-ai-schema-gen
prd: 8.16
delivery-tasks: [task-nv9-ai-schema-gen]
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
code:
  - backend/src/modulo/core/schema_registry/inference.py
  - backend/src/modulo/core/schema_registry/generation.py
  - backend/src/modulo/api/routes/schemas.py
depends-on: [feat-core-schema-inference]
status: partial
---

# AI Schema Inference & Generation

Schema Inference (8.16) samples records from a connected tool and uses an LLM to produce a draft JSON Schema. Schema Generation takes a natural-language description plus optional examples and produces a draft schema. Both are read-only, LLM-assisted drafting tools — output always goes through human review before publishing.

### Behaviours

#### Schema Inference — service (`inference.py`, `SchemaInferenceService.infer`)
- [x] LLM infers draft JSON Schema (draft-07/2020-12) from sample data records
- [x] Samples capped at configurable max (default 50, `_MAX_SAMPLE_RECORDS`)
- [x] Handles LLM responses wrapped in markdown code fences (with or without lang hint)
- [x] Handles plain JSON responses (no fences)
- [x] Adds default `type: "object"` and `properties: {}` if missing from LLM output
- [x] Raises `SchemaInferenceError` on LLM call failure (backend exception)
- [x] Raises `SchemaInferenceError` on LLM timeout (configurable, default 60s)
- [x] Raises `SchemaInferenceError` on unparseable LLM response (invalid JSON, non-dict)
- [x] Raises `SchemaInferenceError` on non-string `AIMessage.content`
- [x] Raises `ValueError` on non-dict samples input
- [x] Handles empty sample list (returns backend response)
- [x] Supports custom system prompt injection via constructor
- [x] Nested object/array structures in samples inferred correctly
- [x] Mixed field presence across records handled (required vs optional based on appearance)
- [x] All-null-value fields omitted from draft schema
- [x] Prompt truncates sample set to `_MAX_SAMPLE_RECORDS` (50) #### Schema Generation — service (`generation.py`, `SchemaGenerationService.generate`)
- [x] LLM generates JSON Schema from natural-language description
- [x] Optional example records shape the generated schema
- [x] Handles markdown-fenced and plain-JSON LLM responses
- [x] Raises `SchemaGenerationError` on LLM call failure
- [x] Raises `SchemaGenerationError` on timeout (configurable, default 60s)
- [x] Raises `SchemaGenerationError` on unparseable response
- [x] Raises `SchemaGenerationError` on non-string `AIMessage.content`
- [x] Raises `SchemaGenerationError` on backend returning object without `.content`
- [x] Raises `ValueError` on empty/whitespace-only description
- [x] Omits examples section when none provided (or empty list)
- [x] Supports custom system prompt via constructor
- [x] Rejects non-fenced surrounding explanatory text from LLM (no greedy extraction) #### API endpoints (`routes/schemas.py`)
- [x] `POST /api/v1/schemas/infer` — samples connector data, LLM infers, returns draft
- [x] `POST /api/v1/schemas/generate` — description + examples, LLM generates, returns draft
- [x] Both endpoints require authentication + RLS org scoping
- [x] `POST /infer` returns 404 when connector instance not found
- [x] Both return 400 when no model backends configured
- [x] `POST /infer` returns 502 when connector sampling fails
- [x] Both return 502 when LLM step fails
- [x] `POST /infer` returns `definition_json`, `sample_count`, `suggestion_name`, `suggestion_description` #### Unit test coverage (`test_schema_inference.py`, `test_schema_generation.py`)
- [x] Prompt-building: system + human message structure, truncation, empty samples
- [x] Response parsing: plain JSON, markdown fences, fences without lang hint, whitespace, missing fields
- [x] Error parsing: invalid JSON, non-dict JSON, empty string
- [x] Inference service: happy path, markdown-wrapped, LLM failure, unparseable, empty samples, nested structures, mixed field presence, all-null values, non-string content
- [x] Generation service: happy path, with examples, markdown-wrapped, LLM failure, unparseable, empty/blank description, non-string content, empty examples list, non-fenced surrounding text, complex nested schema, backend without `.content`, timeout #### Integration test coverage (`test_schema_inference_integration.py`)
- [x] Full end-to-end: realistic records, stub LLM, parsed schema
- [x] Schema includes all fields from samples
- [x] Empty records list
- [x] Markdown-wrapped LLM response
- [x] Backend failure wrapped in `SchemaInferenceError`
- [x] Unparseable response wrapped in `SchemaInferenceError`
- [x] Deeply nested object/array structures
- [x] Non-string `AIMessage` content #### PRD scope gaps (8.16) — not yet implemented
- [ ] Configurable sample count (default 200 per PRD, not 50 as today)
- [ ] Enum detection for `issue_type`, `status`, `priority` fields
- [ ] Fields appearing in <10% of records flagged as rarely-used, excluded from default draft
- [ ] Inferred `abstract_name` suggestion surfaced in response
- [ ] Draft opens in schema editor for operator review before publishing
- [ ] Sandboxed LLM prompt (`SandboxedEnvironment`) for untrusted record data
- [ ] Sampled data not stored after inference completes (data lifecycle statement)
- [ ] SDLC onboarding path: connect, infer, review, publish, browse library, wire agents #### BDD coverage
- [x] Gherkin scenarios in `schema_inference.feature` — 6 scenarios covering infer, 404, 400, validation, migration plan, and unsupported connector types #### Error Handling — DB ProgrammingError → 501
- [x] `list_schemas_endpoint` (line 142) — caught, returns 501
- [x] `create_schema_endpoint` (line 173) — caught, returns 501
- [x] `get_schema_endpoint` (line 192) — caught, returns 501
- [x] `update_schema_endpoint` (line 215) — caught, returns 501
- [x] `deprecate_schema_endpoint` (line 237) — caught, returns 501
- [x] `delete_schema_endpoint` (line 265) — caught, returns 501
- [x] `list_schema_versions_endpoint` (line 295) — caught, returns 501
- [x] `create_version_endpoint` (line 336) — caught, returns 501
- [x] `get_schema_version_endpoint` (line 356) — caught, returns 501
- [x] `list_schema_fields_endpoint` (line 385 → now fixed) — caught, returns 501
- [ ] `POST /api/v1/schemas/validate` — no DB access, no catch needed
- [ ] `POST /api/v1/schemas/import` — no DB access yet, no catch needed
- [ ] `POST /api/v1/schemas/migrate/plan` — no DB access, no catch needed
- [x] `POST /api/v1/schemas/infer` (line 494) — caught, returns 501
- [x] `POST /api/v1/schemas/generate` (line 597) — caught, returns 501

## Known Gaps
- **Sample cap mismatch:** Code hardcodes 50 max samples; PRD specifies default 200.
  - **Compounding issue:** `SchemaSampleQuery.limit` max is 100 while inference caps at 50. Users can request 100 samples but only 50 reach the LLM — wasted sampling work.
  - **Misleading API response:** `SchemaInferResponse.sample_count` reports total sampled records, not the count actually sent to the LLM.
- **No enum/rare-field logic:** Inference prompt doesn't instruct for enum detection or rare-field flagging (8.16)
- **No `abstract_name` inference:** `abstract_name` field exists on `SchemaCreate`/`SchemaUpdate`/`SchemaResponse` models (CRUD layer supports it), but `/infer` endpoint (`SchemaInferResponse`) does NOT include `abstract_name`. `suggestion_name` is hardcoded `"Inferred from {ci.name}"`, not AI-inferred.
- **No SandboxedEnvironment:** LLM prompt doesn't isolate untrusted record values per 8.16 security requirement
- **No data lifecycle enforcement:** No mechanism to ensure sampled data is not persisted after inference
- **`ch.initialise()` not wrapped in try/except** (line 512): `ConnectorHub.initialise()` can fail on Fernet key mismatch or missing connector credentials. Unlike `determination.py` (which catches `ConnectorDecryptError`), the `/infer` route would propagate an unhandled 500.
- **`mh.initialise()` not wrapped** (lines 533, 621): ModelBackendHub decryption failure → unhandled 500.
- **`mh.get()` not wrapped** (lines 535, 623): `StopIteration` possible if `backend_ids` empty. 