---
id: feat-frontend-eval-editor-ui
prd: 8.17
delivery-tasks: [task-nv2-eval-ui-editor]
bdd:
  - backend/tests/bdd/features/eval/eval_run.feature
code:
  - frontend/src/views/EvalEditorView.vue
  - frontend/src/router/index.ts
depends-on: [feat-evals-eval-definitions]
status: partial
---

# Frontend Eval Editor UI

CRUD UI for eval definitions — create, edit, delete evals scoped to a pipeline and optionally a node. Supports all four eval types from 8.17: llm_judge, regex, json_schema, custom_function.

## Behaviours

### Pipeline & node selection

- [ ] Load pipeline list from `/api/v1/pipelines` on mount
- [ ] Select a pipeline to load its graph nodes and existing evals
- [ ] Select a node (optional) to scope the eval to a single node's output
- [ ] Show node loading spinner while graph endpoint resolves
- [ ] Disable node selector when no pipeline selected

### Create eval definition

- [ ] Fill form: name, eval type, config JSON, pass threshold (slider 0–1), failure behaviour (warn/block)
- [ ] Save sends POST `/api/v1/evals` with pipeline_id, name, eval_type, config_json, pass_threshold, failure_behaviour
- [ ] Reset form on successful create and reload evals list
- [ ] Show success toast "Eval created." for 2s
- [ ] Show inline error on API failure

### Edit eval definition

- [ ] Click edit icon on an existing eval to populate form with its current values
- [ ] Update sends PUT `/api/v1/evals/{id}`
- [ ] Show success toast "Eval updated." for 2s
- [ ] Cancel reverts form to empty / new-eval state

### Delete eval definition

- [ ] Click delete icon reveals inline "Confirm / No" buttons
- [ ] Confirm sends DELETE `/api/v1/evals/{id}`
- [ ] Remove deleted eval from list without full reload
- [ ] Cancel hides confirmation without action

### Eval type selection

- [ ] Select from: llm_judge, regex, json_schema, custom_function
- [ ] Config JSON textarea with placeholder per type
- [ ] Real-time JSON parse validation; show "Invalid JSON" inline error
- [ ] Disable save button when JSON is invalid

### Pass threshold

- [ ] Range slider 0.0–1.0 in 0.05 steps
- [ ] Show current numeric value alongside slider label

### Failure behaviour

- [ ] Radio toggle: warn vs block
- [ ] Show contextual description beneath radios

### Eval list

- [ ] Show evals for selected pipeline in a scrollable list
- [ ] Each card shows: name, eval type badge, failure behaviour badge, threshold, node id (if scoped)
- [ ] Empty state: "No evals for this pipeline yet."
- [ ] Prompt state: "Select a pipeline above to see its evals."

### Error & loading states

- [ ] Full-page loading spinner on mount
- [ ] Full-page error banner with retry button on load failure
- [ ] Evals list loading spinner while fetching
- [ ] Save button shows "Saving..." and is disabled during submission
- [ ] Delete confirm button shows "..." and is disabled during deletion
- [ ] Pipeline load error displays inline message

### Route

- [ ] Accessible at `/evals/editor` (name: `eval-editor`)

## Known Gaps

- No BDD feature file for eval editor UI specifically — only eval_run.feature exists under backend/tests/bdd/features/eval/
- No pagination in evals list (API response carries pagination fields but UI shows all)
- No search or filter within evals list
- Suite assignment UI missing (suite_id field exists on EvalDefinition but no UI to assign)
- No eval preview / test-against-sample-output workflow
- No keyboard shortcuts
- No form validation for pass_threshold being required for llm_judge / custom_function types
- No conditional form sections per eval type (e.g. pattern field for regex, schema editor for json_schema)
