---
id: feat-router
prd: N/A
adr:
  - docs/adr/025-execution-graph-router-hitl-nodes.md
code:
  - backend/src/modulo/core/pipeline_engine/jmespath_eval.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
  - backend/src/modulo/core/pipeline_engine/errors.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/core/workflow_import_export/__init__.py
  - backend/src/modulo/db/models/run.py
  - backend/src/modulo/db/migrations/versions/0150_add_router_no_match_status.py
  - frontend/src/views/PipelineEditorView.vue
  - frontend/src/constants/runStatuses.ts
unit-tests:
  - backend/tests/unit/pipeline_engine/test_router_hitl_nodes.py
  - backend/tests/integration/test_analytics_endpoint.py
bdd: []
depends-on:
  - feat-pipelines
status: covered
---

# Router & HITL Execution-Graph Nodes

First-class, authorable Router decision nodes and human-in-the-loop (HITL) gate
nodes in the pipeline execution graph (FAR-402 P1 / FAR-415, ADR 025). The
Router promotes the buried conditional-edge branching into a visible ordered
rule node; the HITL node promotes the legacy edge-gate HITL into a draggable
node that compiles to the exact same synthetic-gate path. Ships under the
`/pipelines` surface (`feat-pipelines`) and is registered in the manifest
registry (`feat-router`).

## Behaviours

- [x] `router` is an API-authorable `PipelineGraphNode.node_type` (with a
      `router_config`; `PipelineGraphNode` validates presence of rules) and
      compiles in `build_graph_from_json`
- [x] `make_router_node_fn` evaluates ordered `{guard (JMESPath), target}`
      rules against state, first-match-wins
- [x] An explicit `default` rule maps to its target; `/ _make_conditional_router`
      lowers Router onto the existing conditional-edge compile path
- [x] LLM classifier mode (`mode == "classifier"`) matches the
      `state["_llm_next_node"]` label to a rule `label`, falling back to the
      default rule
- [x] Router shares ONE truthiness rule (`bool(...)`) with every other JMESPath
      guard site (conditional edges, loop counters, HITL gate conditions,
      polling triggers) via the consolidated evaluator
      `evaluate_jmespath_condition` (`jmespath_eval.py`); invalid expressions
      surface a `ValueError`
- [x] Compile-time default-rule enforcement: a *new* Router node without a
      `default` rule raises `RouterConfigError` (classifier mode exempt);
      existing conditional-edge graphs remain valid (backward compat)
- [x] Runtime no-match (no rule matches and no default) raises
      `RouterNoMatchError`; the executor terminalizes the run with the
      `router_no_match` terminal status and error code `router.no_match`,
      NOT classified as `failed`
- [x] `router_no_match` is a terminal, non-failure run status in
      `TERMINAL_STATUSES` (`run.py`) and in the `ck_runs_status` DB CHECK
      constraint (migration `0150_add_router_no_match_status`), echoed in
      `frontend/src/constants/runStatuses.ts` and analyzable via the analytics
      status filter
- [x] Router rule targets are excluded from pipeline entry-point resolution
- [x] `hitl` is an API-authorable node type; the HITL node's `hitl_config` is
      injected onto each outgoing edge and flows through the identical legacy
      synthetic-gate path (a compile-equivalence test asserts the `hitl` node
      produces the same compiled graph as the legacy edge-gate HITL)
- [x] `manual` is retained as the non-gating human-output step (distinct from
      the gating `hitl` node)
- [x] Taxonomy reconciliation: `loop` is an authorable edge type in
      `VALID_EDGE_TYPES`, while `connector` remains an internal engine
      resolution, never an API-authored node type
- [x] Pipeline editor renders Router nodes with a dedicated `node-router`
      template slot and localised labels (`PipelineEditorView.vue`)

## Known Gaps

- **No BDD feature scenarios** — Router/HITL/loop behaviour is pinned by unit
  tests (`test_router_hitl_nodes.py`) and the analytics round-trip integration
  test only; there is no `.feature` file for authoring Router/HITL pipelines.
- **Edge-gate HITL remains compile-supported** — the legacy edge-level
  `hitl_gate_config` is deliberately not removed; the `hitl` node lowers onto
  it (ADR 025, backward-compatible).

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — entry added to close
  the feature-graph gap behind the manifest `feat-router` registry entry (with
  `/pipelines` route referencing it) that shipped in FAR-415 but had no
  behaviour-tracker node. Behaviours re-verified against ADR 025,
  `pipeline_engine/{node_runner,graph_cache,jmespath_eval,errors,executor}*.py`,
  `run.py` + migration `0150_add_router_no_match_status`, the
  `PipelineGraphNode` validators in `api/routes/pipelines.py`, and the
  router/HITL unit + analytics integration suites. Status: covered.
