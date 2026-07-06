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

- [x] Edge `type: "conditional"` compiled via `add_conditional_edges` with a JMESPath-based router function
- [x] Router evaluates `condition_expression` against the full LangGraph state dict
- [x] First matching conditional edge's target is returned as the next node
- [x] Normal edges from the same source serve as fallback targets when no condition matches
- [x] `default_target` on a conditional edge overrides the fallback behaviour
- [x] Source node with conditional edges uses router for ALL outgoing edges (normal edges become fallbacks only)
- [x] No normal targets and no default target — last conditional edge's target is used as implicit default
- [x] No edges to route through raises `ValueError`
- [x] Conditional edges are validated for non-empty `condition_expression` by `graph_validator`
- [x] `condition_expression` is persisted in DB column `pipeline_edges.condition_expression`
- [x] API schema accepts `edge_type: "conditional"` for pipeline edge creation
- [x] All four eval result fields (score, passed, detail, evaluated_at) available to JMESPath condition expressions

### Conditional HITL Gating (JMESPath condition)
- [x] `condition` field on `hitl_gate_config` supports JMESPath expression evaluated against state
- [x] Condition evaluates to falsy value → gate skipped with `condition_skipped` artifact, no interrupt
- [x] Condition evaluates to truthy value → gate proceeds to autonomy checks (may interrupt)
- [x] Condition `null` or absent → gate proceeds normally (non-conditional behaviour)
- [x] Falsy semantics: `None` → false, `False` → false, numeric `0` → false, empty string → false, empty list/dict → false
- [x] Truthy semantics: `True` → true, non-zero numbers → true, non-empty strings/lists/dicts → true
- [x] Conditional gate skip records `condition`, `condition_result`, and `"condition_skipped"` status in artifact

### Conditional HITL Gating (eval-reference — PRD 8.17)
- [x] PRD specifies gate `condition` as `{eval_id, threshold, operator}` referencing an eval definition
- [x] Code implements `eval_condition` on gate config with `{eval_name, threshold, operator}` evaluated against captured eval results
- [x] BDD scenarios test the eval-reference pattern explicitly
- [x] Eval-reference condition evaluates based on eval result (score compared to threshold with operator)
- [x] Eval-reference condition true (score below threshold) → `NodeInterrupt` raised
- [x] Eval-reference condition false (score at or above threshold) → execution continues without interrupt
- [x] Gate artifact contains `"condition_skipped"` when eval-reference condition evaluates to false

### Eval-Before-Interrupt
- [x] Node-scoped eval definitions evaluated after condition check but before interrupt
- [x] Eval definitions loaded by executor: query `EvalDefinition` where `pipeline_id` matches and `node_id IS NOT NULL`
- [x] Eval definitions grouped by `node_id` and passed to `build_graph_from_json`
- [x] Eval with `failure_behaviour="warn"` logs warning — gate still proceeds to interrupt
- [x] Eval with `failure_behaviour="block"` raises `EvalBlockedError` — prevents interrupt, run transitions to `eval_failed`
- [x] Multiple evals on one node: all pass → gate proceeds normally
- [x] Multiple evals on one node: first block failure raises `EvalBlockedError`, remaining evals are not evaluated
- [x] `EvalBlockedError` includes eval name and detail in message
- [x] Eval definitions are scoped to the upstream node (not the gate) — evaluated against upstream node output

### Resume Semantics
- [x] Gate detects `_hitl_decision` in state on resume — returns immediately with approved/rejected artifact
- [x] Resume check is first in gate function: condition and evals are NOT re-evaluated on resume
- [x] Rejected decision routes via reject edge (if configured) or normal forward edge
- [x] Gate artifact on resume records `"interrupted"` status and `result: "approved"` or `result: "rejected"`

### Error States
- [x] Condition expression runtime error (invalid JMESPath) → percolates as node error, run fails
- [x] Eval definitions list is empty → no eval check performed, gate proceeds to autonomy checks
- [x] Eval definition with no `node_id` (pipeline-level) → not loaded by executor for eval-before-interrupt
- [x] Block failure publishes `run_failed` broker event
- [ ] Block failure recorded in audit event — not yet implemented at executor level
- [x] `EvalSuiteBlockedError` is raised when suite fails threshold check

### Edge Cases
- [x] Suite-level `pass_threshold` check after run completion — suite passes, run stays "complete"
- [x] Suite-level `pass_threshold` check — suite fails, run transitions to "failed" with `error_code="eval_suite_blocked"`
- [x] No suite definitions with `pass_threshold` → no post-run check
- [x] Empty suite (no results) → passes at aggregate_score=1.0
- [x] Multiple suites with thresholds — first failing suite terminates check
- [x] Post-run suite check reads results from a fresh session after streaming session closes
- [x] Eval definitions loaded before capacity slot claim — stale if definition added during wait
- [x] Cancel during eval evaluation stops run with status "cancelled"

## Known Gaps

- [ ] PRD 8.17 specifies HITL gate `condition` as `{eval_id, threshold, operator}` referencing an eval definition. The code implements `eval_condition` with `{eval_name, threshold, operator}` — using `eval_name` (string name) instead of `eval_id` (UUID). BDD scenarios and step definitions use `eval_name`. This is a schema difference from the PRD but functionally equivalent.
- [ ] No integration test for the full eval-before-interrupt → suite check → eval_failed chain end-to-end.
- [x] **RESOLVED** (2026-07-03): `EvalSuiteBlockedError` dead-code loop in executor.py post-run suite check removed. `_check_eval_suites()` raises `EvalSuiteBlockedError` before returning non-passing results, so the iteration was unreachable.
- [x] **RESOLVED** (2026-07-06): `replace_pipeline_graph_endpoint` in pipelines.py does catch `ProgrammingError` at line 530 and returns 501. The product map entry was outdated.
- [ ] Empty string `""` as `condition` on `hitl_gate_config` is treated as a JMESPath expression rather than "no condition". `jmespath.compile("")` does not raise, and `.search(state)` returns `None` which is falsy, causing the gate to be skipped. The correct behaviour should treat empty string the same as `null`/absent — gate proceeds normally.
- [ ] Block failure audit event: when `EvalBlockedError` is raised during eval-before-interrupt (node_runner.py → executor.py:808-810), no audit event is written. The `_stream_graph` method publishes a broker event but does not call `append_audit_event`. The PRD §8.17 states block failures should be recorded as AuditEvents.
- [ ] No timeout on eval evaluation: the `EvalEngine.evaluate()` method has no configurable timeout for `llm_judge` evals. If the LLM call hangs, the entire gate node hangs indefinitely. The node-level `timeout` parameter on the gate node (`make_hitl_gate_fn`) is the configured timeout field but it's not plumbed through to eval evaluation.

## Error Handling

### ProgrammingError (missing DB table)
None of the pipeline CRUD routes in `pipelines.py` catch `ProgrammingError`. If the `pipeline_edges` table (which stores `condition_expression`) does not exist, the `replace_pipeline_graph_endpoint` will return a raw 500. Per AGENTS.md lessons-learned, all DB-backed routes should return 501 with a descriptive message.

### Eval-before-interrupt error propagation
Eval-before-interrupt errors follow this chain:
1. `EvalEngine.evaluate()` raises `EvalBlockedError` (eval_engine/__init__.py:186-187)
2. Propagates through `make_hitl_gate_fn` (node_runner.py) unhandled
3. Caught by `_stream_graph` at executor.py:808-810 — publishes `run_failed` broker event
4. Returns `("eval_failed", "eval_blocked", str(exc), None)`
5. Executor writes final status to DB at executor.py:622-634

### JMESPath condition expression errors
Invalid JMESPath in gate `condition`:
1. `jmespath.compile()` raises at node_runner.py:210-211
2. Caught by `except Exception`, logged with `_log.exception`
3. Re-raised as `ValueError`
4. Caught by executor.py:541-544 as `pipeline.execution_error`

### Condition empty string edge case
An empty string `""` for `condition` compiles as a valid JMESPath expression. `jmespath.compile("").search(state)` returns `None`, which `_is_truthy()` evaluates as falsy → gate is incorrectly skipped. This should be treated as "no condition" (same as `null`/absent).

## QA History

- 2026-07-03: Cross-cutting QA (index 116). Verified all behaviour checkboxes against code — marked 47/48 implemented checkboxes as `[x]`. Found `replace_pipeline_graph_endpoint` missing ProgrammingError catch. Found empty-string condition edge case in node_runner.py. Confirmed BDD step definitions exist at `test_conditional_hitl.py` and wire all 8 scenarios. Confirmed eval-reference condition mechanism (node_runner.py:248-279) is implemented since product map was written. Found no `audit_event` recording for block failures. Found no timeout mechanism on eval evaluation. Status: partial (6 known gaps remain).
- 2026-07-06: Quick follow-up QA. Confirmed `replace_pipeline_graph_endpoint` ProgrammingError catch is now in place (pipelines.py:530) — marked gap #4 resolved. Verified all 395 lines of unit tests in `test_conditional_transitions.py` pass. Created website docs stub at `Website/modulo-website/src/docs/evals/conditional-transitions.md`. Status: partial (5 known gaps remain).
