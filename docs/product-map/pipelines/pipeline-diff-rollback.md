---
id: feat-pipelines-pipeline-diff-rollback
prd: 8.13
delivery-tasks: [task-nv10-pipeline-diff-rollback]
bdd: []
code:
  - backend/src/modulo/db/models/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot_versioning.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/runs.py
unit-tests:
  - backend/tests/unit/db/test_pipeline_snapshot.py
  - backend/tests/unit/pipelines/test_snapshot_versioning.py
  - backend/tests/unit/pipelines/test_snapshot_backward_compat.py
depends-on: []
status: partial
---

# Pipeline Snapshot Diff & Rollback

Discovered from 1 completed delivery tasks.

## Behaviours

### Happy Path

- [ ] Snapshot created automatically when a run is triggered
- [ ] List snapshots for a pipeline returns snapshots ordered by version descending with total count
- [ ] Get snapshot detail by ID returns full snapshot graph and metadata
- [ ] Tag and annotate a snapshot with custom tag and notes via PATCH
- [ ] Rollback to a previous snapshot creates a new snapshot that restores the graph, tagged `rollback-v{version}`
- [ ] Diff two snapshots returns added, removed, and modified nodes and edges with per-field changes

### Request Validation

- [ ] Non-existent snapshot ID returns 404
- [ ] Non-existent pipeline ID returns 404
- [ ] Invalid UUID format for snapshot or pipeline ID returns 422
- [ ] Page parameter less than 1 is rejected
- [ ] Page_size less than 1 or greater than 100 is rejected
- [ ] Diff with identical snapshot IDs returns empty change sets
- [ ] Rollback to snapshot from a different pipeline returns 404

### Auth & Permissions

- [ ] Unauthenticated requests return 401 (UNTESTED)
- [ ] Delete snapshot requires admin or owner role — operator or runner gets 403
- [ ] Snapshot access is scoped to the authenticated org via RLS (UNTESTED)
- [ ] Cross-org snapshot access returns 404 (not 403) to avoid leaking existence (UNTESTED)

### State & Lifecycle

- [ ] Snapshot version is monotonically increasing per pipeline (max existing + 1)
- [ ] Pipeline graph is locked with FOR UPDATE during snapshot creation
- [ ] The latest snapshot per pipeline cannot be deleted
- [ ] In-flight runs continue on their original snapshot after pipeline edit
- [ ] Rollback creates a new snapshot — old snapshots are never mutated
- [ ] Snapshot graph is immutable after creation

### Edge Cases

- [ ] Pipeline with no nodes produces snapshot with empty node list
- [ ] Pipeline with no edges produces snapshot with empty edge list
- [ ] Snapshot list for pipeline with no snapshots returns empty items and total=0
- [ ] Diff of two identical snapshots returns empty changes in all six categories
- [ ] Snapshot diff handles node_type field appearing or changing between versions
- [ ] Nodes without node_type field (old-format snapshots) default to 'agent' during compilation (UNTESTED at API level)
- [ ] Connector bindings store human-readable instance_name for historical display
- [ ] Credentials are excluded from snapshot connector and model backend pins

### Concurrency

- [ ] FOR UPDATE lock on pipeline serialises concurrent snapshot creation (UNTESTED)
- [ ] Rollback acquires FOR UPDATE lock, preventing concurrent pipeline edits during restore (UNTESTED)
- [ ] Snapshot version allocation is atomic within the transaction (UNTESTED)

### Error Handling

- [ ] Missing pipeline during run trigger returns 404 before snapshot creation fails
- [ ] Missing agents referenced by pipeline nodes — snapshot still created with available data
- [ ] Missing connectors referenced by pipeline nodes — snapshot still created with available info
- [ ] Missing schemas referenced by agents — snapshot still created with available data

### Backward Compatibility

- [ ] Old-format snapshots without node_type field compile and execute, defaulting node_type to 'agent'
- [ ] Old-format snapshots with HITL gate edges compile and execute
- [ ] Mixed old-format and new-format nodes compile correctly
- [ ] Old-format snapshots with 'role' field instead of 'node_type' compile and execute
- [ ] Old-format snapshots with unexpected fields are tolerated (ignored)
- [ ] Snapshot diff handles node_type appearing or changing between old and new format

## Known Gaps

- No BDD feature files cover snapshot, diff, or rollback scenarios
- No API-level integration tests for snapshot endpoints
- No tests for RLS enforcement across org boundaries for snapshot access
- No concurrency tests for FOR UPDATE lock serialisation during rollback
- No tests for auth/401 on snapshot endpoints
- Rollback-then-new-run flow not tested end-to-end
