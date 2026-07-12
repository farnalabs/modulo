---
id: feat-core-rollback-agent-replacement
prd: 8.4
delivery-tasks: [task-nv11-rollback-agent-replacement]
bdd:
  - backend/tests/bdd/features/pipelines/node_types.feature
  - backend/tests/bdd/features/hitl/manual_node.feature
  - backend/tests/bdd/features/personas/alice-devx-sme.feature
code:
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/db/crud/pipeline.py
  - frontend/src/views/PipelineEditorView.vue
depends-on: [feat-core-replace-step-agent]
unit-tests: [backend/tests/unit/test_pipeline_node_conversion.py]
status: partial
---

# Rollback Agent Replacement

Reverting an agent node back to a manual node, using a pipeline snapshot to restore the pre-replacement configuration. Complements `feat-core-replace-step-agent` (which covers manual → agent conversion) by providing the revert direction — the safety valve when an AI agent underperforms and the team needs to fall back to the manual process.

## Behaviours

### Revert API — Backend

- [x] POST /{pipeline_id}/nodes/{node_id}/revert-to-manual accepts snapshot_id query param
- [x] 404 when pipeline does not exist
- [x] 404 when node does not exist
- [x] 422 when node is not type "agent" ("Only agent nodes can be reverted to manual")
- [x] 404 when referenced snapshot does not exist
- [x] 422 when snapshot does not contain the target node
- [x] 422 when snapshot node was not type "manual" ("Snapshot node was not a manual node")
- [x] 422 when snapshot node has no output_schema_id
- [x] Restores node_type to "manual" on success
- [x] Restores output_schema_id from snapshot on success
- [x] Removes agent_id from the node on revert
- [x] Removes connector_binding from the node on revert
- [x] Falls back to snapshot node label when current label is empty
- [x] Persists change via replace_pipeline_graph (full graph replacement)
- [x] Runs within a database transaction with RLS context

### Revert UI — Frontend

- [x] "Revert to Manual" button displayed on selected agent nodes
- [x] Clicking opens a snapshot picker modal dialog
- [x] Modal lists pipeline snapshots with version number and tag
- [x] Snapshots filtered to exclude version 0 (initial state)
- [x] "Revert" button disabled until a snapshot is selected
- [x] Loading spinner shown during the revert request
- [x] Error message displayed on failure in a red alert box
- [x] Modal closes and graph refreshes on successful revert
- [x] Cancelling the modal does not change pipeline state

### Run-time Behaviour After Revert

- [x] Reverted manual node pauses execution at run time (NodeInterrupt)
- [x] Human can provide output via HITL review UI
- [x] Output is validated against restored output_schema_id
- [x] Pipeline continues after manual output is provided
- [x] Mixed graph of reverted manual + other agent nodes compiles and runs

### Snapshot Interactions

- [ ] Revert-to-manual creates a new snapshot of the reverted graph state
- [ ] Full pipeline snapshot rollback (POST /rollback) can undo the revert entirely
- [ ] Snapshot diff shows before/after of the converted node

### Alice Persona Scenario (@goal-alice-rollback-step)

- [x] Given an agent node, revert it back to manual type
- [x] Pipeline saves successfully after reversion
- [ ] Can restore a previous pipeline snapshot to undo the revert entirely
- [ ] After snapshot restore, pipeline matches state before agent was added

## Error Handling

- [x] Route wraps DB operations in `try/except (IntegrityError, ProgrammingError, SQLAlchemyError)` returning 501
- [x] `replace_pipeline_graph` called within the transaction scope so errors propagate to the route-level handler
- [x] Frontend shows error message in a styled red alert box on failure
- [x] Non-existent pipeline/node returns 404 with descriptive message
- [x] Invalid node type (non-agent) returns 422 with specific detail ("Only agent nodes can be reverted to manual")
- [x] Missing snapshot returns 404
- [x] Snapshot without target node returns 422 with specific detail
- [x] Non-manual snapshot node returns 422 with specific detail
- [x] Snapshot node without output schema returns 422 with specific detail
- [x] Frontend error display uses `formatApiError(e)` for structured error extraction — verified at PipelineEditorView.vue:864
- [x] Frontend uses `$t()` for all revert dialog strings — title, snapshot label, placeholder, cancel, revert, error box, description

## Resilience

- [x] Transaction boundary protects atomicity — revert either fully commits or fully rolls back
- [x] Row-level lock (`SELECT ... FOR UPDATE`) prevents concurrent modifications during revert
- [x] RLS context established within the transaction before any DB operations
- [x] Full graph replacement (`replace_pipeline_graph`) atomically swaps nodes + edges
- [ ] No advisory lock for multi-worker contention on the same pipeline (same pattern as the replace endpoint)
- [ ] No idempotency check — multiple reverts of the same node produce no error (no-op after first revert since `node_type` is already "manual")
- [ ] Revert does not create a new snapshot of the reverted state — cannot undo a revert via snapshot rollback

## Edge Cases

- [x] Agent node with empty label falls back to snapshot node label or "Manual {id}"
- [x] Snapshot `output_schema_id` may be a string or UUID — code handles both with `str()`
- [x] Node `id` in snapshot may be UUID or string — `_find_node_in_list` handles both
- [x] Pipeline with no graph_nodes_json or edges — treated as empty lists
- [x] Reverting an already-manual node returns 422 (proper validation)
- [ ] Multiple concurrent revert requests on the same node — no concurrency test
- [ ] Large pipeline graph (1000+ nodes) — no performance testing
- [ ] Snapshot with multiple manual nodes — only the target node is restored
- [ ] Edge between reverted node and downstream nodes — preserved as-is, no validation of compatibility post-revert

## Known Gaps
- BDD feature files (node_types.feature, manual_node.feature) lack revert-to-manual scenarios
- `test_revert_to_manual_steps.py` exists but tests a PATCH /graph endpoint, not the real revert-to-manual POST endpoint
- Persona scenario `@goal-alice-rollback-step` is tagged @delivered but steps use mocked endpoints, not real revert-to-manual
- No integration tests verifying snapshot-based restore of a reverted node
- Frontend snapshot picker does not show snapshot creation date or diff preview
- No visual diff between current agent config and selected snapshot's manual config
- Revert confirmation has no "are you sure?" step before execution
- Revert-to-manual does not create a new snapshot (the code updates the graph in-place but does not create a PipelineSnapshot)
- Website docs: no page exists for rollback-agent-replacement feature

## QA History

### 2026-07-10 — Cross-cutting QA (index 302)

**Fixes applied:**
- MAJOR — Fixed 2 stale product map Error Handling claims: Frontend already uses `formatApiError(e)` (PipelineEditorView.vue:864) and `$t()` for all revert dialog strings. Changed both `[ ]` to `[x]`.
- MAJOR — Added 4 error path unit tests in `TestRevertToManual`: `test_integrity_error_returns_409`, `test_programming_error_returns_501`, `test_sqlalchemy_error_returns_503`, `test_unexpected_exception_returns_500`. Product map known gap "No unit tests for the revert-to-manual API endpoint" now resolved.
- MINOR — Updated known gaps: marked i18n and unit test gaps as `[x]` (resolved).

**Unchanged gaps:** BDD steps still mock PATCH /graph instead of real POST /revert-to-manual endpoint; snapshot interactions (no snapshot created on revert, no diff, no undo-by-rollback) are genuine feature gaps outside QA scope.

**Status:** partial (3 snapshot interaction gaps, 1 BDD endpoint mismatch gap, 4 remaining known gaps)
