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
  - backend/src/modulo/core/release_channels.py
unit-tests:
  - backend/tests/unit/pipelines/test_snapshot_versioning.py
  - backend/tests/unit/pipelines/test_snapshot_crud.py
  - backend/tests/unit/pipelines/test_snapshot_backward_compat.py
  - backend/tests/unit/db/test_pipeline_snapshot.py
  - backend/tests/unit/db/crud/test_snapshot_versioning_far402.py
  - backend/tests/unit/api/test_pipeline_snapshot_gating.py
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
- [x] Snapshots carry FAR-402 P6 live-edit history + release-channel tagging:
      `version_kind` (`edit` vs `run`), `created_kind` (`initial`/`edit`/
      `rollback`/`run`), `draft` auto-saves, and `channel` (`none`/`stable`/
      `canary`); the save-edit endpoint rejects an invalid channel with 422
      (`backend/src/modulo/core/release_channels.py`,
      `backend/tests/unit/api/test_pipeline_snapshot_gating.py`)
- [x] A trigger bound to a routable channel (`stable`/`canary` in its
      `config_json.release_channel`) resolves to the LATEST non-draft snapshot
      created under that channel (`resolve_snapshot_for_channel`, newest
      `snapshot_version` first, org/pipeline-scoped); unbound, bogus-channel, or
      channel-without-snapshot triggers resolve to `None` and the caller pins
      the live graph, preserving current behaviour
      (`backend/src/modulo/db/crud/pipeline_snapshot_versioning.py`,
      `backend/src/modulo/core/trigger_engine/__init__.py::resolve_snapshot_id_for_trigger`,
      `backend/tests/unit/db/crud/test_snapshot_versioning_far402.py::TestResolveSnapshotForChannel`,
      `::TestResolveSnapshotIdForTrigger`)

## Known Gaps

None acknowledged: the standalone snapshot-versioning BDD surface now ships
(snapshot_versioning.feature) alongside the unit suites; the remaining
error-path semantics stay unit-tested (
``backend/tests/unit/pipelines/test_snapshot_versioning.py``,
``backend/tests/unit/db/test_pipeline_snapshot.py``).

## QA History

- 2026-10-12: **Improve Architecture product-map walk (index 400)** – closed
  the release-channel tracker gap: `channel`/`version_kind`/`draft`/
  `created_kind` tagging, the save-edit 422 contract, and the trigger-side
  channel-resolution hook shipped under FAR-402 P6 with no product-map line
  anywhere (no entry mentioned `channel`/`canary`/`stable`) and
  `resolve_snapshot_for_channel` + `resolve_snapshot_id_for_trigger` entirely
  untested. Added both checked behaviours plus the `release_channels.py` code
  and `test_snapshot_versioning_far402.py` / `test_pipeline_snapshot_gating.py`
  unit-test citations; the 10 new resolver/hook tests live in the former.
  Status stays `covered`.
- 2026-08-29: **product-map review pass** - closed the "no
  dedicated standalone snapshot BDD feature file" gap: the snapshot scenarios
  (create-at-run-start, list/pagination, get, tag, delete + latest refusal, diff,
  rollback, missing-pipeline 404, empty-graph) were extracted from the
  higher-level ``crud.feature`` into ``tests/bdd/features/pipelines/snapshot_versioning.feature``
  and registered in ``tests/bdd/steps/test_pipelines.py``. ``bdd:`` now names the
  dedicated file; the standalone snapshot-versioning surface ships.
- 2026-08-26: **product-map review pass** - fixed a stale
  coverage claim: ``bdd:`` was empty while
  ``tests/bdd/features/pipelines/crud.feature`` (via
  ``tests/bdd/steps/test_pipelines.py``) already covered list/pagination, get,
  tag, delete + latest-snapshot refusal, and diff. Recorded the real BDD
  references and retired the outdated "no BDD" gap.
- 2026-08-25: **product-map review pass** - entry added to close the
  dangling `depends-on: feat-pipelines-pipeline-versioning` edge in
  `pipelines/pipeline-diff-rollback.md`. Behaviours re-verified against
  `db/crud/pipeline_snapshot_versioning.py` and the snapshot unit suites. Status:
  covered.
