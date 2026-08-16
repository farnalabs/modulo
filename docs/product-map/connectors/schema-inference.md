---
id: feat-connectors-schema-inference
prd: 8.16
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
  - backend/tests/bdd/features/schemas/schema_inference.feature
unit-tests:
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/core/test_schema_sanitize.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_validation.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/api/test_schema_generate_endpoint.py
code:
  - backend/src/modulo/core/connector_hub/__init__.py
  - backend/src/modulo/core/schema_registry/inference.py
  - backend/src/modulo/api/routes/schemas.py
depends-on:
  - feat-core-schema-inference
  - feat-connectors-hub
  - feat-model-backends-hub
status: partial
---

# Schema Inference from Connected Tools

LLM-assisted schema draft generation from connected tool data (issue trackers, git hosts). When onboarding an existing SDLC, operators connect their tools (Jira, Linear, GitHub, GitLab) and Modulo inspects the tool's data to propose a schema for the schemas that will drive the pipeline. This feature couples connector data inspection with the schema inference system described in `feat-core-schema-inference`.

## Behaviours

### Tool Data Inspection — connector-agnostic read interface

- [x] ConnectorHub provides a read interface for schema inference that works across all `ConnectorType` instances (`ConnectorHub.sample()`)
- [x] Schema inference reads sample data from the connected tool: issue records from Jira/Linear, repo metadata from GitHub/GitLab
- [x] Reads respect the connector's ACL and credential scope — no escalation via inference path
- [ ] Reads are limited to a configurable sample size (default: 200 per PRD; actual default is 10, max 100, prompt caps at 50)
- [x] Sample data is sanitised before being sent to the inference LLM — credential-like values masked, control chars stripped, no plaintext tokens or credentials leak into the prompt
- [x] Read timeout is independent of pipeline-run read timeout (30s `asyncio.timeout` around sampling in the infer endpoint, separate from the LLM call timeout; slower than the PRD's 10s default but independent). NOTE: the raised 504 now surfaces as a proper RFC 9457 `gateway_timeout` (was 500 before the `problem_from_http_exception` 504 mapping was added 2026-08-16)

### Schema Generation — LLM-assisted draft from sample data

- [x] Sample data is sent as context to a model backend for field extraction and typing
- [x] The inference LLM returns a proposed JSON Schema draft with field names, types, optional descriptions
- [ ] The draft schema is stored as a new unreleased schema version in the Schema Registry — NOT implemented: the draft is returned in the API response and persisted only when the operator publishes (review-before-publish); there is no in-registry draft state (see Known Gaps)
- [ ] The draft schema is marked `status: draft` — NOT implemented: no draft lifecycle status in the registry; drafts exist only in the client between infer and publish (see Known Gaps)
- [ ] The operator can edit the draft schema before publishing — PARTIAL: schema name/description are editable in the onboarding wizard step 3, but there is no field-level schema editor (rename fields, adjust types, toggle required); the standalone view shows name/description read-only (see Known Gaps)
- [x] The operator can reject the draft schema entirely (deletes the draft version)
- [x] The inference run is recorded in the audit log with the tool source and model used

### Connector Type Awareness — type-specific field extraction

- [ ] Issue-tracker connectors (Jira, Linear) propose fields from issue metadata: summary, description, priority, status, assignee, labels
- [ ] Git-host connectors (GitHub, GitLab) propose fields from repository/PR metadata: repo name, branch, PR title, PR description, file paths
- [ ] CI-runner connectors propose fields from pipeline/job metadata: pipeline ID, status, branch, commit SHA, duration
- [ ] Chat connectors (Slack) propose fields derived from message/channel metadata
- [ ] Generic connectors with no specialised schema return a minimal field set (id, name, type)

### Edge Cases and Error States

- [x] Connected tool has no data (empty project, new workspace) — inference returns a minimal placeholder draft
- [x] Connected tool returns insufficient permissions — inference is blocked with a named error (ACL enforcement in `ConnectorHub.sample()`)
- [x] Connected tool times out — inference fails gracefully with a named timeout error ("Connector sampling timed out after 30s", no hang). NOTE: the status now surfaces as 504 `gateway_timeout` (previously 500 — 504 mapping added to `problem_from_http_exception` 2026-08-16)
- [x] Inference LLM call fails — original error surfaced via 502, no partial draft created
- [ ] Inferred schema contains unsupported field types (e.g. `anyOf`, `$ref`) — schema is edited before publishing
- [ ] Draft schema naming — PARTIAL: auto-generated `"Inferred from {connector name}"` (not tool-type + project name) and editable only in wizard step 3 (standalone view read-only) — see Known Gaps
- [ ] Concurrency: only one active inference per (org, connector_instance) — subsequent requests queued
- [x] Empty sample data returns a minimal `{"type": "object", "properties": {}}` schema (valid fallback)

### Error Handling

- [x] Connector instance not found → 404 (tested)
- [x] Unsupported connector type → 400 (tested in BDD)
- [x] No model backends configured → 400 (tested)
- [x] Empty resource name → 422 (tested)
- [x] Unauthenticated request → 401/403 (tested)
- [x] Sampling failure → 502 with original error message (tested)
- [x] Inference LLM failure → 502 with descriptive error (tested)
- [x] ProgrammingError (missing DB table) → 501 Not Implemented (coded, tested)
- [x] Schema generation: no model backends → 400 (tested)
- [x] Schema generation: ProgrammingError → 501 Not Implemented (coded, tested)
- [x] Schema generation: GenerationError → 502 (tested)
- [x] Schema inference: unguarded `create_secrets_backend` → 500 with descriptive detail (coded, tested)
- [x] Schema generation: unguarded `create_secrets_backend` → 500 with descriptive detail (coded, tested)

### Security and Data Isolation

- [x] Sample data never leaves the org's ModelBackend (inference uses the org's configured model backend)
- [x] Sample data is discarded after inference completes — not stored in the database (samples live in memory for the request only; the response carries only the inferred definition + metadata; regression test added 2026-08-15)
- [x] Inferred schema is scoped to the org — not shared across orgs (RLS enforced)
- [x] Connector ACL is enforced: operator must have read access to the connector instance
- [ ] Schema inference is behind a team feature flag (v1 feature)
- [x] Schema inference runs emit audit events (payload: connector_name, connector_type, resource, sample_count, model_backend_id — `connector_type` added to the payload 2026-08-15; asserted by a unit test)

## Known Gaps

- [ ] **Connector read interface for inference not defined**: ConnectorHub has `sample()` method but no `infer_schema()` or connector-type-aware sampling
- [x] ~~**Data sanitisation rules not defined**: what fields are scrubbed from sample data before LLM inference is unspecified~~ **RESOLVED 2026-08-12**: `schema_registry/sanitize.py` defines the scrubbing rules — sensitive-keyed values are masked (segment/suffix matching incl. plural forms for token/secret/password/api_key/access_key/private_key/authorization/credential, masking strings, non-string scalars, and list/dict contents under a sensitive key), control characters stripped, strings capped at 2000 chars, arrays at 100, nesting at depth 8, deep defensive copy (caller data never mutated)
- [x] ~~**No CLI or UI for triggering inference**~~ **RESOLVED 2026-08-15**: the onboarding wizard (OnboardingWizard.vue) implements the Run Inference → Review Schemas steps and the standalone `/schemas/infer` view exists; no CLI command for triggering per-connector inference is provided (endpoint + UI are the trigger paths)
- [ ] **Sampled record default (200)**: PRD says default 200 records, code caps at 50 in prompt builder, API default limit is 10, max is 100
- [ ] **Rare-field exclusion**: PRD says fields appearing in <10% of samples should be flagged and excluded from draft — not implemented
- [ ] **No abstract_name inference**: PRD says inferred `abstract_name` suggestion per resource type — not implemented; only static string "Inferred from {name}"
- [ ] **SandboxedEnvironment for LLM prompt**: PRD requires `SandboxedEnvironment` with structural separators for prompt safety — structural separators + untrusted-data instruction implemented (2026-08-12); Jinja SandboxedEnvironment itself not used (no user-authored template in the inference path)
- [x] ~~**CRITICAL: Sample data not sanitised before LLM prompt**: raw fields are interpolated with only markdown code-fence separation; PRD requires SandboxedEnvironment with structural separators~~ **RESOLVED 2026-08-12**: sample data is sanitised by `schema_registry/sanitize.py` and rendered between `<<<SAMPLE_DATA>>>` / `<<<END_SAMPLE_DATA>>>` structural separators; the system prompt declares the block untrusted input and forbids following embedded instructions
- [ ] **No concurrency guard**: multiple concurrent inference requests per connector are not serialised
- [ ] **No team feature flag on inference endpoint**
- [ ] **Connector-type-aware field extraction not implemented**: all connector types use the same generic prompt
- [ ] **No dedicated BDD feature file for connector-type-aware inference (only generic schema_inference.feature exists)**

- [x] ~~**504 status mapped to 500**: the sampling-timeout path raises `HTTPException(504)` but the shared `modulo/api/models/problem.py::problem_from_http_exception` lookup has no 504 mapping (no `GATEWAY_TIMEOUT` ProblemType), so the response surfaces as 500~~ **RESOLVED 2026-08-16**: `problem.py` gained a `GATEWAY_TIMEOUT` ProblemType (`urn:problem:modulo:gateway_timeout`, title "Gateway Timeout") + a 504 lookup entry, so `HTTPException(504)` now surfaces as 504 (cross-cutting — also fixes `POST /api/v1/errors` and any future 504 raise site). Locked by `test_problem.py::test_504_maps_to_gateway_timeout_not_internal_error` + endpoint regression `test_schema_infer_endpoint.py::test_infer_schema_sampling_timeout_returns_504_problem`.
- [ ] **No registry-level draft state**: the inferred draft is returned in the API response and persisted only on publish; there is no "unreleased draft schema version" or `status: draft` in the registry (PRD's review-then-publish flow is satisfied via the client-side draft, not registry draft versions).
- [ ] **No field-level schema editor on drafts**: operator can rename name/description (wizard step 3) and discard, but cannot rename fields, adjust types, or toggle required before publishing — blocked on the schema-editor gap in `feat-core-schema-inference-ui`.

## QA History

- 2026-08-16: improve-architecture — RESOLVED the cross-cutting "504 status mapped to 500" Known Gap. `modulo/api/models/problem.py` gained a `GATEWAY_TIMEOUT` ProblemType (`urn:problem:modulo:gateway_timeout`, title "Gateway Timeout", status 504) + a 504 entry in the `problem_from_http_exception` lookup, so a plain `HTTPException(504)` — e.g. the schema-inference 30s sampling timeout — now surfaces as a proper 504 `gateway_timeout` problem detail instead of collapsing to 500 `internal_error`. Cross-cutting: also fixes `POST /api/v1/errors` (observability.py Gateway timeout) and any future 504 raise site. Added 3 unit-test cases in `test_problem.py` (metadata row, known-status map row, + `test_504_maps_to_gateway_timeout_not_internal_error`) and an endpoint-level regression `test_infer_schema_sampling_timeout_returns_504_problem` in `test_schema_infer_endpoint.py` (asserts 504 + problem type + title + detail; both fail when the lookup entry is removed). 39/39 `test_problem.py` + 14/14 `test_schema_infer_endpoint.py` + 241/241 focused problem/error-handling/request-timeout tests pass, ruff check + format clean, mypy --strict clean on `problem.py`. Status: partial.

- 2026-08-15: improve-architecture coverage drive (FAR-234). Verified behaviours and added missing coverage: (1) added `connector_type` to the inference audit-event payload and a unit test asserting it (`test_infer_schema_emits_audit_event_with_tool_source_and_model`); (2) added a no-sample-persistence regression test (`test_infer_schema_response_does_not_contain_or_persist_sample_records`) — response carries only `definition_json`/`sample_count`/`suggestion_name`/`suggestion_description`, never raw records; (3) added ProgrammingError→501 + SQLAlchemyError→503 tests for both the infer and generate endpoints; (4) marked `[x]` the independent sampling-timeout behaviour (30s `asyncio.timeout`, named error, no hang); (5) resolved the stale "No CLI or UI for triggering inference" gap (onboarding wizard Run Inference / Review Schemas steps exist); (6) un-checked two over-stated behaviours (in-registry draft storage / `status: draft`, and the "same schema editor as manual creation" claim) and documented them in Known Gaps. New Known Gaps: 504→500 mapping in `problem.py`, no registry-level draft state, no field-level draft editor. Status: partial.


- 2026-08-12: improve-architecture — RESOLVED the "CRITICAL: Sample data not sanitised before LLM prompt" and "Data sanitisation rules not defined" known gaps. Sample/example data flowing into the inference and generation prompts is now scrubbed by `schema_registry/sanitize.py` (credential-like values masked via segment/suffix key matching incl. plural/collection forms, container/scalar masking under sensitive keys, control chars stripped, string/array/depth bounds enforced, deep defensive copy) and rendered between `<<<SAMPLE_DATA>>>` / `<<<END_SAMPLE_DATA>>>` structural separators (with delimiter-marker escaping) plus an explicit "untrusted input" instruction in both system prompts. Added 48 unit-test cases in `test_schema_sanitize.py` asserting no secret ever reaches the prompt and separators render correctly. 91 targeted schema unit tests pass, ruff clean, mypy --strict clean, import-linter 7/7.
- 2026-07-03: Cross-cutting QA (index 111). Fixed stale checkboxes (audit event [ ]→[x], connector-type validation confirmed). Added Error Handling section (11 behaviour checkboxes covering all error paths). Added 30s timeout on connector sampling step. Added ProgrammingError→501 unit tests (infer + generate endpoints). Updated Known Gaps: removed 2 stale gaps (connector-type validation, audit event dispatch), added 9 new gaps. Created website docs stub. Status: partial (17 known gaps remain).
- 2026-07-08: Cross-cutting QA (index 269). Fixed CRITICAL — added `try/except Exception→500` guard around `create_secrets_backend()` in both `infer_schema_endpoint` and `generate_schema_endpoint` (previously unguarded — bad Fernet key or unexpected error from `create_secrets_backend` would propagate to CatchAllMiddleware as opaque 500). Corrected 3 stale `[ ]`→`[x]` Error Handling checkboxes for schema generation (no backends→400, ProgrammingError→501, GenerationError→502). Added 2 new Error Handling checkboxes. Added `test_schema_generate_endpoint.py` to unit-tests. Added 2 new tests for Exception→500 on both endpoints. All new tests pass. Pre-existing: `test_generate_schema_generation_failure_returns_502` asserts 502 but CatchAllMiddleware returns 500 — systemic CatchAllMiddleware bug intercepting HTTPException before FastAPI's exception handler. Status: partial.
- 2026-07-11: Round 3 improve-architecture. Removed stale Known Gap (timeout fix verified in code with `async with asyncio.timeout(30.0)`). Removed 3 duplicate gaps (sample limit, rare-field, abstract_name already documented in lines 100-102). 12 gaps remain (was 16).
