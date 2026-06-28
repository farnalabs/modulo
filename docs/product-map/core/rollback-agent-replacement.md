---
id: feat-core-rollback-agent-replacement
prd: 8.4
delivery-tasks: [task-nv11-rollback-agent-replacement]
  - backend/tests/bdd/features/pipelines/node_types.feature
  - backend/tests/bdd/features/hitl/manual_node.feature
  - backend/tests/features/personas/alice-devx-sme.feature
code:
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/db/crud/pipeline.py
  - frontend/src/views/PipelineEditorView.vue
depends-on: [feat-core-replace-step-agent]
status: partial
---
# Rollback Agent Replacement Reverting an agent node back to a manual  node, using a pipeline snapshot to restore the pre-replacement configuration. Complements `feat-core-replace-step-agent` (which covers manual → agent conversion) by providing the revert direction — the safety valve when an AI agent underperforms and the team needs to fall back to the manual process. ## Behaviours ### Revert API — Backend
- [ ] POST /{pipeline_id}/nodes/{node_id}/revert-to-manual accepts snapshot_id query param
- [ ] 404 when pipeline does not exist
- [ ] 404 when node does not exist
- [ ] 422 when node is not type "agent" ("Only agent nodes can be reverted to manual")
- [ ] 404 when referenced snapshot does not exist
- [ ] 422 when snapshot does not contain the target node
- [ ] 422 when snapshot node was not type "manual" ("Snapshot node was not a manual node")
- [ ] 422 when snapshot node has no output_schema_id
- [ ] Restores node_type to "manual" on success
- [ ] Restores output_schema_id from snapshot on success
- [ ] Removes agent_id from the node on revert
- [ ] Removes connector_binding from the node on revert
- [ ] Falls back to snapshot node label when current label is empty
- [ ] Persists change via replace_pipeline_graph (full graph replacement)
- [ ] Runs within a database transaction with RLS context ### Revert UI — Frontend
- [ ] "Revert to Manual" button displayed on selected agent nodes
- [ ] Clicking opens a snapshot picker modal dialog
- [ ] Modal lists pipeline snapshots with version number and tag
- [ ] Snapshots filtered to exclude version 0 (initial state)
- [ ] "Revert" button disabled until a snapshot is selected
- [ ] Loading spinner shown during the revert request
- [ ] Error message displayed on failure in a red alert box
- [ ] Modal closes and graph refreshes on successful revert
- [ ] Cancelling the modal does not change pipeline state ### Run-time Behaviour After Revert
- [ ] Reverted manual node pauses execution at run time (NodeInterrupt)
- [ ] Human can provide output via HITL review UI
- [ ] Output is validated against restored output_schema_id
- [ ] Pipeline continues after manual output is provided
- [ ] Mixed graph of reverted manual + other agent nodes compiles and runs ### Snapshot Interactions
- [ ] Revert-to-manual creates a new snapshot of the reverted graph state
- [ ] Full pipeline snapshot rollback (POST /rollback) can undo the revert entirely
- [ ] Snapshot diff shows before/after of the converted node ### Alice Persona Scenario (@goal-alice-rollback-step)
- [ ] Given an agent node, revert it back to manual type
- [ ] Pipeline saves successfully after reversion
- [ ] Can restore a previous pipeline snapshot to undo the revert entirely
- [ ] After snapshot restore, pipeline matches state before agent was added ## Known Gaps - BDD feature files (node_types.feature, manual_node.feature) are placeholders with no scenarios
- No BDD step definitions exist for the revert-to-manual endpoint
- Persona scenario `@goal-alice-rollback-step` is tagged @delivered but has no step definitions
- No unit tests for the revert-to-manual API endpoint
- No integration tests verifying snapshot-based restore of a reverted node
- Frontend snapshot picker does not show snapshot creation date or diff preview
- No visual diff between current agent config and selected snapshot's manual config
- Revert confirmation has no "are you sure?" step before execution 