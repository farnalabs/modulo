---
id: feat-frontend-eval-editor-ui
prd: 8.17
delivery-tasks: [task-nv2-eval-ui-editor]
bdd:
  - backend/tests/bdd/features/eval/eval_run.feature
code:
  - frontend/src/views/EvalEditorView.vue
  - frontend/src/router/index.ts
unit-tests:
  - frontend/src/__tests__/EvalEditorView.spec.ts
  - backend/tests/unit/api/test_evals_endpoint.py
  - backend/tests/unit/api/test_evals_compare.py
  - backend/tests/unit/api/test_evals_dashboard.py
  - backend/tests/unit/core/test_eval_engine.py
  - backend/tests/unit/core/test_eval_judge_injection.py
  - backend/tests/unit/core/test_eval_regressions.py
  - backend/tests/unit/core/test_eval_suite.py
depends-on: [feat-evals-eval-definitions]
status: partial
---

# Frontend Eval Editor UI

CRUD UI for eval definitions — create, edit, delete evals scoped to a pipeline and
optionally a node. Supports all four eval types from 8.17: llm_judge, regex,
json_schema, custom_function.

## Behaviours

### Pipeline & node selection

- [x] Load pipeline list from `/api/v1/pipelines` on mount
- [x] Select a pipeline to load its graph nodes and existing evals
- [x] Select a node (optional) to scope the eval to a single node's output
- [x] Show node loading spinner while graph endpoint resolves
- [x] Disable node selector when no pipeline selected

### Create eval definition

- [x] Fill form: name, eval type, config JSON, pass threshold (slider 0–1), failure behaviour (warn/block)
- [x] Save sends POST `/api/v1/evals` with pipeline_id, name, eval_type, config_json, pass_threshold, failure_behaviour
- [x] Reset form on successful create and reload evals list
- [x] Show success toast "Eval created." for 2s
- [x] Show inline error on API failure

### Edit eval definition

- [x] Click edit icon on an existing eval to populate form with its current values
- [x] Update sends PUT `/api/v1/evals/{id}`
- [x] Show success toast "Eval updated." for 2s
- [x] Cancel reverts form to empty / new-eval state

### Delete eval definition

- [x] Click delete icon reveals inline "Confirm / No" buttons
- [x] Confirm sends DELETE `/api/v1/evals/{id}`
- [x] Remove deleted eval from list without full reload
- [x] Cancel hides confirmation without action

### Eval type selection

- [x] Select from: llm_judge, regex, json_schema, custom_function
- [ ] Config JSON textarea with placeholder per type
- [x] Real-time JSON parse validation; show "Invalid JSON" inline error
- [x] Disable save button when JSON is invalid

### Pass threshold

- [x] Range slider 0.0–1.0 in 0.05 steps
- [x] Show current numeric value alongside slider label

### Failure behaviour

- [x] Radio toggle: warn vs block
- [x] Show contextual description beneath radios

### Eval list

- [x] Show evals for selected pipeline in a scrollable list
- [x] Each card shows: name, eval type badge, failure behaviour badge, threshold, node id (if scoped)
- [x] Empty state: "No evals for this pipeline yet."
- [x] Prompt state: "Select a pipeline above to see its evals."

### Error & loading states

- [x] Full-page loading spinner on mount
- [x] Full-page error banner with retry button on load failure
- [x] Evals list loading spinner while fetching
- [x] Save button shows "Saving..." and is disabled during submission
- [x] Delete confirm button shows "..." and is disabled during deletion
- [x] Pipeline load error displays full-page ErrorAlert with retry (by design — pipelines load on mount, not inline scope)

### Route

- [x] Accessible at `/evals/editor` (name: `eval-editor`)

## Error Handling

- [x] Pipeline load failure → full-page error with retry
- [x] Node load failure → inline error message below node selector
- [x] Evals list load failure → inline error message below heading
- [x] Save API failure → inline form error with API message
- [x] Delete API failure → inline form error with API message
- [x] Delete-404 → specific "already deleted" message
- [x] Invalid JSON config → inline parse error (configParseError computed)
- [x] Network unavailable on initial load → full-page error

## Edge Cases

- [x] Delete while network down → error shown in form
- [x] Save while editing cleared form → Cancel resets to new
- [x] Switch pipeline during edit → form resets (onPipelineChange calls resetForm)
- [x] Empty pipeline list → disabled node selector, no evals prompt
- [x] Pipeline with no graph nodes → empty node selector
- [x] Edit then switch pipeline → form is cleared (resetForm called in onPipelineChange)
- [x] Delete eval that was already deleted by another user → specific "Eval was already deleted" error

## Known Gaps

- No BDD feature file for eval editor UI specifically — only eval_run.feature
  exists under backend/tests/bdd/features/eval/
- No pagination in evals list (API response carries pagination fields but UI shows all)
- No search or filter within evals list
- Suite assignment UI missing (suite_id field exists on EvalDefinition but no UI to assign)
- No eval preview / test-against-sample-output workflow
- No keyboard shortcuts
- No form validation for pass_threshold being required for llm_judge / custom_function types
- No conditional form sections per eval type (e.g. pattern field for regex, schema editor for json_schema)
- Config textarea has a single generic placeholder, not type-specific placeholders
- Smoke test is minimal (renders + text check only, no interaction tests)

## QA History

### 2026-07-04 — Cross-cutting QA (index 125)

Verified ~36 behaviours, 8 error handling paths, 7 edge cases. Fixed 5 issues: silent error swallowing in loadNodes/loadEvals (inline error messages), Delete-404 message, config placeholder i18n, hardcoded 'Invalid JSON' → t().

### 2026-07-06 — Cross-cutting QA follow-up

Verified i18n compliance, no `${err}` template literals, backend ProgrammingError/SQLAlchemyError catching on all eval routes. Website docs stub created.

### 2026-07-09 — Second-pass QA (frontend docs)

- **Fixed**: Pipeline load error checkbox corrected from `[ ]` to `[x]` — full-page ErrorAlert with retry is the intended design (pipelines load on mount, not inline scope). Cleaned up inline explanation.
