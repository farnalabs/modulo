---
id: feat-core-replace-step-agent
prd: §8.4
delivery-tasks: [task-nv11-replace-step-agent]
bdd:
  - backend/tests/bdd/features/pipelines/node_types.feature
  - backend/tests/bdd/features/hitl/manual_node.feature
  - backend/tests/bdd/features/ui/pipeline_builder.feature
  - backend/tests/features/personas/alice-devx-sme.feature
code:
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/db/crud/pipeline.py
  - frontend/src/views/PipelineEditorView.vue
depends-on: [task-nv0-manual-node]
status: partial
---

# Replace Step Agent

Replacing a manual placeholder node with an AI agent node (and reverting from agent back to manual). Powers the SDLC onboarding path where teams model their existing process in Modulo and progressively replace manual steps with AI agents.

## Behaviours

### Manual Node Runtime
- [ ] Manual node compiles successfully in a pipeline graph via build_graph_from_json
- [ ] Manual node raises NodeInterrupt on first invocation with manual=True payload
- [ ] Manual node awaits human input until _hitl_decision is provided on resume
- [ ] Manual node validates human output against output_schema_json required fields
- [ ] Manual node passes any output through when no output_schema_json is set
- [ ] Manual node preserves prior artifacts when processing resume
- [ ] Manual node handles non-dict _hitl_decision output by setting manual_output to None
- [ ] Manual node passes output_schema_id in the interrupt payload for UI rendering
- [ ] Manual node logs completion to artifacts with status "completed"
- [ ] Mixed manual + agent graph compiles and runs successfully
- [ ] Nodes without explicit node_type default to "agent"

### Convert Manual → Agent
- [ ] POST /{pipeline_id}/nodes/{node_id}/convert-to-agent accepts agent_id, connector_binding, model_backend_id
- [ ] Validates agent exists and belongs to the same org
- [ ] Validates connector instance exists and connector_type matches binding
- [ ] Validates model backend exists
- [ ] Sets node_type to "agent", populates agent_id and connector_binding
- [ ] Removes output_schema_id from the node after conversion

### Revert Agent → Manual
- [ ] POST /{pipeline_id}/nodes/{node_id}/revert-to-manual requires snapshot_id query param
- [ ] Validates snapshot exists
- [ ] Validates snapshot contains the node with node_type "manual"
- [ ] Validates snapshot node has an output_schema_id
- [ ] Restores node_type to "manual" and output_schema_id from snapshot
- [ ] Removes agent_id, connector_binding from the node
- [ ] Falls back to snapshot node label when current label is empty

### Frontend — Pipeline Editor
- [ ] Manual nodes rendered with amber border/styling and MANUAL badge
- [ ] Agent nodes rendered with sky border/styling and AGENT badge
- [ ] Node properties panel displays type-specific fields (output_schema for manual; agent_id, connector_binding for agent)
- [ ] "Convert to Agent" button on manual nodes opens agent picker modal
- [ ] Agent picker lists available agents with name and schema info
- [ ] Agent picker shows connector dropdown filtered by eligible connectors
- [ ] Agent picker displays model backend name and input/output schema details
- [ ] "Convert" button disabled until agent and connector are selected
- [ ] Convert calls POST endpoint and refreshes the graph
- [ ] "Revert to Manual" button on agent nodes opens snapshot picker dialog
- [ ] Snapshot picker lists pipeline snapshots with version numbers and tags
- [ ] "Revert" button disabled until a snapshot is selected
- [ ] Revert calls POST endpoint and refreshes the graph
- [ ] Pipeline saves successfully after conversion
- [ ] Pipeline saves successfully after reversion

### Error States
- [ ] 404 when converting on a non-existent pipeline
- [ ] 404 when converting a non-existent node
- [ ] 422 when converting an already-agent node ("Only manual nodes can be converted to agent")
- [ ] 404 when referenced agent not found in org
- [ ] 404 when referenced connector instance not found
- [ ] 422 when connector type does not match binding
- [ ] 404 when referenced model backend not found
- [ ] 404 when reverting on a non-existent pipeline
- [ ] 404 when reverting a non-existent node
- [ ] 422 when reverting an already-manual node ("Only agent nodes can be reverted to manual")
- [ ] 404 when referenced snapshot not found
- [ ] 422 when snapshot does not contain the target node
- [ ] 422 when snapshot node was not of type "manual"
- [ ] 422 when snapshot node has no output_schema_id
- [ ] Manual node raises ValueError when required schema fields are missing in human output
- [ ] Concurrent graph replacement uses row-level locking (SELECT FOR UPDATE)

### Alice Persona Scenarios
- [ ] Create a pipeline with mixed manual and HITL nodes representing an existing SDLC
- [ ] Execute the pipeline with no AI agents configured (all manual)
- [ ] Each manual node produces a log entry when completed
- [ ] Replace a manual QA step with an agent by changing node type, assigning schema, binding connector
- [ ] Pipeline saves and executes the replaced step as an agent on next run
- [ ] Revert a step replacement back to manual when the agent underperforms
- [ ] Restore a previous pipeline snapshot to roll back the replacement entirely

## Known Gaps

- BDD feature files (`node_types.feature`, `manual_node.feature`, `pipeline_builder.feature`) are placeholders with no scenarios
- No BDD step definitions exist for the convert-to-agent or revert-to-manual endpoints
- Persona Gherkin scenarios (alice-devx-sme.feature: `@goal-alice-replace-step`, `@goal-alice-rollback-step`) are tagged @delivered but have no step definitions
- No unit tests for the convert-to-agent and revert-to-manual API endpoints
- Agent picker does not implement the PRD-specified library/org tab split or schema compatibility warning badge
- Team ownership enforcement during node conversion is not tested
