---
id: feat-pipelines-pipeline-versioning
prd: 8.13
delivery-tasks: [task-nv0-snapshot-expansion]
bdd: [backend/tests/bdd/features/pipelines/crud.feature]
unit-tests:
  - backend/tests/unit/db/test_pipeline_snapshot.py
  - backend/tests/unit/pipelines/test_snapshot_versioning.py
  - backend/tests/unit/pipelines/test_snapshot_backward_compat.py
code:
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot_versioning.py
  - backend/src/modulo/db/models/pipeline_snapshot.py
depends-on: [feat-pipelines-pipeline-diff-rollback]
status: partial
---

# Pipeline Versioning

## Behaviours

### Snapshot Creation

- [x] Snapshot is created at run-start with a frozen copy of the live pipeline graph
- [x] Snapshot includes all connector bindings, schema version pins, prompt version pins, and model backend pins
- [x] Locking the pipeline row makes snapshot version allocation atomic with respect to concurrent graph replacement
- [x] Snapshot version is auto-incremented (max existing version + 1) per pipeline
- [x] `graph_json` stores serialised nodes (agent_id, connector_binding) and edges (edge_type, hitl_gate_config)
- [x] `connector_bindings_json` stores human-readable instance_name for historical display
- [x] `prompt_pins_json` stores SHA-256 hash of prompt_template and `updated_at` timestamp
- [x] `schema_pins_json` stores schema_id, version, and abstract_name for each referenced schema
- [x] `model_backend_pins_json` stores model_backend_id and model_id for cost calculation
- [x] `run_context_defaults` is copied from the pipeline at snapshot time
- [x] Credentials are not copied into snapshot — connector/model pins reference by ID only
- [x] Created by account_id is recorded (nullable FK to accounts.id for trigger-initiated runs)
- [x] Missing pipeline returns None (no crash)

### Run Isolation

- [x] Pipeline is locked (`SELECT ... FOR UPDATE`) during snapshot creation to prevent concurrent graph replacement
- [ ] Active runs execute against their snapshot — live pipeline changes do not affect in-progress runs
- [ ] Runs started before a snapshot is replaced continue using the snapshot they were created with
- [ ] UI displays a warning when a user edits a pipeline while a run is `awaiting_human`

### Snapshot Querying

- [x] Snapshots can be listed for a pipeline, ordered by version descending
- [x] Listing supports pagination (page, page_size) and returns total count
- [x] Individual snapshot can be retrieved by ID with full graph detail
- [x] Snapshots created_at timestamp is recorded and displayable
- [ ] Snapshots display their version number prominently in the admin UI

### Tagging and Notes

- [x] Snapshots can be tagged with an optional string (max 100 chars)
- [x] Snapshots can have optional notes (max 2000 chars)
- [x] Tags and notes can be updated after creation

### Rollback

- [x] Rollback creates a new snapshot from the restored pipeline graph
- [x] Rollback assigns a `rollback-v<N>` tag and descriptive note
- [x] Rollback does not affect in-flight runs (they continue on their original snapshot)
- [x] Rollback to a snapshot from a different pipeline returns None

### Deletion

- [x] Deleting a historical snapshot removes it from the database
- [x] Deleting the latest snapshot is refused (returns False)
- [x] Deleting a non-existent snapshot returns False

### Diff

- [x] Diffing identical snapshots returns empty change sets
- [x] Diffing different snapshots returns added, removed, and modified nodes
- [x] Diffing different snapshots returns added, removed, and modified edges
- [x] Diffing includes full graph snapshots for both sides
- [x] Diff returns None if either snapshot does not exist

### Reference Integrity

- [x] Schemas are stored by ID + version (not embedded) — deletion protection is the integrity guarantee
- [x] Pipeline FK has `ON DELETE RESTRICT` — prevents deletion of pipeline with existing snapshots
- [x] Account FK has `ON DELETE SET NULL` — snapshot survives account deletion
- [x] Environment profile FK has `ON DELETE SET NULL`
- [x] Unique constraint on (pipeline_id, snapshot_version) prevents duplicate version numbers

### Error States

- [x] Snapshot creation fails gracefully if pipeline is not found (returns None)
- [x] Snapshot creation fails if a referenced agent, connector, schema, or model backend is missing
- [x] Concurrent snapshot creation for the same pipeline is serialised via row lock
- [x] Missing or null environment_profile_id is handled (field is nullable)

## Known Gaps

- No integration tests for the full snapshot -> run -> live-edit -> isolation lifecycle
- No concurrency tests for simultaneous snapshot creation and pipeline edit
- Missing UI behaviour specs for snapshot version display in pipeline history view
- Missing deletion protection lifecycle: what happens to snapshots when a referenced schema version is deprecated?
- No coverage for snapshot creation boundary: what happens if graph has 0 nodes?
