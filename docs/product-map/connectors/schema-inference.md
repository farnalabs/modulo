---
id: feat-connectors-schema-inference
prd: 8.16
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
  - backend/tests/bdd/features/schemas/schema_inference.feature
unit-tests:
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_validation.py
  - backend/tests/unit/api/test_schema_infer_programming_error.py
code:
  - backend/src/modulo/core/connector_hub/__init__.py
  - backend/src/modulo/core/schema_registry/inference.py
  - backend/src/modulo/api/routes/schemas.py
depends-on:
  - feat-core-schema-inference
  - feat-connectors-hub
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
- [ ] Sample data is sanitised before being sent to the inference LLM — no plaintext credentials, no internal URLs
- [ ] Read timeout is independent of pipeline-run read timeout (shorter: 10s default)

### Schema Generation — LLM-assisted draft from sample data

- [x] Sample data is sent as context to a model backend for field extraction and typing
- [x] The inference LLM returns a proposed JSON Schema draft with field names, types, optional descriptions
- [ ] The draft schema is stored as a new unreleased schema version in the Schema Registry
- [ ] The draft schema is marked `status: draft` — not usable in pipelines until reviewed and published
- [x] The operator can edit the draft schema before publishing (same schema editor as manual creation)
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
- [ ] Connected tool times out — inference fails gracefully with a timeout error
- [x] Inference LLM call fails — original error surfaced via 502, no partial draft created
- [ ] Inferred schema contains unsupported field types (e.g. `anyOf`, `$ref`) — schema is edited before publishing
- [ ] Draft schema naming — auto-generated name from tool type + project name, editable by operator
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
- [x] ProgrammingError (missing DB table) → 501 Not Implemented (coded, untested)
- [ ] Schema generation: no model backends → 400 (tested)
- [ ] Schema generation: ProgrammingError → 501 Not Implemented (coded, untested)
- [ ] Schema generation: GenerationError → 502 (tested)

### Security and Data Isolation

- [x] Sample data never leaves the org's ModelBackend (inference uses the org's configured model backend)
- [ ] Sample data is discarded after inference completes — not stored in the database
- [x] Inferred schema is scoped to the org — not shared across orgs (RLS enforced)
- [x] Connector ACL is enforced: operator must have read access to the connector instance
- [ ] Schema inference is behind an enterprise feature flag (v1 feature)
- [ ] Schema inference runs emit audit events (connector_type, model_backend_id, sample_count)

## Known Gaps

- [x] **No implementation exists**: the core schema inference flow (sample data → LLM → draft) is fully implemented via feat-core-schema-inference; this entry tracks the connector-coupling layer
- [x] **BDD placeholder**: the file at `connectors/schema_inference.feature` has 5 real scenarios, and `schemas/schema_inference.feature` has 6 more — step definitions exist in `test_schema_inference.py`
- [x] **No unit tests**: unit tests exist at `backend/tests/unit/core/test_schema_inference.py`, `backend/tests/unit/api/test_schema_infer_endpoint.py`, `backend/tests/unit/core/test_schema_migration.py`, and `backend/tests/unit/core/test_schema_validation.py`
- [ ] **Connector read interface for inference not defined**: ConnectorHub has `sample()` method but no `infer_schema()` or connector-type-aware sampling
- [ ] **Data sanitisation rules not defined**: what fields are scrubbed from sample data before LLM inference is unspecified
- [ ] **No CLI or UI for triggering inference**: endpoint and SchemaInferenceView.vue exist, but no onboarding wizard step or CLI command for triggering per-connector inference
- [ ] **Sampled record default (200)**: PRD says default 200 records, code caps at 50 in prompt builder, API default limit is 10, max is 100
- [ ] **Rare-field exclusion**: PRD says fields appearing in <10% of samples should be flagged and excluded from draft — not implemented
- [ ] **No abstract_name inference**: PRD says inferred `abstract_name` suggestion per resource type — not implemented; only static string "Inferred from {name}"
- [ ] **SandboxedEnvironment for LLM prompt**: PRD requires `SandboxedEnvironment` with structural separators for prompt safety — not used
- [ ] **CRITICAL: Sample data not sanitised before LLM prompt**: raw fields are interpolated with only markdown code-fence separation; PRD requires SandboxedEnvironment with structural separators
- [ ] **No timeout on connector sampling step (fixed: 30s timeout added)**
- [ ] **Default sample limit is 10 (PRD says 200); code caps at 50 in prompt builder, 100 via API max**
- [ ] **No rare-field exclusion based on <10% frequency (PRD §8.16)**
- [ ] **No abstract_name inference**: always returns 'Inferred from {name}'
- [ ] **No concurrency guard**: multiple concurrent inference requests per connector are not serialised
- [ ] **No enterprise feature flag on inference endpoint**
- [ ] **Connector-type-aware field extraction not implemented**: all connector types use the same generic prompt
- [ ] **No dedicated BDD feature file for connector-type-aware inference (only generic schema_inference.feature exists)**

## QA History

- 2026-07-03: Cross-cutting QA (index 111). Fixed stale checkboxes (audit event [ ]→[x], connector-type validation confirmed). Added Error Handling section (11 behaviour checkboxes covering all error paths). Added 30s timeout on connector sampling step. Added ProgrammingError→501 unit tests (infer + generate endpoints). Updated Known Gaps: removed 2 stale gaps (connector-type validation, audit event dispatch), added 9 new gaps. Created website docs stub. Status: partial (17 known gaps remain).
