---
id: feat-pipelines-pipeline-diff-rollback
prd: 8.13
delivery-tasks: []
code:
  - backend/src/modulo/db/models/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline_snapshot_versioning.py
  - backend/src/modulo/db/crud/pipeline.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/runs.py
  - backend/src/modulo/db/migrations/versions/0317_drop_retired_pipeline_diff_rollback_flag.py
  - frontend/src/views/PipelineEditorView.vue
  - frontend/src/components/pipeline/PipelineSnapshotTimeline.vue
  - frontend/src/components/pipeline/PipelineSnapshotCompare.vue
unit-tests:
  - backend/tests/unit/db/test_pipeline_snapshot.py
  - backend/tests/unit/pipelines/test_snapshot_versioning.py
  - backend/tests/unit/pipelines/test_snapshot_backward_compat.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/db/crud/test_snapshot_versioning_far402.py
  - backend/tests/unit/db/crud/test_far1666_policy_gate_graph_writers.py
  - backend/tests/unit/db/crud/test_pipeline_graph_updated_audit.py
  - backend/tests/unit/api/test_pipeline_snapshot_gating.py
  - backend/tests/unit/api/test_snapshot_path_scoping_and_residual_gates.py
  - backend/tests/integration/db/test_migration_0317_drop_retired_pipeline_diff_rollback_flag.py
  - frontend/src/__tests__/PipelineEditorHistoricVersion.spec.ts
  - frontend/src/__tests__/PipelineEditorCompare.spec.ts
  - frontend/src/__tests__/components/PipelineSnapshotTimeline.spec.ts
  - frontend/src/__tests__/components/PipelineSnapshotCompare.spec.ts
bdd:
  - backend/tests/bdd/features/pipelines/snapshot_versioning.feature
  - backend/tests/bdd/features/pipelines/crud.feature
  - backend/tests/bdd/steps/test_pipelines.py
depends-on:
  - feat-pipelines-pipeline-versioning
status: covered
---

# Pipeline Snapshot Diff & Rollback

Automatic pipeline snapshotting plus structured diff and rollback, exposed through the
`snapshots` endpoints under `/api/v1/pipelines`. A snapshot is captured automatically
when a run is triggered and on every explicit graph save, so any pipeline version can be
recovered or compared. Referenced by ADR 047/018 as a product-map entry touched during
centralized authorization cleanup.

## Behaviours

- [x] Snapshot created automatically when a run is triggered and on every
      explicit graph save (the save→edit contract is detailed under
      `feat-pipelines-pipeline-versioning`)
- [x] List snapshots for a pipeline, ordered by version descending, with total count
- [x] Get snapshot detail by id (full snapshot graph and metadata)
- [x] Tag and annotate a snapshot via PATCH (custom tag + notes)
- [x] Rollback to a previous snapshot creates a NEW live-edit snapshot tagged
      `rollback-v{version}` that restores the graph: `rollback_to_snapshot`
      writes `version_kind='edit'` / `created_kind='rollback'` with the next
      allocated `snapshot_version`, so a restore is a new version rather than a
      rewrite and history is never deleted, so the same target can be restored
      again (`backend/src/modulo/db/crud/pipeline_snapshot_versioning.py::rollback_to_snapshot`)
- [x] The editor's restore flow is ONE shared FormDialog opened from the
      read-only historic view (the persistent banner's Restore this version and
      the version timeline's per-version action): it embeds the old-vs-latest
      change summary from `POST /snapshots/diff` (nodes restored/removed/
      modified, connections added/removed/modified, downstream impact, breaking
      port changes), names the real consequences (new runs use the restored
      version, edits made since stop being used, history is NOT deleted, runs
      already in progress keep the version they started on), and uses a plain
      confirm (no typed phrase), because the action is reversible. A refusal
      that would remove a HITL gate, strip a node-bound guardrail, or use
      another team's connector is rendered as plain-language copy, never the raw
      server error; success navigates back to the latest version and announces
      the outcome (`Restored v{n}. Created v{m}.`) in an `<output>` status
      notice (`frontend/src/views/PipelineEditorView.vue`,
      `frontend/src/__tests__/PipelineEditorHistoricVersion.spec.ts`)
- [x] The historic version view and the restore flow are reachable from the
      route deep links `?snapshot=<uuid>` / `?version=<n>`; the full read-only
      contract (frozen graph, live editor state preserved, write affordances
      unmounted, `?compare=<uuid>`) is tracked under
      `feat-pipelines-pipeline-versioning`
- [x] Snapshot diff and rollback are Community-tier: the
      `pipeline_diff_rollback` plan gate is RETIRED (FAR-1819): no
      `require_feature` guard remains on the routes, and migration
      `0317_drop_retired_pipeline_diff_rollback_flag` deletes the stale
      `feature_flag_catalog` row so the admin Feature Flags surface cannot
      advertise a flag that gates nothing
      (`frontend/tests/e2e/journeys/snapshot-versioning-journey.spec.ts` asserts
      no 402; `backend/tests/unit/api/test_pipeline_snapshot_gating.py`)
- [x] Rollback is guarded under the pipeline row lock before any graph mutation:
      a HITL-gate weakening is denied (`HitlReviewWeakeningDenied`, legacy
      snapshots fail closed as `legacy-snapshot-ambiguous`), a non-admin may not
      roll back to a snapshot that drops a guardrail-bound node, and the live
      role is re-read under the lock for `rest` callers
      (`backend/src/modulo/db/crud/pipeline_snapshot_versioning.py::rollback_to_snapshot`,
      `backend/tests/bdd/features/pipelines/snapshot_versioning.feature`
      "Rollback that would weaken a HITL review is denied")
- [x] Rollback reconciles declared `policy_gate` blocks against the pre-rollback
      graph in the same transaction through the shared
      `reconcile_graph_policy_gates` (FAR-1666): a gate the rollback reintroduces
      gets its row, one it drops is soft-deleted - without this the restored
      graph would carry a block with no governing row
      (`backend/src/modulo/db/crud/pipeline_snapshot_versioning.py`,
      `backend/tests/unit/db/crud/test_far1666_policy_gate_graph_writers.py`)
- [x] Rollback runs the connector-team gate under the same row lock via the
      `post_lock_gate` hook (FAR-1515 MAJOR 3): the target snapshot's bindings
      are judged against the pipeline's CURRENT owner team and a mismatch raises
      the shared named 409 `connector_team_mismatch` - the team boundary may
      have moved since the snapshot was taken (shared rule tracked under
      `feat-connectors`)
      (`backend/src/modulo/api/routes/pipelines.py::rollback_snapshot_endpoint`)
- [x] Rollback emits the same `pipeline.graph_updated` audit event a graph write
      does (FAR-1471), attributed to the rollback caller, in the same transaction
      as the restore
      (`backend/tests/unit/db/crud/test_pipeline_graph_updated_audit.py`)
- [x] Tag / diff / delete scope their snapshot reads to the `{pipeline_id}` path
      segment (FAR-1360): a snapshot of another pipeline resolves to `None` and
      the route maps it to the existing 404, so the path is never decorative;
      every mutation additionally re-checks the team gate inside the transaction
      (FAR-1312 TOCTOU close)
      (`backend/tests/unit/api/test_snapshot_path_scoping_and_residual_gates.py`)
- [x] Diff two snapshots returns added / removed / modified nodes and edges with
      per-field changes, plus the FAR-402 P6 semantic layer: aggregated
      port-signature deltas, downstream `impacted_nodes`, and `breaking_changes`
      (`block` vs `warning`)
      (`backend/tests/unit/db/crud/test_snapshot_versioning_far402.py::TestDiffSnapshotsSemantic`)
- [x] Non-existent snapshot or pipeline id → 404; invalid UUID → 422
- [x] Delete snapshot (versioned) is supported
- [x] Backward compatibility for pre-versioning snapshot records
- [x] Snapshot authorization re-reads the live role under the pipeline row lock
      (`rollback_to_snapshot`, graph-replace, pipeline update)

## Known Gaps

- The FAR-402 P6 release-channel follow-ups stay out of scope by design
  (`backend/src/modulo/core/release_channels.py` contract): the channel metrics
  pipeline, the promotion/rollback dashboard, and the channel-promotion reducer
  do not exist - snapshots can be tagged per channel and triggers resolve to
  the latest channel snapshot, but nothing promotes a channel or auto-rolls one
  back on error-rate signal (`should_rollback` is a pure, unit-tested oracle
  with no caller yet).
- Lens-9 evaluation 2026-10-12: `diff_snapshots` indexes nodes by `n["id"]`
  (KeyError on an id-less node) while `rollback_to_snapshot` tolerates
  schemaless historic nodes via `node.get("id")`. Judged not actionable: the
  graph validator rejects id-less nodes on every write path
  (`TOPOLOGY_NODE_MISSING_ID`), so no stored snapshot reachable through a
  validated write can trigger it - hardening the diff would mask corruption
  rather than fix a reachable crash. Revisit if an unvalidated graph writer
  ever ships.

## QA History

- 2026-10-19: **FAR-1814 docs drift pass** – documented the shipped restore and
  gating surfaces that had no tracker line: rollback now names its
  `version_kind='edit'` / `created_kind='rollback'` provenance and the fact it
  never deletes history; the editor's shared restore dialog (diff summary,
  consequences, plain confirm, plain-language guard refusals, success notice);
  the `?snapshot` / `?version` historic entry point (cross-referenced to
  `feat-pipelines-pipeline-versioning`); and the FAR-1819 retirement of the
  `pipeline_diff_rollback` tier gate plus migration 0317's catalog sweep. Every
  claim was re-verified against the code before writing; `status` stays
  `covered`.
- 2026-10-12: **Improve Architecture product-map walk (index 400)** – closed
  five untracked sub-surfaces shipped since the 2026-08-29 pass: (1) FAR-1666
  policy-gate reconciliation on rollback (same-txn `reconcile_graph_policy_gates`,
  tested but undescribed - added checked behaviour + `test_far1666_*` citation);
  (2) FAR-1515 MAJOR 3 connector-team `post_lock_gate` on rollback (added checked
  behaviour cross-referencing `feat-connectors`); (3) FAR-1471
  `pipeline.graph_updated` audit on rollback (added checked behaviour +
  `test_pipeline_graph_updated_audit.py` citation); (4) FAR-1360 path scoping +
  FAR-1312 in-txn team-gate re-check on tag/diff/delete (added checked behaviour
  + `test_snapshot_path_scoping_and_residual_gates.py` citation); (5) FAR-402 P6
  semantic diff layer (added checked behaviour + `TestDiffSnapshotsSemantic`
  citation). MAJOR coverage fix: `resolve_snapshot_for_channel` (CRUD) and
  `TriggerEngine.resolve_snapshot_id_for_trigger` (hook) had ZERO tests - added
  `TestResolveSnapshotForChannel` (5 tests: passthrough, None, compiled-statement
  pipeline/channel/draft-exclusion/version-ordering/limit scoping, org-scope
  present/absent) + `TestResolveSnapshotIdForTrigger` (5 tests: none/unbound/
  bogus → None with no DB hit, bound → channel snapshot id, bound-but-absent →
  None) to `test_snapshot_versioning_far402.py` (26 → 36 tests, all pass).
  Recorded two Known Gaps: release-channel promotion/metrics/dashboard follow-ups
  (`should_rollback` verified caller-less), and the evaluated-not-actionable
  id-less-node diff asymmetry. Status stays `covered`.
- 2026-08-29: **product-map review pass** - closed the "no
  dedicated standalone snapshot BDD feature file" gap: the snapshot/rollback/diff
  scenarios (create-at-run-start, list/pagination, get, tag, rollback + HITL-gate
  weakening denial, delete + latest refusal, diff, missing-pipeline 404,
  empty-graph) were extracted from the higher-level ``crud.feature`` into
  ``tests/bdd/features/pipelines/snapshot_versioning.feature`` and registered in
  ``tests/bdd/steps/test_pipelines.py``. ``bdd:`` now names the dedicated file.
- 2026-08-26: **product-map review pass** - fixed a stale
  coverage claim: the entry was parked at ``status: partial`` with ``bdd: []``
  while ``tests/bdd/features/pipelines/crud.feature`` already exercised
  create-at-run-start, list/pagination, get, tag, rollback (incl. HITL-gate
  weakening denial), delete (+latest refusal), diff, and missing-pipeline 404
  via ``tests/bdd/steps/test_pipelines.py``. Recorded the real BDD references and
  moved the entry to ``status: covered``.
- 2026-08-25: **product-map review pass** - restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. The entry is referenced by ADR 047/018
  (centralized-authorization cleanup). Re-verified endpoints and CRUD modules against the
  current tree. Status: partial (no BDD coverage).
