---
id: feat-pipelines-pipeline-versioning
prd: 8.13
adr: []
code:
  - backend/src/modulo/db/models/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot_versioning.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/runs.py
unit-tests:
  - backend/tests/unit/pipelines/test_snapshot_versioning.py
  - backend/tests/unit/pipelines/test_snapshot_crud.py
  - backend/tests/unit/pipelines/test_snapshot_backward_compat.py
  - backend/tests/unit/db/test_pipeline_snapshot.py
bdd:
  - backend/tests/bdd/features/pipelines/snapshot_versioning.feature
  - backend/tests/bdd/features/pipelines/crud.feature
  - backend/tests/bdd/steps/test_pipelines.py
depends-on:
  - feat-pipelines
status: covered
---

# Pipeline Snapshot Versioning

Versioned pipeline snapshots: a snapshot is captured automatically at run
trigger, each carries an incrementing `snapshot_version`, and the versioning
CRUD (tag, annotate, delete, rollback, diff) is the data layer under
`feat-pipelines-pipeline-diff-rollback`.

## Behaviours

- [x] Snapshots are created from the live graph and ordered by version descending
      with total count
- [x] Diff between two snapshots returns added / removed / modified nodes and
      edges with per-field changes (including connector bindings, environment
      bindings, and HITL gate config), and an empty diff when identical
- [x] Diff for a missing snapshot resolves to `None` (caller surfaces 404)
- [x] Tag / annotate a snapshot via PATCH
- [x] Delete refuses the latest snapshot of a pipeline
- [x] Backward compatibility for pre-versioning snapshot records
- [x] Versioning CRUD is the back-end of the `snapshots` endpoints under
      `/api/v1/pipelines`

## Known Gaps

None acknowledged: the standalone snapshot-versioning BDD surface now ships
(snapshot_versioning.feature) alongside the unit suites; the remaining
error-path semantics stay unit-tested (
``backend/tests/unit/pipelines/test_snapshot_versioning.py``,
``backend/tests/unit/db/test_pipeline_snapshot.py``).

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — closed the "no
  dedicated standalone snapshot BDD feature file" gap: the snapshot scenarios
  (create-at-run-start, list/pagination, get, tag, delete + latest refusal, diff,
  rollback, missing-pipeline 404, empty-graph) were extracted from the
  higher-level ``crud.feature`` into ``tests/bdd/features/pipelines/snapshot_versioning.feature``
  and registered in ``tests/bdd/steps/test_pipelines.py``. ``bdd:`` now names the
  dedicated file; the standalone snapshot-versioning surface ships.
- 2026-08-26: **improve-architecture (product-map walk)** — fixed a stale
  coverage claim: ``bdd:`` was empty while
  ``tests/bdd/features/pipelines/crud.feature`` (via
  ``tests/bdd/steps/test_pipelines.py``) already covered list/pagination, get,
  tag, delete + latest-snapshot refusal, and diff. Recorded the real BDD
  references and retired the outdated "no BDD" gap.
- 2026-08-25: **improve-architecture (product-map walk)** — entry added to close the
  dangling `depends-on: feat-pipelines-pipeline-versioning` edge in
  `pipelines/pipeline-diff-rollback.md`. Behaviours re-verified against
  `db/crud/pipeline_snapshot_versioning.py` and the snapshot unit suites. Status:
  covered.
