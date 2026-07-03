---
id: feat-core-replace-step-agent
prd: 8.4
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
depends-on: [feat-core-pipeline-execution]
unit-tests: [backend/tests/unit/test_pipeline_node_conversion.py]
status: partial
---

# Replace Step Agent

Replacing a manual placeholder node with an AI agent node (and reverting from agent back to manual). Powers the SDLC onboarding path where teams model their existing process in Modulo and progressively replace manual steps with AI agents.

## Behaviours

### Manual Node Runtime

- [x] Manual node compiles successfully in a pipeline graph via build_graph_from_json
- [x] Manual node raises NodeInterrupt on first invocation with manual=True payload
- [x] Manual node awaits human input until _hitl_decision is provided on resume
- [x] Manual node validates human output against output_schema_json required fields
- [x] Manual node passes any output through when no output_schema_json is set
- [x] Manual node preserves prior artifacts when processing resume
- [x] Manual node handles non-dict _hitl_decision output by setting manual_output to None
- [x] Manual node passes output_schema_id in the interrupt payload for UI rendering
- [x] Manual node logs completion to artifacts with status "completed"
- [x] Mixed manual + agent graph compiles and runs successfully
- [x] Nodes without explicit node_type default to "agent"

### Convert Manual → Agent

- [x] POST /{pipeline_id}/nodes/{node_id}/convert-to-agent accepts agent_id, connector_binding, model_backend_id
- [x] Validates agent exists and belongs to the same org
- [x] Validates connector instance exists and connector_type matches binding
- [x] Validates model backend exists
- [x] Sets node_type to "agent", populates agent_id and connector_binding
- [x] Removes output_schema_id from the node after conversion

### Revert Agent → Manual

- [x] POST /{pipeline_id}/nodes/{node_id}/revert-to-manual requires snapshot_id query param
- [x] Validates snapshot exists
- [x] Validates snapshot contains the node with node_type "manual"
- [x] Validates snapshot node has an output_schema_id
- [x] Restores node_type to "manual" and output_schema_id from snapshot
- [x] Removes agent_id, connector_binding from the node
- [x] Falls back to snapshot node label when current label is empty

### Frontend — Pipeline Editor

- [x] Manual nodes rendered with amber border/styling and MANUAL badge
- [x] Agent nodes rendered with sky border/styling and AGENT badge
- [x] Node properties panel displays type-specific fields (output_schema for manual; agent_id, connector_binding for agent)
- [x] "Convert to Agent" button on manual nodes opens agent picker modal
- [x] Agent picker lists available agents with name and schema info
- [x] Agent picker shows connector dropdown filtered by eligible connectors
- [x] Agent picker displays model backend name and input/output schema details
- [x] "Convert" button disabled until agent and connector are selected
- [x] Convert calls POST endpoint and refreshes the graph
- [x] "Revert to Manual" button on agent nodes opens snapshot picker dialog
- [x] Snapshot picker lists pipeline snapshots with version numbers and tags
- [x] "Revert" button disabled until a snapshot is selected
- [x] Revert calls POST endpoint and refreshes the graph
- [x] Pipeline saves successfully after conversion
- [x] Pipeline saves successfully after reversion

### Error States

- [x] 404 when converting on a non-existent pipeline
- [x] 404 when converting a non-existent node
- [x] 422 when converting an already-agent node ("Only manual nodes can be converted to agent")
- [x] 404 when referenced agent not found in org
- [x] 404 when referenced connector instance not found
- [x] 422 when connector type does not match binding
- [x] 404 when referenced model backend not found
- [x] 404 when reverting on a non-existent pipeline
- [x] 404 when reverting a non-existent node
- [x] 422 when reverting an already-manual node ("Only agent nodes can be reverted to manual")
- [x] 404 when referenced snapshot not found
- [x] 422 when snapshot does not contain the target node
- [x] 422 when snapshot node was not of type "manual"
- [x] 422 when snapshot node has no output_schema_id
- [x] Manual node raises ValueError when required schema fields are missing in human output
- [x] Concurrent graph replacement uses row-level locking (SELECT FOR UPDATE)
- [x] convert-to-agent catches ProgrammingError and returns 501 Not Implemented
- [ ] convert-to-agent _save_graph returns None → 404 (race condition pipeline deleted between get and save)
- [x] revert-to-manual catches ProgrammingError and returns 501 Not Implemented
- [ ] revert-to-manual _save_graph returns None → 404 (race condition pipeline deleted between get and save)
- [x] convert-to-agent dispatches pipeline.node.convert_to_agent audit event

### Alice Persona Scenarios

- [x] Create a pipeline with mixed manual and HITL nodes representing an existing SDLC
- [x] Execute the pipeline with no AI agents configured (all manual)
- [x] Each manual node produces a log entry when completed
- [x] Replace a manual QA step with an agent by changing node type, assigning schema, binding connector
- [x] Pipeline saves and executes the replaced step as an agent on next run
- [x] Revert a step replacement back to manual when the agent underperforms
- [x] Restore a previous pipeline snapshot to roll back the replacement entirely

### QA History

- 2026-07-01: Added ProgrammingError catch to convert-to-agent (501 Not Implemented), added audit event dispatch (pipeline.node.convert_to_agent), fixed append_audit_event parameter names in both convert and revert endpoints (org_id/actor_user_id/payload_json). Created BDD step definitions for @goal-alice-replace-step (convert-to-agent) and all 4 node_types.feature scenarios. Created 17 unit tests covering all convert-to-agent and revert-to-manual error paths (pipeline not found, node not found, wrong type, agent/connector/model backend/snapshot not found, connector mismatch, snapshot constraints, ProgrammingError). 17/17 unit tests pass.
- 2026-07-03: Cross-cutting QA (index 93). Fixed frontmatter unit-tests ref (was empty, now points to test_pipeline_node_conversion.py). Added missing error state checkboxes: revert-to-manual ProgrammingError → 501, both endpoints _save_graph returns None → 404 (race condition). Confirmed 17 unit tests exist on disk. All 3 known gaps remain open.

## Known Gaps
- Agent picker does not implement the PRD-specified library/org tab split or schema compatibility warning badge
- Team ownership enforcement during node conversion is not tested
- No BDD step definitions for pipeline_builder.feature (5 UI scenarios)
