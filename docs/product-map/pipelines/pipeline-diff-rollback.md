---
id: feat-pipelines-pipeline-diff-rollback
prd: 8.13
delivery-tasks: []
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
  - backend/tests/unit/api/test_error_handling.py
bdd: []
depends-on:
  - feat-pipelines-pipeline-versioning
status: partial
---

# Pipeline Snapshot Diff & Rollback

Automatic pipeline snapshotting plus structured diff and rollback, exposed through the
`snapshots` endpoints under `/api/v1/pipelines` (plus the `/runs` surface via
`runs-diff`, `/runs/diff`). A snapshot is captured automatically when a run is triggered,
so any pipeline version can be recovered or compared. Referenced by ADR 017/018 as a
product-map entry touched during centralized authorization cleanup.

## Behaviours

- [x] Snapshot created automatically when a run is triggered
- [x] List snapshots for a pipeline, ordered by version descending, with total count
- [x] Get snapshot detail by id (full snapshot graph and metadata)
- [x] Tag and annotate a snapshot via PATCH (custom tag + notes)
- [x] Rollback to a previous snapshot creates a new snapshot tagged `rollback-v{version}`
      that restores the graph
- [x] Diff two snapshots returns added / removed / modified nodes and edges with
      per-field changes
- [x] Non-existent snapshot or pipeline id → 404; invalid UUID → 422
- [x] Delete snapshot (versioned) is supported
- [x] Backward compatibility for pre-versioning snapshot records
- [x] Snapshot authorization re-reads the live role under the pipeline row lock
      (`rollback_to_snapshot`, graph-replace, pipeline update)

## Known Gaps

- **No BDD feature files** — snapshot/rollback is covered by unit tests
  (`backend/tests/unit/db/test_pipeline_snapshot.py`,
  `backend/tests/unit/pipelines/test_snapshot_versioning.py`); no pytest-bdd scenarios.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. The entry is referenced by ADR 017/018
  (centralized-authorization cleanup). Re-verified endpoints and CRUD modules against the
  current tree. Status: partial (no BDD coverage).