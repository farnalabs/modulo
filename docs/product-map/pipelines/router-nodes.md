---
id: feat-router
prd: N/A
adr:
  - docs/adr/003-agent-dispatch-model.md
code:
  - backend/src/modulo/core/pipeline_engine/jmespath_eval.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/pipeline_engine/classify.py
  - backend/src/modulo/core/pipeline_engine/errors.py
  - backend/src/modulo/core/lifecycle_map/advancement.py
  - backend/src/modulo/core/lifecycle_map/reconcile.py
  - backend/src/modulo/core/analytics/builder.py
  - backend/src/modulo/db/models/run.py
unit-tests:
  - backend/tests/unit/pipeline_engine/test_router_hitl_nodes.py
  - backend/tests/unit/pipeline_engine/test_run_classification.py
  - backend/tests/unit/core/test_trigger_streak_engine.py
bdd: []
depends-on:
  - feat-pipelines
status: covered
---

# Router Decision Nodes

`router` is an API-authored execution-graph node that lowers to the conditional-edge
compile path (FAR-402 P1 / FAR-415): ordered JMESPath guard rules route run state to a
target node, an ordered `default` rule catches unmatched state, and an optional
`classifier` mode routes on an LLM-produced `_llm_next_node` label. A router with no
matching rule and no default raises `RouterNoMatchError`, which the executor
terminalizes as the dedicated `router_no_match` run status (classified as excluded,
never mislabeled `budget_exceeded`) and which advances out of lifecycle-maps / reconcile /
analytics terminal sets. The `router` node type is listed in `docs/architecture.md`
alongside `agent` / `sandbox_agent` / `manual` / `composite` / `hitl`.

## Behaviours

- [x] A `router` node is authorable in a pipeline snapshot with an ordered `rules`
      list; the node lowers to the conditional-edge compile path at run start
- [x] Rules are evaluated in order with a shared JMESPath evaluator; the first
      matching guard wins and routes state to its target (`test_router_first_match_wins`)
- [x] An ordered `default: true` rule catches matched-by-nothing state
      (`test_router_default_used_when_no_match`); compile-time validation requires a
      default rule unless the node is in `classifier` mode (`test_validate_router_config_requires_default`)
- [x] `classifier` mode routes on the `_llm_next_node` label; a missing label key falls
      back to the default rule (`test_router_classifier_mode_matches_label`,
      `test_router_no_label_key_falls_to_default`)
- [x] A router with no matching rule and no default raises `RouterNoMatchError` at
      routing time (`test_router_no_match_raises`)
- [x] The executor catches `RouterNoMatchError` and terminalizes the run with the
      dedicated `router_no_match` status + `router.no_match` error code — never an
      unclassified `failed` (`test_execute_router_no_match_terminalizes_as_router_no_match`,
      `test_execute_router_no_match_not_classed_as_failed`)
- [x] `router_no_match` is accepted by the run status CHECK constraint, the persistence
      whitelist, and the shared terminal set, and is persisted with `completed_at` by
      `update_run_status` (`test_run_model_check_constraint_allows_router_no_match`,
      `test_run_status_whitelist_includes_router_no_match`,
      `test_terminal_statuses_include_router_no_match`,
      `test_update_run_status_persists_router_no_match_with_completed_at`)
- [x] `router_no_match` runs classify as excluded with the dedicated
      `REASON_ROUTER_NO_MATCH` — never the budget-related label
      (`test_router_no_match_is_not_mislabeled_budget_exceeded`)
- [x] A router's rule targets are registered as graph targets so the entry-point
      selection never picks a rule target as the pipeline entry
      (`test_router_rule_targets_excluded_from_entry_point`)
- [x] A `router` node compiles into the graph; `router_no_match` is a terminal status
      that advances lifecycle-maps / reconcile / analytics terminal sets
      (`test_router_node_compiles`, `_ADVANCING_TERMINAL_STATUSES`,
      `ROUTER_NO_MATCH` in analytics builder)

## Known Gaps

- **No dedicated BDD feature file** — router routing, no-match terminalization and
  classification are unit-tested only (`test_router_hitl_nodes.py`,
  `test_run_classification.py`); there is no pytest-bdd scenario file for the router
  node surface.

## QA History

- 2026-08-26: **improve-architecture (product-map walk)** — `feat-router` was
  registered in the manifest `features:` registry and referenced by the `/pipelines`
  route but had no human-readable product-map entry and no graph-index listing
  (invisible to Remy's feature graph). Added this behaviour-tracker entry keyed to the
  `feat-router` id and linked it from the graph index, verified against the FAR-415
  implementation in `pipeline_engine/{jmespath_eval,node_runner,graph_cache,executor,classify,errors}.py`
  and the `test_router_hitl_nodes.py` / `test_run_classification.py` suites.
