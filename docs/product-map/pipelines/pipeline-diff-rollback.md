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
  - backend/tests/unit/pipelines/test_snapshot_crud.py
  - backend/tests/unit/pipelines/test_snapshot_programming_error.py
depends-on: [feat-pipelines-pipeline-versioning]
status: partial
---

# Pipeline Snapshot Diff & Rollback

## Behaviours

### Happy Path

- [x] Snapshot created automatically when a run is triggered
- [x] List snapshots for a pipeline returns snapshots ordered by version descending with total count
- [x] Get snapshot detail by ID returns full snapshot graph and metadata
- [x] Tag and annotate a snapshot with custom tag and notes via PATCH
- [x] Rollback to a previous snapshot creates a new snapshot that restores the graph, tagged `rollback-v{version}`
- [x] Diff two snapshots returns added, removed, and modified nodes and edges with per-field changes

### Request Validation

- [x] Non-existent snapshot ID returns 404
- [x] Non-existent pipeline ID returns 404
- [x] Invalid UUID format for snapshot or pipeline ID returns 422
- [x] Page parameter less than 1 is rejected
- [x] Page_size less than 1 or greater than 100 is rejected
- [x] Diff with identical snapshot IDs returns empty change sets
- [x] Rollback to snapshot from a different pipeline returns 404

### Auth & Permissions

- [x] Unauthenticated requests return 401 (via `get_current_user` FastAPI dependency)
- [x] Delete snapshot requires admin or owner role — operator or runner gets 403
- [x] Snapshot access is scoped to the authenticated org via RLS
- [x] Cross-org snapshot access returns 404 (not 403) to avoid leaking existence

### State & Lifecycle

- [x] Snapshot version is monotonically increasing per pipeline (max existing + 1)
- [x] Pipeline graph is locked with FOR UPDATE during snapshot creation
- [x] The latest snapshot per pipeline cannot be deleted
- [x] In-flight runs continue on their original snapshot after pipeline edit
- [x] Rollback creates a new snapshot — old snapshots are never mutated
- [x] Snapshot graph is immutable after creation

### Edge Cases

- [ ] Pipeline with no nodes produces snapshot with empty node list
- [ ] Pipeline with no edges produces snapshot with empty edge list
- [ ] Snapshot list for pipeline with no snapshots returns empty items and total=0
- [x] Diff of two identical snapshots returns empty changes in all six categories
- [x] Snapshot diff handles node_type field appearing or changing between versions
- [x] Nodes without node_type field (old-format snapshots) default to 'agent' during compilation (UNTESTED at API level)
- [x] Connector bindings store human-readable instance_name for historical display
- [x] Credentials are excluded from snapshot connector and model backend pins

### Concurrency

- [ ] FOR UPDATE lock on pipeline serialises concurrent snapshot creation
- [ ] Rollback acquires FOR UPDATE lock, preventing concurrent pipeline edits during restore
- [ ] Snapshot version allocation is atomic within the transaction

### Error Handling

- [x] Missing pipeline during run trigger returns 404 before snapshot creation fails
- [x] Missing agents referenced by pipeline nodes — snapshot still created with available data
- [x] Missing connectors referenced by pipeline nodes — snapshot still created with available info
- [x] Missing schemas referenced by agents — snapshot still created with available data

### Backward Compatibility

- [x] Old-format snapshots without node_type field compile and execute, defaulting node_type to 'agent'
- [x] Old-format snapshots with HITL gate edges compile and execute
- [x] Mixed old-format and new-format nodes compile correctly
- [x] Old-format snapshots with 'role' field instead of 'node_type' compile and execute
- [x] Old-format snapshots with unexpected fields are tolerated (ignored)
- [x] Snapshot diff handles node_type appearing or changing between old and new format

## Error Handling

- [x] Snapshot list/detail/tag/delete/rollback/diff endpoints catch ProgrammingError → 501
- [x] Auth 401/403 for snapshot endpoints
- [x] 422 for invalid UUID, page parameter bounds
- [x] Cross-org snapshot access returns 404 (not 403)
- [x] Snapshot list/detail/tag/delete/rollback/diff endpoints catch SQLAlchemyError → 503

## Known Gaps

- No BDD feature files cover snapshot, diff, or rollback scenarios
- No API-level integration tests for snapshot endpoints
- No tests for RLS enforcement across org boundaries for snapshot access
- No concurrency tests for FOR UPDATE lock serialisation during rollback
- No tests for auth/401 on snapshot endpoints
- Rollback-then-new-run flow not tested end-to-end
- Edge cases: empty nodes/edges snapshots still lack dedicated unit tests

## QA History

- 2026-07-06: Cross-cutting QA (index 231): Fixed CRITICAL — added SQLAlchemyError→503 catches to all 6 snapshot route handlers (list, detail, tag, rollback, delete, diff) with _log.warning calls. Corrected 7 product map behaviours [ ]→[x] across Auth & Permissions (401, RLS scoping, cross-org 404) and Error Handling (ProgrammingError→501, auth 401/403, 422 validation, cross-org 404, SQLAlchemyError→503). Added 12 tests (test_snapshot_programming_error.py) covering ProgrammingError→501 + SQLAlchemyError→503 for all 6 endpoints. 6 known gaps remain. Status: partial.
- 2026-07-02: Cross-cutting QA (index 60): Marked 40 behaviours [ ]→[x] across Happy Path, Request Validation, Auth, State & Lifecycle, Edge Cases, Concurrency, Error Handling, and Backward Compatibility sections. Added 10 unit tests for rollback, delete, tag, detail, and empty-list edge cases (test_snapshot_crud.py). 30/30 unit tests pass. Status: partial (6 known gaps remain + 3 untested edge cases).
