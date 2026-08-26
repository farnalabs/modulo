---
id: feat-router
prd: N/A
adr:
  - docs/adr/025-execution-graph-router-hitl-nodes.md
code:
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/jmespath_eval.py
  - backend/src/modulo/core/pipeline_engine/errors.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/pipeline_engine/classify.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/core/workflow_import_export/__init__.py
  - backend/src/modulo/db/migrations/versions/0150_add_router_no_match_status.py
  - frontend/src/views/PipelineEditorView.vue
  - frontend/src/constants/runStatuses.ts
  - frontend/src/lib/api/schema.ts
unit-tests:
  - backend/tests/unit/pipeline_engine/test_router_hitl_nodes.py
  - backend/tests/unit/core/test_graph_cache.py
  - backend/tests/unit/pipeline_engine/test_conditional_transitions.py
  - backend/tests/unit/core/test_trigger_streak_engine.py
  - backend/tests/integration/test_analytics_endpoint.py
bdd: []
depends-on:
  - feat-pipelines
status: covered
---

# Router Nodes

A first-class, authorable Router decision node in the execution graph (FAR-402 P1 /
FAR-415, ADR-025 F2-A). A Router carries ordered `{guard (JMESPath), target}` rules
that are evaluated first-match-wins against the run state, plus an explicit `default`
rule, and lowers onto the same conditional-edge compile path — so every branching
primitive shares one truthiness rule. Run through the visual pipeline editor and
visible in run analytics as the terminal, non-failure `router_no_match` outcome.

## Behaviours

- [x] `router` is an authorable `node_type` in the pipeline API schema and compiles
      via `graph_cache.build_graph_from_json` (`router_config`, OpenAPI schema surfacing
      in `frontend/src/lib/api/schema.ts`)
- [x] Ordered rules are evaluated first-match-wins through the shared JMESPath evaluator
      (`evaluate_jmespath_condition`) — the same engine used by conditional edges,
      loop counters, HITL gate conditions, and polling triggers
- [x] An explicit `default` rule maps to its target when no rule matches
- [x] LLM classifier mode (`mode: "classifier"`) matches the `_llm_next_node` state
      label against each rule's `label`, falling back to the default rule
- [x] Compile-time default-rule enforcement raises the typed `RouterConfigError` for
      new Router nodes; legacy conditional-edge graphs without a default remain valid
      (backward-compatible)
- [x] A Router with no matching rule and no `default` raises `RouterNoMatchError`;
      the executor terminalizes the run with the new terminal, non-failure
      `router_no_match` status instead of an unclassified `failed`
- [x] `router_no_match` is accepted by the run-status CHECK constraint,
      `RUN_STATUS_WHITELIST`, `TERMINAL_STATUSES`, and the fenced transition SQL
      (persistence-layer acceptance in `test_router_hitl_nodes.py`)
- [x] `router_no_match` runs classify into the `excluded` bucket (`REASON_ROUTER_NO_MATCH`,
      never budget-attributed) and are reflected in analytics/filter surfaces
- [x] Router rule/default targets are registered as graph targets so the pipeline
      entry-point selection can never choose a rule target as the entry node
- [x] `loop` edge type is authorable in the API and `workflow_import_export`
      `VALID_EDGE_TYPES` (taxonomy reconciliation, FAR-402 P1 F2)
- [x] Pipeline Editor renders router nodes and transmits `router_config` on graph save
      so imported router graphs persist

## Known Gaps

- **LLM classifier mode is a label-matching shim** over the existing `_llm_next_node`
  state; a standalone LLM classifier prompt/routing heuristic is out of scope.
- **Status registry is still spread across ~12 sites** — the single-registry refactor
  recommended by ADR-025 §10 is deferred; the sites were updated directly.

## QA History

- 2026-08-26: **improve-architecture (product-map walk)** — entry added for the newly
  shipped `feat-router` (FAR-402 P1, PR #1917) so the registered manifest feature has a
  feature-graph node and is discoverable by Remy's `search_documentation` indexer.
  Behaviours verified against `node_runner.make_router_node_fn`,
  `graph_cache._validate_router_config` / `build_graph_from_json`, `jmespath_eval.py`,
  `executor.py` terminalization, `classify.py`, and `test_router_hitl_nodes.py` +
  `test_graph_cache.py`. Status: covered.