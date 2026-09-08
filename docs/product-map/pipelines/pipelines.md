---
id: feat-pipelines
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/node_categories.py
  - backend/src/modulo/api/routes/pipeline_folders.py
  - backend/src/modulo/api/routes/composite_templates.py
  - backend/src/modulo/core/pipeline_engine
unit-tests:
  - backend/tests/unit/api/test_pipelines_endpoint.py
  - backend/tests/unit/api/test_node_category_endpoint.py
  - backend/tests/unit/api/test_composite_templates_api.py
  - backend/tests/unit/api/test_pipeline_patch_updated_at.py
  - backend/tests/unit/api/test_pipeline_copy_errors.py
  - backend/tests/unit/api/test_pipeline_retry_policy.py
  - backend/tests/unit/api/test_pipeline_team_visibility.py
  - backend/tests/unit/test_pipeline_execution.py
  - backend/tests/unit/test_pipeline_node_conversion.py
  - backend/tests/unit/graph_validator
  - backend/tests/unit/pipeline_engine
bdd:
  - backend/tests/bdd/features/pipelines/create.feature
  - backend/tests/bdd/features/pipelines/crud.feature
  - backend/tests/bdd/features/pipelines/node_types.feature
  - backend/tests/bdd/features/pipelines/conditional_transitions.feature
  - backend/tests/bdd/features/pipelines/concurrency.feature
  - backend/tests/bdd/features/pipelines/error_recovery.feature
  - backend/tests/bdd/features/pipelines/scheduling.feature
  - backend/tests/bdd/features/pipelines/webhook_trigger.feature
  - backend/tests/bdd/features/pipelines/checkpoint_resume.feature
  - backend/tests/bdd/features/admin/node-categories.feature
  - backend/tests/bdd/steps/test_pipelines.py
  - backend/tests/bdd/steps/test_alpha_pipelines.py
  - backend/tests/bdd/steps/test_node_categories.py
depends-on:
  - feat-schemas
  - feat-model-backends
  - feat-connectors
  - feat-router
status: covered
---

# Visual Pipeline Editor and Pipeline Graph

Pipelines are the visual, composable graph of agent / manual / approval / router nodes
that Modulo executes, authored through `/pipelines/:id/editor`, `/pipelines`,
`/library/:id/create-pipeline` and the composite editor. `api/routes/pipelines.py` owns
pipeline CRUD and the versioned snapshot endpoints (`feat-pipelines-pipeline-versioning` /
`-diff-rollback` hang off this surface), and execution semantics are pinned by
`core/pipeline_engine` and the pipelines BDD/unit suites.

## Behaviours

- [x] Pipeline creation supports minimal, LLM-node, manual-node and run_context-default
      configs; duplicate names are refused (409) (`create.feature`)
- [x] Pipeline CRUD is team/org scoped and versioned, with copy errors surfaced
      (`crud.feature`, `test_pipeline_copy_errors.py`, `test_pipeline_patch_updated_at.py`)
- [x] Node types — standard agent, manual (pauses to `awaiting_human`), HITL gate
      (`waiting_for_approval`) — are authorable and execute per type (`node_types.feature`)
- [x] Conditional transitions and parallel fan-out route state between nodes
      (`conditional_transitions.feature`)
- [x] Concurrency and error-recovery guard the authored graph
      (`concurrency.feature`, `error_recovery.feature`); graph/config validation is
      unit-covered (`tests/unit/graph_validator`, `test_pipelines_endpoint.py`)
- [x] Scheduling and webhook triggers start runs from the authored graph
      (`scheduling.feature`, `webhook_trigger.feature`); checkpoint/resume replays a
      failed run from its last checkpoint — now BDD-exercised end to end
      (`checkpoint_resume.feature`) and unit-covered
      (`tests/unit/pipeline_engine` recovery suite)
- [x] Node categories: deleting an unreferenced category succeeds, deleting one still
      referenced by a pipeline node is refused (409) with the referencing pipeline listed,
      and viewers cannot delete categories (403) (`admin/node-categories.feature`)
- [x] Graph validation and run-time enforcement are unit-covered under
      `tests/unit/graph_validator` and `tests/unit/pipeline_engine`
      (`test_pipeline_execution.py`, `test_pipeline_node_conversion.py`)
- [x] Sandbox `agent_commands` LIST items ending with a heredoc terminator are
      rejected at save time with code `SANDBOX_HEREDOC_TERMINATOR_IN_LIST_ITEM`
      — list items are joined with `commands_concatenation_string`, so a
      terminated item would corrupt into `PY && <next>` (unterminated heredoc)
      or a line-leading `&&` that no join fix can repair without changing
      operator semantics (reject, never clamp — same precedent as FAR-511); a
      scalar `agent_command` is unaffected because there is no join
      (FAR-664, `backend/tests/unit/graph_validator/test_edges_and_sandbox_validation.py`)

## Known Gaps

- **Run-level execution, history and output diff semantics are tracked under `feat-runs`**
  (and `feat-pipelines-pipeline-diff-rollback`) — this entry covers authoring,
  management, validation and the graph layer, not the run-detail surfaces.
- **`run_context.feature` / `run_variants.feature`** live under the pipelines BDD
  directory but describe run-time behaviour and are registered for execution — by
  `steps/test_run_context.py` and `steps/test_pipelines.py` respectively — so they are
  not re-listed here to keep the run surfaces owned by `feat-runs` / `feat-variants`.
- **No executing BDD surface for `run_lifecycle.feature` / `run_sequential.feature`** —
  the two feature files ship under `tests/bdd/features/pipelines/` but no step module
  registers them via `scenarios(...)`, so they never execute. The run lifecycle is
  otherwise pinned by the registered step suite (`steps/test_pipelines.py` engine-pickup
  / node-completion / error / cancellation transitions); wiring these files up needs a
  few missing step definitions written.
- **No executing BDD surface for graph validation, pipeline-config validation or
  checkpoint/resume** — `pipelines/validation.feature` and
  `pipelines/pipeline_config_validation.feature` ship under `tests/bdd/features/pipelines/`
  but `steps/test_pipelines.py` does not register them via `scenarios(...)`, so they
  never execute and are no longer cited as coverage here. The behaviours are
  unit-covered (`tests/unit/graph_validator`, `tests/unit/pipeline_engine`,
  `test_pipelines_endpoint.py`); wiring the feature files up needs their missing step
  definitions written. `checkpoint_resume.feature` is now registered and exercises the
  replay-from-last-checkpoint contract (`steps/test_pipelines.py`).

## QA History

- 2026-09-08: **improve-architecture (product-map walk)** — added the FAR-664
  sandbox save-time validation behaviour to the graph layer: `agent_commands`
  list items terminated by a heredoc terminator are rejected at save time
  (`SANDBOX_HEREDOC_TERMINATOR_IN_LIST_ITEM`) while a scalar `agent_command` is
  unaffected (`graph_validator/__init__.py`
  `_check_sandbox_heredoc_list_item`, unit-covered in
  `test_edges_and_sandbox_validation.py`).
- 2026-09-08: **improve-architecture (product-map walk)** — corrected a stale coverage
  claim in Known Gaps: `run_lifecycle.feature` / `run_sequential.feature` were described
  as "exercised by the same step suite", but no step module registers them via
  `scenarios(...)` (verified across `backend/tests/bdd/steps/`). The claim now splits the
  registered run files (`run_context.feature`, `run_variants.feature`) from the two
  never-executing ones, and the latter are listed as a genuine no-executing BDD gap.
- 2026-09-07: **improve-architecture (feature-gap walk)** — registered
  `checkpoint_resume.feature` in `steps/test_pipelines.py` (previously shipped but never
  executed) and aligned the resume step so a `Given a run that failed at node N` derives
  the restart node from the failure point. The three checkpoint/resume scenarios now
  collect and pass; the graph/config-validation BDD gap remains for
  `validation.feature` / `pipeline_config_validation.feature`.
- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-pipelines`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/pipelines.py`,
  `core/pipeline_engine` and the pipelines/graph-validator BDD+unit suites.
  Status: covered.
