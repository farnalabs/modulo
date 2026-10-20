---
id: feat-pipelines-pipeline-versioning
prd: 8.13
adr: []
code:
  - backend/src/modulo/db/models/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot_versioning.py
  - backend/src/modulo/db/crud/pipeline.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/runs.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/core/release_channels.py
  - backend/src/modulo/db/migrations/versions/0317_drop_retired_pipeline_diff_rollback_flag.py
  - frontend/src/views/PipelineEditorView.vue
  - frontend/src/components/pipeline/PipelineSnapshotTimeline.vue
  - frontend/src/components/pipeline/PipelineVersionChip.vue
  - frontend/src/components/pipeline/PipelineSnapshotCompare.vue
  - frontend/src/components/runs/RunPipelineVersionLink.vue
unit-tests:
  - backend/tests/unit/pipelines/test_snapshot_versioning.py
  - backend/tests/unit/pipelines/test_snapshot_crud.py
  - backend/tests/unit/pipelines/test_snapshot_backward_compat.py
  - backend/tests/unit/db/test_pipeline_snapshot.py
  - backend/tests/unit/db/crud/test_snapshot_versioning_far402.py
  - backend/tests/unit/api/test_pipeline_snapshot_gating.py
  - backend/tests/integration/db/test_migration_0317_drop_retired_pipeline_diff_rollback_flag.py
  - frontend/src/__tests__/PipelineEditorHistoricVersion.spec.ts
  - frontend/src/__tests__/PipelineEditorCompare.spec.ts
  - frontend/src/__tests__/components/PipelineSnapshotTimeline.spec.ts
  - frontend/src/__tests__/components/PipelineSnapshotCompare.spec.ts
  - frontend/src/__tests__/RunPipelineVersionLink.spec.ts
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
trigger and on every explicit graph save, each carries an incrementing
`snapshot_version`, and the versioning CRUD (list, get by id or version, tag,
annotate, delete, rollback, diff) is the data layer under
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
- [x] An explicit pipeline graph save records the save as an immutable live-edit
      version (FAR-1810): every explicit-save path (`PUT /{pipeline_id}/graph`,
      `PATCH /{pipeline_id}` carrying `graph_json`, the node-targeted
      `PATCH /{pipeline_id}/graph/nodes/{node_id}`, the two node-conversion
      saves and the MCP `update_pipeline_graph` tool) writes a
      `version_kind='edit'` / `created_kind='edit'` snapshot in the SAME
      transaction as the graph write, after the graph rows and the policy-gate
      reconciliation. A save whose incoming graph is semantically identical to
      the live one records no snapshot, and an allocation failure rolls the
      whole save back rather than committing unversioned
      (`backend/src/modulo/db/crud/pipeline.py::replace_pipeline_graph` +
      `_record_edit_snapshot_if_needed`,
      `backend/src/modulo/db/crud/pipeline_snapshot.py::create_snapshot_edit`)
- [x] The editor opens a saved version READ-ONLY from `?snapshot=<uuid>` (the
      canonical deep link) or `?version=<n>` (an alias resolved against the
      version-descending snapshot list; `snapshot` wins when both are present),
      and `?compare=<uuid>` opens the semantic compare mode for the viewed
      version. While viewing, the frozen snapshot graph renders on the canvas
      and the editable state keeps the LIVE graph, so no Save can rewrite live
      with a stale graph; every write affordance is unmounted rather than
      disabled (`frontend/src/views/PipelineEditorView.vue`,
      `frontend/src/components/pipeline/PipelineSnapshotCompare.vue`,
      `frontend/src/__tests__/PipelineEditorHistoricVersion.spec.ts`,
      `frontend/src/__tests__/PipelineEditorCompare.spec.ts`)
- [x] Run surfaces link to the version the run executed (FAR-1813): the runs
      list row and the run detail render the pipeline name as
      `/pipelines/{pipeline_id}/editor?snapshot={snapshot_id}`, built ONLY from
      that run's own `snapshot_id`, plus a `v{n}` chip fed by the run's
      `snapshot_version`. A legacy run with no snapshot links to the live editor
      and renders no chip, and a deleted pipeline renders plain text
      (`frontend/src/components/runs/RunPipelineVersionLink.vue`,
      `backend/src/modulo/api/routes/runs.py`,
      `frontend/src/__tests__/RunPipelineVersionLink.spec.ts`)
- [x] Snapshot save, list, get, diff and rollback are Community-tier: the
      `pipeline_diff_rollback` plan gate is RETIRED (FAR-1819): the
      `require_feature` guard is gone from the routes and the key is removed
      from the feature registry, and migration
      `0317_drop_retired_pipeline_diff_rollback_flag` sweeps the stale
      `feature_flag_catalog` row so no admin surface advertises a flag that
      gates nothing (`backend/tests/unit/api/test_pipeline_snapshot_gating.py`,
      `backend/src/modulo/db/migrations/versions/0317_drop_retired_pipeline_diff_rollback_flag.py`)
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

- 2026-10-19: **FAR-1814 docs drift pass** – documented four shipped surfaces
  that had no tracker line: (1) the FAR-1810 save→edit-snapshot contract
  (`replace_pipeline_graph` calls `_record_edit_snapshot_if_needed` →
  `create_snapshot_edit`, same transaction, no-op suppressed, fail-closed);
  (2) the FAR-1812/1818 read-only historic view and its
  `?snapshot` / `?version` / `?compare` route query;
  (3) FAR-1813 run→version linking on the runs list and run detail; and
  (4) the FAR-1819 retirement of the `pipeline_diff_rollback` tier gate, with
  migration 0317 sweeping the stale feature-flag catalog row. Every claim was
  re-verified against the code before writing; `status` stays `covered`.
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
