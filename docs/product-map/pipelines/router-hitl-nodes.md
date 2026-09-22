---
id: feat-router
prd: N/A
adr:
  - ADR 025 (execution-graph-router-hitl-nodes)
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
bdd:
  - backend/tests/bdd/features/pipelines/router_nodes.feature
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

- **Edge-gate HITL remains compile-supported** — the legacy edge-level
  `hitl_gate_config` is deliberately not removed; the `hitl` node lowers onto
  it (ADR 025, backward-compatible). (The prior "No BDD feature scenarios"
  gap was closed 2026-09-17 by `router_nodes.feature` — Router authoring,
  first-match-wins/default/classifier routing semantics, and the
  RouterNoMatchError → `router_no_match` terminalisation are now executing BDD
  coverage.)

## QA History

- 2026-09-17: **product-map review pass** — closed the
  "No BDD feature scenarios" Known Gap. Added
  `backend/tests/bdd/features/pipelines/router_nodes.feature` (wired from
  `steps/test_router_nodes.py`), exercising the real shipped seams: the
  `build_graph_from_json` compile path (Router-node compile-time default-rule
  guard + rule-target entry-point exclusion), the real `make_router_node_fn`
  decision function (first-match-wins, default rule, classifier label mode,
  runtime `RouterNoMatchError`), and the executor's real
  `_stream_operational_outcome` mapping of `RouterNoMatchError` to the
  terminal `router_no_match` status (code `router.no_match`), distinct from a
  `failed` classification, plus the `TERMINAL_STATUSES` contract. All 9
  scenarios executing green; `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-08-29: **product-map review pass** — entry added to close
  the feature-graph gap behind the manifest `feat-router` registry entry (with
  `/pipelines` route referencing it) that shipped in FAR-415 but had no
  behaviour-tracker node. Behaviours re-verified against ADR 025,
  `pipeline_engine/{node_runner,graph_cache,jmespath_eval,errors,executor}*.py`,
  `run.py` + migration `0150_add_router_no_match_status`, the
  `PipelineGraphNode` validators in `api/routes/pipelines.py`, and the
  router/HITL unit + analytics integration suites. Status: covered.
