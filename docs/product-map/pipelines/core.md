---
id: feat-pipelines-core
prd: 8.4
delivery-tasks: []  # not yet linked — ~180 behaviours across 11 sub-features
bdd:
  - backend/tests/bdd/features/pipelines/pipeline_config_validation.feature
  - backend/tests/bdd/features/pipelines/create.feature
  - backend/tests/bdd/features/ui/pipeline_builder.feature
code:
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/pipeline_folders.py
  - backend/src/modulo/db/models/pipeline_folder.py
  - backend/src/modulo/db/models/pipeline_edge.py
  - backend/src/modulo/db/crud/pipeline_folder.py
  - backend/src/modulo/db/migrations/versions/0110_schema_pipeline_runtime.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - backend/src/modulo/core/graph_validator/category_validator.py
  - frontend/src/views/PipelineEditorView.vue
  - frontend/src/views/PipelineListView.vue
  - frontend/src/components/pipelines/FolderTree.vue
  # frontend/src/views/PipelineTemplateGallery.vue — removed, merged into PipelineListView
  - frontend/src/views/pipeline/CompositeEditorView.vue
  # frontend/src/components/pipeline/composite/CompositeConfigPanel.vue — removed, dead code
  # frontend/src/components/pipeline/composite/CompositeLibraryPicker.vue — removed, dead code
  - frontend/src/components/pipeline/composite/FieldMappingPair.vue
  - frontend/src/components/pipeline/composite/OutputValidationTab.vue
  - frontend/src/components/pipeline/composite/ParameterPortForm.vue
  - frontend/src/components/pipeline/composite/PortDefinitionPanel.vue
  - frontend/src/components/pipeline/composite/PublishCompositeFlow.vue
  - frontend/src/components/pipeline/composite/SchemaMappingPanel.vue
  - frontend/src/components/pipeline/nodes/CompositeNode.vue
unit-tests:
  - backend/tests/unit/api/test_pipelines_endpoint.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/test_pipeline_node_conversion.py
  - backend/tests/unit/graph_validator/test_graph_validator.py
  - backend/tests/unit/graph_validator/test_category_validator.py
  - backend/tests/bdd/steps/test_alpha_pipelines.py
  - frontend/src/__tests__/PipelineListView.spec.ts
depends-on:
  - feat-core-pipeline-execution
  - feat-pipelines-cicd-pipeline
  - feat-core-agent-model
status: partial
---

# Pipeline Builder Core

Pipeline Builder UI and data-model components — the visual side of pipelines.
Execution, run lifecycle, event streaming, HITL runtime, and token/cost tracking
are covered by `feat-pipelines-cicd-pipeline`. This entry covers the builder,
canvas, edge model, graph validation (on-save), agent/schema pickers,
copy-to-adapt (save-as-composite), node conversion, and ownership/visibility.

## Behaviours

### Pipeline Folders

- [x] Pipeline folders are organisation-scoped records with name, optional parent folder, sort order, and creator
- [x] Folder REST API supports list, create, partial update, delete, and sort-order updates under `/api/v1/pipeline-folders`
- [x] Folder endpoints set organisation and user RLS context before accessing data
- [x] Folder names are required and limited to 255 characters; sort order is non-negative
- [x] Pipeline list supports filtering by `folder_id`
- [x] Pipelines can be moved into a folder or returned to the unfiled list via `PATCH /api/v1/pipelines/{pipeline_id}/folder`
- [x] Moving a pipeline validates that the target folder exists in the active organisation
- [x] Deleting a folder preserves its pipelines by clearing their `folder_id` and promotes direct child folders to the top level
- [x] Folder list UI renders a nested tree and supports create, rename, delete, selection, and move-to-folder workflows
- [x] Pipeline editor renders folder breadcrumbs and links back to the filtered pipeline list
- [x] Folder routes return 501 Not Implemented when the folder migration has not been applied
- [x] Folder CRUD, move, and cycle-rejection endpoints have dedicated backend unit coverage (`TestPipelineFolderEndpoints` + `TestPipelineFolderCyclePrevention` in `test_pipelines_endpoint.py`)
- [x] Folder parent updates reject self-parenting and ancestry cycles via the shared folder-tree validator (`folder_tree.py`, used by `pipeline_folder.py`) — invalid parent assignments return 422

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
- [x] GraphValidator unit tests expanded since index 254 — test_graph_validator.py now covers topology (nodes, cycles, reachability), schema compatibility, connector bindings, model backend health, nesting depth, kickback edges, deep schema compatibility, input payload validation, and conditional edges (52 test functions)
- [x] Category validator unit tests added — test_category_validator.py covers node category validation (12 test functions)
- [x] Composite validation has dedicated unit tests — `TestCompositeValidation` in `test_pipelines_endpoint.py` covers sub-graph structure (`COMPOSITE_SUBGRAPH_*`), duplicate ids, HITL-gate-on-sub-edge rejection, and output-validation config (`COMPOSITE_VALIDATION_*` retries range / eval type / regex compile)
- [x] Pre-run validation (validate_for_run) also checks input payload against the entry node's input schema — covered by the `test_graph_validator.py` input-payload tests (match / missing field / type mismatch) and the run-lifecycle entry

### Real-Time Run Progress (WebSocket Events)

WebSocket event streaming, event types, broker lifecycle, and replay buffer are covered
by `feat-pipelines-cicd-pipeline` (Event Streaming section). This entry notes the
frontend-side integration:

- [ ] PipelineEditorView does not display real-time run progress within the canvas
- [ ] No WebSocket subscription started from PipelineEditorView
- [ ] Run detail page (frontend/src/views/RunDetailView.vue) uses WebSocket events — covered separately

### Agent Theme (V1) — ?mode=agent Route

- [ ] No `?mode=agent` route parameter handling found in PipelineEditorView or frontend routing
- [ ] No GET /api/v1/viewmodel/current endpoint found in routes

### Resource Ownership on Creation

- [x] Pipeline create accepts visibility field (org/team pattern)
- [x] Pipeline Pydantic model validates visibility with regex pattern
- [ ] Pipeline model does not have owner_team_id field — only visibility

## Edge Cases

- [x] Pipeline graph with no nodes — GraphValidator returns TOPOLOGY_NO_NODES error
- [x] Pipeline graph with cycle — GraphValidator detects and returns TOPOLOGY_CYCLE error
- [x] Graph exceeding max nodes (500) or max edges (1000) — rejected with 422
- [x] Graph with duplicate node IDs — rejected with 422
- [x] Graph with duplicate edge paths — rejected with 422
- [x] Non-existent agent in graph — 422 with unknown agent IDs
- [x] Non-existent output schema in graph — 422 with unknown schema IDs
- [x] Manual node output_schema_id that doesn't exist in org — 422
- [x] Missing snapshot in revert-to-manual — 404

## Error Handling

- [x] Pipeline CRUD routes catch ProgrammingError → 501
- [x] Pipeline CRUD routes catch SQLAlchemyError → 503
- [x] Pipeline clone route catches ProgrammingError → 501 and SQLAlchemyError → 503 (via @handle_db_errors decorator)
- [x] Node conversion routes catch ProgrammingError → 501
- [x] Node conversion routes catch SQLAlchemyError → 503
- [x] Node conversion routes catch IntegrityError → 409 (separate from ProgrammingError)
- [x] Pipeline graph routes catch ProgrammingError where applicable (via @handle_db_errors decorator — also covers SQLAlchemyError, IntegrityError, ValidationError, and generic Exception)
- [x] Save-as-composite route moved all DB queries inside session.begin() — RLS leak fixed
- [x] GraphValidator errors propagate with specific error codes (TOPOLOGY_*, SCHEMA_*, CONNECTOR_*, MODEL_BACKEND_*, ENV_*, COMPOSITE_*)
- [x] Graph validation errors surfaced in PipelineGraphResponse.validation_issues — returned to frontend, not lost

## Known Gaps

### GraphValidator unit tests expanded (index 338)
`backend/tests/unit/graph_validator/test_graph_validator.py` now has 52 test functions covering topology (nodes, cycles, reachability, nesting depth, kickback), schema compatibility (shallow + deep field-level), connector bindings (active, inactive, NotFound, missing operations), model backend health (active, inactive, unhealthy, empty pins), conditional edge expressions, input payload validation, and early short-circuit on topology errors. `test_category_validator.py` adds 12 tests for node category validation. The GraphValidator (~817 lines) also relies on BDD `pipeline_config_validation.feature` (4 scenarios), endpoint-level tests, and — since 2026-08-15 — the `TestCompositeValidation` unit class in `test_pipelines_endpoint.py` covering composite sub-graph structure and output-validation config (previously the only composite validation gap).

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
- No docs stubs for Pipeline Builder or Graph Validation features

### Frontend pipeline views not fully i18n'd
- PipelineEditorView.vue (~825 lines) has 23 `$t()` calls but ~30 hardcoded English strings in templates (MANUAL, AGENT, HITL labels, edge properties, HITL gate config, Save as Composite dialog, schema info)
- CompositeEditorView.vue and all 9 composite component files have 0 `$t()` calls — fully hardcoded English
- PipelineListView.vue and PipelineEditorView.vue correctly use `formatApiError(e)` for error handlers

### Stale product map code reference
- `PipelineTemplateGallery.vue` referenced in frontmatter `code:` but the file no longer exists — was merged into PipelineListView

### No BDD for pipeline builder UI
- pipeline_builder.feature has only 5 UI scenarios — does not cover agent picker, edge config, HITL gate config, or save-as-composite
- No BDD for graph validation (pipeline_config_validation.feature has only 4 scenarios — no schema compatibility, connector, model backend, or composite validation scenarios)

### Ownership picker incomplete
- Pipeline model lacks owner_team_id
- No dedicated ownership picker component in UI

## QA History

- 2026-08-15 (distribute partial-model-backends, round 3): Audit pass. All remaining unchecked behaviours are frontend/route gaps in the pipeline editor and its supporting API (agent-picker auto-filter by `node_category_id`, no dedicated schema picker, no JMESPath edge-condition validation, no standalone edges CRUD, no manual-node creation from canvas alone, no undo/redo/keyboard-shortcuts/minimap, no viewport persistence across drill-down, copy-to-adapt/library separation, no real-time run progress in canvas, no WebSocket subscription from editor, no `?mode=agent` / `GET /api/v1/viewmodel/current`, no `owner_team_id` on the pipeline model). None are affected by this session's model-backend deletion-protection change; they require frontend or route changes outside this delivery's allowlist. No tests deleted or disabled. Status: partial.

- 2026-08-15 (distribute): Drove from partial toward covered. IMPLEMENTED — folder parent updates now reject self-parenting and ancestry cycles (shared folder-tree validator in `folder_tree.py`, enforced on `PATCH /api/v1/pipeline-folders/{id}` with a 422 response; create/update folder routes now map `ValueError` → 422 instead of 500). ADDED COVERAGE — `TestPipelineFolderEndpoints` + `TestPipelineFolderCyclePrevention` (folder CRUD, reorder, move-pipeline-to-folder incl. missing-folder 422 and pipeline 404, self-parent 422, cycle 422, depth-overflow 422) and `TestCompositeValidation` (composite sub-graph structure + output-validation config checks) in `test_pipelines_endpoint.py`. VERIFIED — pre-run input payload validation (`validate_for_run` → `_check_input_schema_compatibility`) is implemented and tested in `test_graph_validator.py`. Marked folder CRUD/move coverage, folder cycle rejection, composite unit tests, and pre-run input payload `[x]`. Known Gaps unchanged for canvas features (undo/redo, minimap, keyboard shortcuts), agent theme, schema picker dropdown, standalone edges CRUD, manual-node canvas creation, and owner_team_id (all noted below).
- 2026-08-14 (improve-architecture): Linked the already-wired `backend/tests/bdd/features/pipelines/create.feature` (5 executable scenarios: minimal pipeline, LLM/manual nodes, run_context defaults, duplicate name rejection) to the `feat-pipelines-core` `bdd:` field and added its wiring step file `backend/tests/bdd/steps/test_alpha_pipelines.py` to `unit-tests:`. The feature file was executable on disk but never listed in frontmatter.
- 2026-07-08: Cross-cutting QA (index 254): Fixed CRITICAL — RLS leak in save_as_composite_endpoint (3 DB queries outside session.begin() — Agent lookup, PipelineEdge fetch, create_composite_template — missing RLS context on Postgres; all moved inside transaction). Fixed CRITICAL — added SQLAlchemyError→503 catches to 8 pipeline CRUD + clone routes. Fixed CRITICAL — combined `except (IntegrityError, ProgrammingError, SQLAlchemyError)` in convert-to-agent/revert-to-manual split into separate handlers with correct status codes (409/501/503). Fixed MAJOR — replaced 10 `e instanceof Error ? e.message : String(e)` handlers with `formatApiError(e)` in 4 frontend views (PipelineEditorView, PipelineListView, PipelineTemplateGallery). Fixed 2 pre-existing test failures (license tier assertion, AsyncMock for publish_primitive). Merged to main at v0.3.227. Status: partial.
- 2026-07-05: Prodmap pipelines QA: Fixed depends-on direction (core → cicd was inverted). Fixed false Known Gap about missing `test_graph_validator.py`. Fixed website docs path prefix. Updated delivery-tasks note.
- 2026-07-09: Cross-cutting QA (index 338): Updated frontmatter — added graph_validator unit tests to unit-tests, populated depends-on with feat-core-pipeline-execution, feat-pipelines-cicd-pipeline, feat-core-agent-model. Corrected stale claims: graph_validator/__init__.py is 817 lines (not 876), test_graph_validator.py now covers topology/schema/connector/backend/conditional edges (52 tests), test_category_validator.py covers node categories (12 tests). Removed dead `PipelineTemplateGallery.vue` code reference. Added Known Gaps for frontend i18n coverage (PipelineEditorView, CompositeEditorView all have 30+ hardcoded strings). Added `@handle_db_errors` decorator clarification to Error Handling section. Status: partial.
