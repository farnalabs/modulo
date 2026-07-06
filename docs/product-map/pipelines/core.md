---
id: feat-pipelines-core
prd: 8.4
delivery-tasks: []  # not yet linked — ~180 behaviours across 11 sub-features
bdd:
  - backend/tests/bdd/features/pipelines/pipeline_config_validation.feature
  - backend/tests/bdd/features/ui/pipeline_builder.feature
code:
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/stages.py
  - backend/src/modulo/db/models/pipeline_edge.py
  - backend/src/modulo/db/models/stage.py
  - backend/src/modulo/db/crud/stage.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - backend/src/modulo/core/graph_validator/category_validator.py
  - frontend/src/views/PipelineEditorView.vue
  - frontend/src/views/StageBoardView.vue
  - frontend/src/views/PipelineListView.vue
  - frontend/src/views/PipelineTemplateGallery.vue
  - frontend/src/views/pipeline/CompositeEditorView.vue
  - frontend/src/components/pipeline/composite/CompositeConfigPanel.vue
  - frontend/src/components/pipeline/composite/CompositeLibraryPicker.vue
  - frontend/src/components/pipeline/composite/FieldMappingPair.vue
  - frontend/src/components/pipeline/composite/OutputValidationTab.vue
  - frontend/src/components/pipeline/composite/ParameterPortForm.vue
  - frontend/src/components/pipeline/composite/PortDefinitionPanel.vue
  - frontend/src/components/pipeline/composite/PublishCompositeFlow.vue
  - frontend/src/components/pipeline/composite/SchemaMappingPanel.vue
  - frontend/src/components/pipeline/nodes/CompositeNode.vue
unit-tests:
  - backend/tests/unit/api/test_stages.py
  - backend/tests/unit/api/test_pipelines_endpoint.py
  - backend/tests/unit/api/test_stage_programming_error.py
  - backend/tests/unit/test_pipeline_node_conversion.py
depends-on: []
status: partial
---

# Pipeline Builder Core

Pipeline Builder UI and data-model components — the visual side of pipelines.
Execution, run lifecycle, event streaming, HITL runtime, and token/cost tracking
are covered by `feat-pipelines-cicd-pipeline`. This entry covers the builder,
canvas, Stage Board, edge model, graph validation (on-save), agent/schema pickers,
copy-to-adapt (save-as-composite), node conversion, and ownership/visibility.

## Behaviours

### Stage Board — Kanban View

- [x] Stage created with name, optional description, position, visibility (org/team), and optional owner_team_id via POST /api/v1/stages — 201
- [x] Stage list returns paginated results sorted by position then name with RLS enforcement
- [x] Stage list filterable by owner_team_id query parameter
- [x] Stage detail retrieved by ID via GET /api/v1/stages/{id} — 200
- [x] Stage fields updated via PATCH /api/v1/stages/{id} (partial update)
- [x] Stage deleted via DELETE /api/v1/stages/{id} — 204
- [x] Stage list filtered by team filter (Frontend computed filter based on owner_team_id)
- [x] Stage list filtered by pipeline status (running, idle, failed, complete, awaiting_human)
- [x] Stage list filtered by date range (from/to)
- [x] Stage details slide-out panel with name, description, position, visibility, connected pipeline count, created date
- [x] Pipeline cards show name, status badge (with colour coding), team name, created date within stage columns
- [x] Pipeline move-left/right buttons advance pipeline between stages via PATCH /api/v1/pipelines/{id} (stage_id update)
- [x] Move buttons disabled while API call is in flight
- [x] Empty stage shows dashed "No pipelines" placeholder
- [x] Pipeline detail slide-out panel with name, status, stage, created date
- [x] Stage column header clickable to show stage details panel
- [x] Create Stage dialog with name, description, position, visibility inputs
- [x] Create Stage validates non-empty name before enabling submit
- [x] Create Stage error displayed inline on failure
- [x] Stage board wrapped in FeatureGate (team_rbac, team tier, show-disabled mode)
- [x] Stage CRUD routes return 501 Not Implemented when stages table does not exist (ProgrammingError caught)
- [x] Stage ownership model: visibility 'team' requires owner_team_id (CHECK constraint), visibility 'org' is default
- [x] Stage CRUD enforces RLS — all queries scoped to organisation_id
- [ ] Stage reorder (drag-and-drop position swap) — not implemented, only move-left/right per pipeline
- [ ] Stage board has no search input for stages — only team/status/date filters on pipelines
- [ ] Stage deletion cascading — no test for what happens to pipelines assigned to a deleted stage

### Agent Picker — Convert Manual to Agent

- [x] Converting a manual node to an agent opens a modal with agent selector, connector picker, model backend display
- [x] Selecting an agent filters eligible connectors by connector_type_id
- [x] Agent picker shows input/output schema names for selected agent
- [x] Convert disabled until agent + connector are selected
- [x] Convert submits POST /api/v1/pipelines/{id}/nodes/{node_id}/convert-to-agent
- [x] Backend validates agent exists in org — 404 if not found
- [x] Backend validates connector instance exists and type matches — 404/422
- [x] Backend validates model backend exists — 404 if not found
- [x] Backend validates node is a manual node before conversion — 422 if not manual
- [x] Backend replaces node_type, adds agent_id, connector_binding, removes output_schema_id
- [x] Backend records audit event (pipeline.node.convert_to_agent)
- [x] Backend catches ProgrammingError and returns 501 Not Implemented
- [x] Convert error shown inline in modal
- [x] Revert to Manual opens modal with snapshot selector (populated from pipeline snapshots)
- [x] Revert restores output_schema_id from snapshot, removes agent_id and connector_binding
- [x] Backend validates snapshot node was manual before reverting — 422 if not
- [x] Backend validates snapshot has output_schema_id — 422 if missing
- [ ] Agent picker does not auto-filter by node_category_id — shows all org agents

### Schema Picker / Agent Config Panel

- [x] Node Properties panel shows ID, type (manual/agent badge), label, output_schema_id (manual), agent_id (agent), connector_binding (agent)
- [x] Edge Properties panel shows source/target node IDs, edge type selector (normal/reject/conditional), condition expression input
- [x] HITL Gate config within Edge Properties: enable checkbox, label, description, claim expiry, human_only
- [x] HITL Gate condition type: None, JMESPath expression, or Eval Reference
- [x] Eval Reference config: eval_name, threshold (0-1), operator (lt/gt/lte/gte/eq/neq)
- [x] Edge properties saved via PATCH /api/v1/pipelines/{id}/graph
- [x] Save Edge button shows saving state and inline error
- [ ] No dedicated Schema Picker dropdown within PipelineEditorView — schemas are derived from the selected agent
- [ ] No frontend validation for edge condition_expression JMESPath syntax

### Pipeline Edge Data Model

- [x] PipelineEdge model with pipeline_id FK, source_node_id, target_node_id, edge_type, hitl_gate_config (JSON), condition_expression
- [x] edge_type constrained to 'normal', 'reject', 'conditional' via CHECK constraint
- [x] UniqueConstraint on (pipeline_id, source_node_id, target_node_id, edge_type)
- [x] PipelineGraphEdge Pydantic model validates edge_type pattern and condition_expression length
- [x] PipelineGraphUpdate validator rejects duplicate edge IDs and duplicate (source, target, type) paths
- [x] Graph replacement uses row-level locking (SELECT ... FOR UPDATE on pipeline row)
- [x] DoS guard: max 500 nodes, max 1000 edges per graph
- [x] Edges support hitl_gate_config with label, description, reject_target, claim_expiry, human_only, required_team_id, condition (JMESPath), eval_condition
- [x] Composite edges stored as part of composite_template parameter_ports — not PipelineEdge rows
- [ ] No standalone edges CRUD endpoint — edges are always replaced as part of full graph

### Manual (Placeholder) Node — UI side (execution side in feat-pipelines-cicd-pipeline)

- [x] Manual nodes created with node_type='manual' via POST /api/v1/pipelines with graph
- [x] Pydantic validation: manual nodes cannot have agent_id, connector_binding; require output_schema_id and label
- [x] Manual node visual rendering: warning-coloured border with "MANUAL" badge, label display
- [x] Manual node shows output_schema_id in properties panel (font-mono truncated UUID)
- [x] Convert-to-agent available for manual nodes in properties panel
- [x] Node type validation on graph save via _resolve_graph_references (manual schema IDs validated)
- [ ] Manual nodes cannot be created from the UI canvas alone — must be included in the initial graph payload or created via revert-to-manual

### Agent Chain — VueFlow Canvas Graph

- [x] Pipeline Editor uses VueFlow for node graph rendering with smoothstep edges
- [x] Canvas shows Background grid, Controls (zoom/fit), node-click, edge-click, pane-click handlers
- [x] Canvas node types: agent (primary-coloured border, "AGENT" badge), manual (warning-coloured)
- [x] Edge labels rendered for HITL gate edges in custom edge template
- [x] Properties panel appears on node click (right sidebar) with ID, type, label, references
- [x] Properties panel appears on edge click with edge type, condition expression, HITL gate config
- [x] Canvas toolbar with "Save as template" dropdown and save-as-composite action
- [x] Backend graph save via PATCH /api/v1/pipelines/{id}/graph runs on-save validation via GraphValidator
- [x] Validation issues returned in PipelineGraphResponse.validation_issues array
- [ ] No undo/redo support for canvas operations
- [ ] No keyboard shortcuts for canvas operations (delete, copy, paste)
- [ ] No minimap component on the canvas
- [ ] Canvas state across drill-down (viewport persistence per level) — untested, no Vue Router state

### CopyToAdaptWizard (Save as Composite)

- [x] Save as Composite dialog with name, description, node selection checkboxes
- [x] Backend POST /api/v1/pipelines/{id}/save-as-composite extracts sub-graph
- [x] Backend auto-detects parameter placeholders ({{parameter.*}}) from selected agents' prompt templates
- [x] Backend creates CompositeTemplate record with sub_pipeline_graph_json, parameter_ports_json
- [x] Auto-detected parameters include name, label, type, required, target_injection config
- [x] Composite template version auto-initialised to "0.1.0"
- [x] Selected nodes + connecting edges extracted as sub-graph
- [x] Composite node validation in GraphValidator: CompositeTemplate exists, required parameters have values, output validation config checked
- [x] Composite validation checks: max_validation_retries (0-5), eval type (regex/json_schema/llm_judge), failure_behaviour (retry/block/warn), regex pattern compilation
- [x] Composite editor components for schema mapping, port definition, parameter ports, etc.
- [x] PublishCompositeFlow dialog for publishing composite templates
- [ ] Copy from community library is a separate flow in feat-pipelines-library (Copy-to-adapt is library-scoped)
- [ ] No dedicated CopyToAdapt wizard for community pipeline templates in PipelineEditorView

### Graph Validation — On-Save Soft Validation

- [x] GraphValidator.validate_definition called on every PATCH /api/v1/pipelines/{id}/graph (on-save)
- [x] Topology check: no cycles, valid edge references, entry node detection, reachability of all nodes
- [x] Nesting depth check: max 3 levels, errors on exceed
- [x] JMESPath expression validation for conditional edges (compiles expression, errors on invalid)
- [x] HITL eval_condition validation: non-empty eval_name, threshold is number in 0.0-1.0, valid operator
- [x] Schema compatibility (shallow): output schema of source matches input schema of target via schema_pins
- [x] Connector binding check: connector instance exists, status=active, required operations present
- [x] Model backend check: backend exists, status=active, no last_health_check_error
- [x] Environment capability check: bound EnvironmentProfile covers all agent required capabilities
- [x] Node category check via validate_node_categories
- [x] Composite node check: CompositeTemplate exists, required parameters have values
- [x] Output validation config check on composite nodes: retries range, eval types, regex patterns, JSON schema
- [x] Validation returns issues as list — warnings (TOPOLOGY_UNREACHABLE) and errors (blocks save)
- [x] Validation issues returned in PipelineGraphResponse.validation_issues
- [x] Deep schema compatibility (field-level) — only used in validate_for_run, not on-save
- [ ] GraphValidator unit tests are thin — test_graph_validator.py exists but covers only HITL gate config validation; topology, connector, model backend, and composite validation have no unit coverage
- [ ] Pre-run validation (validate_for_run) also checks input payload — covered in feat-pipelines-cicd-pipeline run lifecycle

### Real-Time Run Progress (WebSocket Events)

WebSocket event streaming, event types, broker lifecycle, and replay buffer are covered
by `feat-pipelines-cicd-pipeline` (Event Streaming section). This entry notes the
frontend-side integration:

- [ ] PipelineEditorView does not display real-time run progress within the canvas
- [ ] No WebSocket subscription started from PipelineEditorView
- [ ] Stage Board shows pipeline status badges but does not update in real-time via WebSocket
- [ ] Run detail page (frontend/src/views/RunDetailView.vue) uses WebSocket events — covered separately

### Agent Theme (V1) — ?mode=agent Route

- [ ] No `?mode=agent` route parameter handling found in PipelineEditorView or frontend routing
- [ ] No GET /api/v1/viewmodel/current endpoint found in routes

### Resource Ownership on Creation

- [x] Stage model has owner_team_id FK to teams.id (ondelete=RESTRICT), visibility field
- [x] Stage create accepts owner_team_id, visibility parameters
- [x] Stage CHECK constraint: visibility 'team' requires owner_team_id IS NOT NULL
- [x] Stage list filterable by owner_team_id
- [x] Pipeline create accepts visibility field (org/team pattern)
- [x] Pipeline Pydantic model validates visibility with regex pattern
- [x] Stage Board team filter uses owner_team_id to filter stages and pipelines
- [ ] Pipeline model does not have owner_team_id field — only visibility
- [ ] No ownership picker UI component for pipeline creation — only for stage creation

## Edge Cases

- [x] Empty stage list returns total=0 with empty items array
- [x] Non-existent stage ID returns 404 on get/update/delete
- [x] Stage create with empty name returns 422
- [x] Stage create with missing name returns 422
- [x] Stage create with invalid visibility value returns 422
- [x] Stage update with empty name returns 422
- [x] Unauthenticated stage requests return 401/403
- [x] Pipeline graph with no nodes — GraphValidator returns TOPOLOGY_NO_NODES error
- [x] Pipeline graph with cycle — GraphValidator detects and returns TOPOLOGY_CYCLE error
- [x] Graph exceeding max nodes (500) or max edges (1000) — rejected with 422
- [x] Graph with duplicate node IDs — rejected with 422
- [x] Graph with duplicate edge paths — rejected with 422
- [x] Non-existent agent in graph — 422 with unknown agent IDs
- [x] Non-existent output schema in graph — 422 with unknown schema IDs
- [x] Manual node output_schema_id that doesn't exist in org — 422
- [x] Missing snapshot in revert-to-manual — 404
- [x] Missing DB table for stages — 501 Not Implemented

## Error Handling

- [x] Stage CRUD routes catch ProgrammingError → 501
- [x] Stage CRUD routes catch SQLAlchemyError → 503
- [x] Pipeline CRUD routes catch ProgrammingError → 501
- [x] Pipeline CRUD routes catch SQLAlchemyError → 503
- [x] Pipeline clone route catches ProgrammingError → 501 and SQLAlchemyError → 503
- [x] Node conversion routes catch ProgrammingError → 501
- [x] Node conversion routes catch SQLAlchemyError → 503
- [x] Node conversion routes catch IntegrityError → 409 (separate from ProgrammingError)
- [x] Pipeline graph routes catch ProgrammingError where applicable
- [x] Save-as-composite route moved all DB queries inside session.begin() — RLS leak fixed
- [x] GraphValidator errors propagate with specific error codes (TOPOLOGY_*, SCHEMA_*, CONNECTOR_*, MODEL_BACKEND_*, ENV_*, COMPOSITE_*)
- [x] Graph validation errors surfaced in PipelineGraphResponse.validation_issues — returned to frontend, not lost
- [x] 10 new unit tests covering ProgrammingError→501 and SQLAlchemyError→503 for all 5 stage routes
- [ ] Stage DELETE with pipelines still assigned — no test for FK constraint behaviour
- [ ] Stage create with non-existent owner_team_id — no explicit test (FK constraint at DB level)

## Known Gaps

### GraphValidator unit tests exist but are thin
`backend/tests/unit/graph_validator/test_graph_validator.py` exists with coverage for HITL gate config validation but no unit tests for topology, connector, model backend, or composite validation logic. The GraphValidator (~876 lines) relies on BDD `pipeline_config_validation.feature` (4 scenarios) and endpoint-level tests.

### Canvas features missing
- Undo/redo support for node/edge operations
- Keyboard shortcuts (delete selected node, copy/paste)
- Minimap component
- Viewport persistence per drill-down level
- Real-time run progress display within canvas

### Agent Theme V1 unimplemented
- No `?mode=agent` route mode in frontend
- No `GET /api/v1/viewmodel/current` endpoint

### CopyToAdaptWizard scope limited
- Save as Composite is the only CopyToAdapt-like flow in PipelineEditorView
- Copying community pipeline templates from the Library is handled by feat-pipelines-library

### Missing website docs
- No pipeline-builder page at `Website/modulo-website/src/docs/` — needs separate website worktree
- No docs stubs for Pipeline Builder, Stage Board, or Graph Validation features

### Stage Board limitations
- No drag-and-drop stage reordering
- No search/filter input for stage names (only team filter on stages)
- No pipeline search within stage columns
- No test for stage deletion with assigned pipelines

### No BDD for stages
- No BDD feature file covers Stage CRUD scenarios
- pipeline_builder.feature has only 5 UI scenarios — does not cover agent picker, edge config, HITL gate config, save-as-composite, or stage board
- No BDD for graph validation (pipeline_config_validation.feature has only 4 scenarios — no schema compatibility, connector, model backend, or composite validation scenarios)

### Ownership picker incomplete
- Pipeline model lacks owner_team_id — only Stage has it
- No dedicated ownership picker component in UI — only a visibility selector on stage creation dialog

## QA History

- 2026-07-08: Cross-cutting QA (index 254): Fixed CRITICAL — RLS leak in save_as_composite_endpoint (3 DB queries outside session.begin() — Agent lookup, PipelineEdge fetch, create_composite_template — missing RLS context on Postgres; all moved inside transaction). Fixed CRITICAL — added SQLAlchemyError→503 catches to all 5 stage routes (previously only ProgrammingError→501). Fixed CRITICAL — added SQLAlchemyError→503 catches to 8 pipeline CRUD + clone routes. Fixed CRITICAL — combined `except (IntegrityError, ProgrammingError, SQLAlchemyError)` in convert-to-agent/revert-to-manual split into separate handlers with correct status codes (409/501/503). Fixed MAJOR — added `populate_by_name=True` to StageResponse. Fixed MAJOR — replaced 10 `e instanceof Error ? e.message : String(e)` handlers with `formatApiError(e)` in 4 frontend views (PipelineEditorView, StageBoardView, PipelineListView, PipelineTemplateGallery). Created backend/tests/unit/api/test_stage_programming_error.py with 10 tests covering all 5 stage routes × 2 error types. Fixed 2 pre-existing test failures (license tier assertion, AsyncMock for publish_primitive). Merged to main at v0.3.227. Status: partial.
- 2026-07-05: Prodmap pipelines QA: Fixed depends-on direction (core → cicd was inverted). Fixed false Known Gap about missing `test_graph_validator.py`. Fixed website docs path prefix. Updated delivery-tasks note.

