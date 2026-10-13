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
  - backend/tests/unit/db/crud/test_snapshot_versioning_far402.py
  - backend/tests/unit/db/crud/test_far1666_policy_gate_graph_writers.py
  - backend/tests/unit/db/crud/test_pipeline_graph_updated_audit.py
  - backend/tests/unit/api/test_pipeline_snapshot_gating.py
  - backend/tests/unit/api/test_snapshot_path_scoping_and_residual_gates.py
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
`snapshots` endpoints under `/api/v1/pipelines`. A snapshot is captured automatically when a run is triggered,
so any pipeline version can be recovered or compared. Referenced by ADR 047/018 as a
product-map entry touched during centralized authorization cleanup.

## Behaviours

- [x] Snapshot created automatically when a run is triggered
- [x] List snapshots for a pipeline, ordered by version descending, with total count
- [x] Get snapshot detail by id (full snapshot graph and metadata)
- [x] Tag and annotate a snapshot via PATCH (custom tag + notes)
- [x] Rollback to a previous snapshot creates a new snapshot tagged `rollback-v{version}`
      that restores the graph
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
      gets its row, one it drops is soft-deleted — without this the restored
      graph would carry a block with no governing row
      (`backend/src/modulo/db/crud/pipeline_snapshot_versioning.py`,
      `backend/tests/unit/db/crud/test_far1666_policy_gate_graph_writers.py`)
- [x] Rollback runs the connector-team gate under the same row lock via the
      `post_lock_gate` hook (FAR-1515 MAJOR 3): the target snapshot's bindings
      are judged against the pipeline's CURRENT owner team and a mismatch raises
      the shared named 409 `connector_team_mismatch` — the team boundary may
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
  do not exist — snapshots can be tagged per channel and triggers resolve to
  the latest channel snapshot, but nothing promotes a channel or auto-rolls one
  back on error-rate signal (`should_rollback` is a pure, unit-tested oracle
  with no caller yet).
- Lens-9 evaluation 2026-10-12: `diff_snapshots` indexes nodes by `n["id"]`
  (KeyError on an id-less node) while `rollback_to_snapshot` tolerates
  schemaless historic nodes via `node.get("id")`. Judged not actionable: the
  graph validator rejects id-less nodes on every write path
  (`TOPOLOGY_NODE_MISSING_ID`), so no stored snapshot reachable through a
  validated write can trigger it — hardening the diff would mask corruption
  rather than fix a reachable crash. Revisit if an unvalidated graph writer
  ever ships.

## QA History

- 2026-10-12: **Improve Architecture product-map walk (index 400)** – closed
  five untracked sub-surfaces shipped since the 2026-08-29 pass: (1) FAR-1666
  policy-gate reconciliation on rollback (same-txn `reconcile_graph_policy_gates`,
  tested but undescribed — added checked behaviour + `test_far1666_*` citation);
  (2) FAR-1515 MAJOR 3 connector-team `post_lock_gate` on rollback (added checked
  behaviour cross-referencing `feat-connectors`); (3) FAR-1471
  `pipeline.graph_updated` audit on rollback (added checked behaviour +
  `test_pipeline_graph_updated_audit.py` citation); (4) FAR-1360 path scoping +
  FAR-1312 in-txn team-gate re-check on tag/diff/delete (added checked behaviour
  + `test_snapshot_path_scoping_and_residual_gates.py` citation); (5) FAR-402 P6
  semantic diff layer (added checked behaviour + `TestDiffSnapshotsSemantic`
  citation). MAJOR coverage fix: `resolve_snapshot_for_channel` (CRUD) and
  `TriggerEngine.resolve_snapshot_id_for_trigger` (hook) had ZERO tests — added
  `TestResolveSnapshotForChannel` (5 tests: passthrough, None, compiled-statement
  pipeline/channel/draft-exclusion/version-ordering/limit scoping, org-scope
  present/absent) + `TestResolveSnapshotIdForTrigger` (5 tests: none/unbound/
  bogus → None with no DB hit, bound → channel snapshot id, bound-but-absent →
  None) to `test_snapshot_versioning_far402.py` (26 → 36 tests, all pass).
  Recorded two Known Gaps: release-channel promotion/metrics/dashboard follow-ups
  (`should_rollback` verified caller-less), and the evaluated-not-actionable
  id-less-node diff asymmetry. Status stays `covered`.
- 2026-08-29: **product-map review pass** — closed the "no
  dedicated standalone snapshot BDD feature file" gap: the snapshot/rollback/diff
  scenarios (create-at-run-start, list/pagination, get, tag, rollback + HITL-gate
  weakening denial, delete + latest refusal, diff, missing-pipeline 404,
  empty-graph) were extracted from the higher-level ``crud.feature`` into
  ``tests/bdd/features/pipelines/snapshot_versioning.feature`` and registered in
  ``tests/bdd/steps/test_pipelines.py``. ``bdd:`` now names the dedicated file.
- 2026-08-26: **product-map review pass** — fixed a stale
  coverage claim: the entry was parked at ``status: partial`` with ``bdd: []``
  while ``tests/bdd/features/pipelines/crud.feature`` already exercised
  create-at-run-start, list/pagination, get, tag, rollback (incl. HITL-gate
  weakening denial), delete (+latest refusal), diff, and missing-pipeline 404
  via ``tests/bdd/steps/test_pipelines.py``. Recorded the real BDD references and
  moved the entry to ``status: covered``.
- 2026-08-25: **product-map review pass** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. The entry is referenced by ADR 047/018
  (centralized-authorization cleanup). Re-verified endpoints and CRUD modules against the
  current tree. Status: partial (no BDD coverage).
