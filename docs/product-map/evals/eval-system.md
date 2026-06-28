---
id: feat-evals-system
prd: 8.17
delivery-tasks: - task-nv2-eval-definition - task-nv2-eval-engine - task-nv2-eval-llm-judge - task-nv2-eval-regex-schema - task-nv2-eval-custom-function - task-nv2-conditional-hitl - task-nv2-eval-gate-enforcement - task-nv2-eval-packaging
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
  - backend/tests/bdd/features/eval/eval_scorer.feature
  - backend/tests/bdd/features/eval/eval_run.feature
  - backend/tests/bdd/features/eval/feedback_system.feature
code:
  - backend/src/modulo/core/eval_engine/
  - backend/src/modulo/db/crud/eval_definition.py
unit-tests: [backend/tests/unit/core/test_eval_engine.py]
depends-on: [feat-core-agent-model, feat-core-schema-system]
status: partial
---
# Eval System Evaluates agent outputs at configurable gates. Four eval types: llm_judge, regex
json_schema, custom_function. Each eval can block or warn on failure. ## Behaviours ### Eval Definitions CRUD
- [x] Create eval of each type (llm_judge, regex, json_schema, custom_function)
- [x] Get / List / Update / Delete evals
- [ ] Create with invalid type → 422
- [ ] Delete eval referenced by active pipeline → blocked? (soft-delete only)
- [x] Update pass_threshold
- [x] Assign eval to suite ### Eval Engine — llm_judge
- [x] Rubric prompt produces score 0-1
- [ ] Score ≤ pass_threshold → fail
- [ ] LLM judge returns non-numeric score → handled gracefully
- [ ] Judge model backend unavailable → error propagated as eval failure
- [ ] Rubric with no criteria → default rubric or error?
- [ ] Judge can access output but NOT the full run context (security: eval injection surface) ### Eval Engine — regex
- [x] Pattern matches → pass
- [x] Pattern doesn't match → fail
- [ ] Invalid regex pattern → 400 at eval creation, not at run-time
- [ ] Case-sensitive / case-insensitive toggle works
- [ ] Multi-line flag works
- [ ] Pattern with user-controlled input → ReDoS risk (should limit pattern complexity) ### Eval Engine — json_schema
- [x] Output validates against JSON Schema → pass
- [x] Output doesn't validate → fail, detail messages
- [ ] Schema uses $ref or external references → resolved or blocked?
- [ ] Output contains extra fields → fail unless additionalProperties: true
- [ ] Nested schema validation depth limit ### Eval Engine — custom_function
- [ ] User-defined function executes in isolated sandbox
- [ ] Function returns score 0-1 → pass/fail evaluated against threshold
- [ ] Function raises exception → eval fail, not agent crash
- [ ] Function has side effects → sandbox prevents
- [ ] Module not found → clear error message
- [ ] Function timeout → enforced, sandbox killed ### Eval Gates
- [x] block eval fails → run status = eval_failed, output NOT promoted
- [x] warn eval fails → output promoted, warning recorded
- [x] Multiple evals on same node → all evaluated, AND logic
- [ ] Mix of block and warn on same node → block takes precedence
- [ ] Eval gate runs AFTER node execution, BEFORE next node ### Conditional HITL
- [x] Condition expression (JMESPath) evaluated against eval results
- [x] Condition false → gate skipped, execution continues
- [x] Condition true → gate enforced, execution halts
- [ ] Condition syntax error → eval failure? gate default?
- [ ] Condition references nonexistent eval field → graceful handling ### Edge Cases
- [ ] All evals pass on a node → no eval_failed status
- [ ] No evals configured on a node → no eval step
- [ ] pass_threshold = 0 → always pass (except NaN)
- [ ] pass_threshold = 1 → only perfect scores pass
- [ ] Eval on final node → post-node eval, no follow-on
- [ ] Custom function registered after pipeline compiled → used or cached?
- [ ] Variant group runs with different evals → per-variant eval comparison ## Known Gaps
- ReDoS protection: regex eval with user-influenced patterns
- Eval sandbox: custom functions run in-process (sandboxed environment not wired)
- No eval execution timeout per eval (per-node timeout exists but not per-eval)
- Eval results stored but no trend tracking / regression detection
