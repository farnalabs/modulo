---
id: feat-evals-conditional-transitions
prd: 8.17
delivery-tasks: [task-nv9-conditional-transitions]
bdd:
  - backend/tests/features/evals/conditional_hitl.feature
code:
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/eval_engine/__init__.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - backend/src/modulo/db/models/pipeline_edge.py
  - backend/src/modulo/api/routes/pipelines.py
unit-tests:
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/api/test_evals_endpoint.py
depends-on: [feat-evals-eval-engine]
status: partial
---

# Conditional Transitions

Two conditional routing mechanisms within the LangGraph pipeline: (A) **conditional edges** — JMESPath-based graph edges (`type: "conditional"`) that route execution to different target nodes based on state, and (B) **conditional HITL gating** — a JMESPath `condition` on the HITL gate config that skips the gate when falsy, plus eval-before-interrupt where node-scoped eval definitions are evaluated after the condition check and before the interrupt.

## Behaviours

### Conditional Edge Routing

- [ ] Edge `type: "conditional"` compiled via `add_conditional_edges` with a JMESPath-based router function
- [ ] Router evaluates `condition_expression` against the full LangGraph state dict
- [ ] First matching conditional edge's target is returned as the next node
- [ ] Normal edges from the same source serve as fallback targets when no condition matches
- [ ] `default_target` on a conditional edge overrides the fallback behaviour
- [ ] Source node with conditional edges uses router for ALL outgoing edges (normal edges become fallbacks only)
- [ ] No normal targets and no default target — last conditional edge's target is used as implicit default
- [ ] No edges to route through raises `ValueError`
- [ ] Conditional edges are validated for non-empty `condition_expression` by `graph_validator`
- [ ] `condition_expression` is persisted in DB column `pipeline_edges.condition_expression`
- [ ] API schema accepts `edge_type: "conditional"` for pipeline edge creation
- [ ] All four eval result fields (score, passed, detail, evaluated_at) available to JMESPath condition expressions

### Conditional HITL Gating (JMESPath condition)
- [ ] `condition` field on `hitl_gate_config` supports JMESPath expression evaluated against state
- [ ] Condition evaluates to falsy value → gate skipped with `condition_skipped` artifact, no interrupt
- [ ] Condition evaluates to truthy value → gate proceeds to autonomy checks (may interrupt)
- [ ] Condition `null` or absent → gate proceeds normally (non-conditional behaviour)
- [ ] Falsy semantics: `None` → false, `False` → false, numeric `0` → false, empty string → false, empty list/dict → false
- [ ] Truthy semantics: `True` → true, non-zero numbers → true, non-empty strings/lists/dicts → true
- [ ] Conditional gate skip records `condition`, `condition_result`, and `"condition_skipped"` status in artifact

### Conditional HITL Gating (eval-reference — PRD 8.17)
- [ ] PRD specifies gate `condition` as `{eval_id, threshold, operator}` referencing an eval definition
- [ ] Code implements condition as JMESPath expression (not PRD format) — separate mechanism
- [ ] BDD scenarios test the eval-reference pattern explicitly: "gate condition references eval quality-check with threshold 0.8 operator lt"
- [ ] Eval-reference condition evaluates based on eval result (score compared to threshold with operator)
- [ ] Eval-reference condition true (score below threshold) → `NodeInterrupt` raised
- [ ] Eval-reference condition false (score at or above threshold) → execution continues without interrupt
- [ ] Gate artifact contains `"condition_skipped"` when eval-reference condition evaluates to false

### Eval-Before-Interrupt
- [ ] Node-scoped eval definitions evaluated after condition check but before interrupt
- [ ] Eval definitions loaded by executor: query `EvalDefinition` where `pipeline_id` matches and `node_id IS NOT NULL`
- [ ] Eval definitions grouped by `node_id` and passed to `build_graph_from_json`
- [ ] Eval with `failure_behaviour="warn"` logs warning — gate still proceeds to interrupt
- [ ] Eval with `failure_behaviour="block"` raises `EvalBlockedError` — prevents interrupt, run transitions to `eval_failed`
- [ ] Multiple evals on one node: all pass → gate proceeds normally
- [ ] Multiple evals on one node: first block failure raises `EvalBlockedError`, remaining evals are not evaluated
- [ ] `EvalBlockedError` includes eval name and detail in message
- [ ] Eval definitions are scoped to the upstream node (not the gate) — evaluated against upstream node output

### Resume Semantics
- [ ] Gate detects `_hitl_decision` in state on resume — returns immediately with approved/rejected artifact
- [ ] Resume check is first in gate function: condition and evals are NOT re-evaluated on resume
- [ ] Rejected decision routes via reject edge (if configured) or normal forward edge
- [ ] Gate artifact on resume records `"interrupted"` status and `result: "approved"` or `result: "rejected"`

### Error States
- [ ] Condition expression runtime error (invalid JMESPath) → percolates as node error, run fails
- [ ] Eval definitions list is empty → no eval check performed, gate proceeds to autonomy checks
- [ ] Eval definition with no `node_id` (pipeline-level) → not loaded by executor for eval-before-interrupt
- [ ] Block failure publishes `run_failed` broker event
- [ ] Block failure recorded in audit event
- [x] `EvalSuiteBlockedError` is raised when suite fails threshold check (raised at executor.py:629, caught at executor.py:502)

### Edge Cases
- [ ] Suite-level `pass_threshold` check after run completion — suite passes, run stays "complete"
- [ ] Suite-level `pass_threshold` check — suite fails, run transitions to "failed" with `error_code="eval_suite_blocked"`
- [ ] No suite definitions with `pass_threshold` → no post-run check
- [ ] Empty suite (no results) → passes at aggregate_score=1.0
- [ ] Multiple suites with thresholds — first failing suite terminates check
- [ ] Post-run suite check reads results from a fresh session after streaming session closes
- [ ] Eval definitions loaded before capacity slot claim — stale if definition added during wait
- [ ] Cancel during eval evaluation stops run with status "cancelled"

## Known Gaps
- [ ] PRD 8.17 specifies HITL gate `condition` as `{eval_id, threshold, operator}` referencing an eval definition. The code implements condition as a JMESPath expression against state (node_runner.py), with eval-definitions as a separate array. These are different mechanisms. The BDD includes scenarios for both — the implementation's eval-before-interrupt evaluates ALL eval_definitions against state with no per-eval threshold or operator. The PRD-specified eval-reference format is not implemented.
- [ ] `node_runner.py:168` — `engine.evaluate(state, eval_def)` return value is discarded. Eval results are not logged or persisted for eval-before-interrupt. Only exceptions are surfaced. Warn-level eval failures produce no output.
- [ ] BDD `conditional_hitl.feature` has 8 scenarios but no Python step implementations exist — scenarios are unwired and will not execute.
- [ ] No integration test for the full eval-before-interrupt → suite check → eval_failed chain end-to-end.
- [ ] `EvalSuiteBlockedError` is now raised at `executor.py:629` and caught at `executor.py:502`. However, the `for sr in suite_results: if not sr.passed:` branch in the try block is dead code — the exception always fires before any non-passing result can be returned. One path should be cleaned up. 