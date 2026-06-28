---
id: feat-connectors-schema-inference
prd: 8.16
delivery-tasks: []
  - backend/tests/bdd/features/connectors/schema_inference.feature
unit-tests: []
code:
  - backend/src/modulo/connectors/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-core-schema-inference
status: partial
---

# Schema Inference from Connected Tools

LLM-assisted schema draft generation from connected tool data (issue trackers, git hosts). When onboarding an existing SDLC, operators connect their tools (Jira, Linear, GitHub, GitLab) and Modulo inspects the tool's data to propose a schema for the schemas that will drive the pipeline. This feature couples connector data inspection with the schema inference system described in `feat-core-schema-inference`.

## Behaviours

### Tool Data Inspection — connector-agnostic read interface

- [ ] ConnectorHub provides a read interface for schema inference that works across all `ConnectorType` instances
- [ ] Schema inference reads sample data from the connected tool: issue records from Jira/Linear, repo metadata from GitHub/GitLab
- [ ] Reads respect the connector's ACL and credential scope — no escalation via inference path
- [ ] Reads are limited to a configurable sample size (default: 20 records) to avoid rate-limit pressure
- [ ] Sample data is sanitised before being sent to the inference LLM — no plaintext credentials, no internal URLs
- [ ] Read timeout is independent of pipeline-run read timeout (shorter: 10s default)

### Schema Generation — LLM-assisted draft from sample data

- [ ] Sample data is sent as context to a model backend for field extraction and typing
- [ ] The inference LLM returns a proposed JSON Schema draft with field names, types, optional descriptions
- [ ] The draft schema is stored as a new unreleased schema version in the Schema Registry
- [ ] The draft schema is marked `status: draft` — not usable in pipelines until reviewed and published
- [ ] The operator can edit the draft schema before publishing (same schema editor as manual creation)
- [ ] The operator can reject the draft schema entirely (deletes the draft version)
- [ ] The inference run is recorded in the audit log with the tool source and model used

### Connector Type Awareness — type-specific field extraction

- [ ] Issue-tracker connectors (Jira, Linear) propose fields from issue metadata: summary, description, priority, status, assignee, labels
- [ ] Git-host connectors (GitHub, GitLab) propose fields from repository/PR metadata: repo name, branch, PR title, PR description, file paths
- [ ] CI-runner connectors propose fields from pipeline/job metadata: pipeline ID, status, branch, commit SHA, duration
- [ ] Chat connectors (Slack) propose fields derived from message/channel metadata
- [ ] Generic connectors with no specialised schema return a minimal field set (id, name, type)

### Edge Cases and Error States

- [ ] Connected tool has no data (empty project, new workspace) — inference returns a minimal placeholder draft
- [ ] Connected tool returns insufficient permissions — inference is blocked with a named error
- [ ] Connected tool times out — inference fails gracefully with a timeout error
- [ ] Inference LLM call fails — original error surfaced, no partial draft created
- [ ] Inferred schema contains unsupported field types (e.g. `anyOf`, `$ref`) — schema is edited before publishing
- [ ] Draft schema naming — auto-generated name from tool type + project name, editable by operator
- [ ] Concurrency: only one active inference per (org, connector_instance) — subsequent requests queued

### Security and Data Isolation

- [ ] Sample data never leaves the org's ModelBackend (inference uses the org's configured model backend)
- [ ] Sample data is discarded after inference completes — not stored in the database
- [ ] Inferred schema is scoped to the org — not shared across orgs
- [ ] Connector ACL is enforced: operator must have read access to the connector instance
- [ ] Schema inference is behind an enterprise feature flag (v1 feature)

## Known Gaps

- [ ] **No implementation exists**: this entire feature is speculative — no code, no BDD scenarios
- [ ] **Overlaps with feat-core-schema-inference**: the core schema inference service (LLM-based schema generation from text descriptions) is defined at `core/schema-inference.md` — this connector-specific variant inspects live tool data rather than accepting a user description
- [ ] **BDD placeholder**: `backend/tests/bdd/features/connectors/schema_inference.feature` is a 3-line placeholder with no real scenarios
- [ ] **No unit tests**: no test files exist
- [ ] **Connector read interface for inference not defined**: ConnectorHub has no `infer_schema()` method or equivalent
- [ ] **Data sanitisation rules not defined**: what fields are scrubbed from sample data before LLM inference is unspecified
- [ ] **No CLI or UI for triggering inference**: no endpoint, no button, no wizard step
