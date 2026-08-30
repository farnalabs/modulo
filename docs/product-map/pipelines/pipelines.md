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
  - backend/tests/bdd/features/pipelines/validation.feature
  - backend/tests/bdd/features/pipelines/pipeline_config_validation.feature
  - backend/tests/bdd/features/pipelines/scheduling.feature
  - backend/tests/bdd/features/pipelines/checkpoint_resume.feature
  - backend/tests/bdd/features/pipelines/webhook_trigger.feature
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
`-diff-rollback` hang off this surface), node categories are managed under
`/admin/node-categories`, and execution semantics are pinned by `core/pipeline_engine` and
the pipelines BDD/unit suites.

## Behaviours

- [x] Pipeline creation supports minimal, LLM-node, manual-node and run_context-default
      configs; duplicate names are refused (409) (`create.feature`)
- [x] Pipeline CRUD is team/org scoped and versioned, with copy errors surfaced
      (`crud.feature`, `test_pipeline_copy_errors.py`, `test_pipeline_patch_updated_at.py`)
- [x] Node types — standard agent, manual (pauses to `awaiting_human`), HITL gate
      (`waiting_for_approval`) — are authorable and execute per type (`node_types.feature`)
- [x] Conditional transitions and parallel fan-out route state between nodes
      (`conditional_transitions.feature`)
- [x] Concurrency, error-recovery, validation and pipeline-config validation guard the
      authored graph (`concurrency.feature`, `error_recovery.feature`,
      `validation.feature`, `pipeline_config_validation.feature`)
- [x] Scheduling and webhook triggers start runs from the authored graph
      (`scheduling.feature`, `webhook_trigger.feature`); checkpoint/resume replays a
      failed run from its last checkpoint (`checkpoint_resume.feature`)
- [x] Node categories: deleting an unreferenced category succeeds, deleting one still
      referenced by a pipeline node is refused (409) with the referencing pipeline listed,
      and viewers cannot delete categories (403) (`admin/node-categories.feature`)
- [x] Graph validation and run-time enforcement are unit-covered under
      `tests/unit/graph_validator` and `tests/unit/pipeline_engine`
      (`test_pipeline_execution.py`, `test_pipeline_node_conversion.py`)

## Known Gaps

- **Run-level execution, history and output diff semantics are tracked under `feat-runs`**
  (and `feat-pipelines-pipeline-diff-rollback`) — this entry covers authoring,
  management, validation and the graph layer, not the run-detail surfaces.
- **`run_context.feature` / `run_lifecycle.feature` / `run_sequential.feature` /
  `run_variants.feature`** live under the pipelines BDD directory but describe run-time
  behaviour; they are exercised by the same step suite and are not re-listed here to keep
  the run surfaces owned by `feat-runs`.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-pipelines`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/pipelines.py`,
  `core/pipeline_engine` and the pipelines/graph-validator BDD+unit suites.
  Status: covered.
