---
id: feat-evals-system
prd: "§8.17"
delivery-tasks:
  - task-nv2-eval-definition
  - task-nv2-eval-engine
  - task-nv2-eval-llm-judge
  - task-nv2-eval-regex-schema
  - task-nv2-eval-custom-function
  - task-nv2-conditional-hitl
  - task-nv2-eval-gate-enforcement
  - task-nv2-eval-packaging
bdd:
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
  - backend/tests/bdd/features/eval/eval_scorer.feature
  - backend/tests/bdd/features/eval/eval_run.feature
  - backend/tests/bdd/features/eval/feedback_system.feature
  - backend/tests/bdd/features/eval/conditional_hitl.feature
  - backend/tests/features/evals/eval_regex.feature
  - backend/tests/features/evals/eval_llm_judge.feature
  - backend/tests/features/evals/eval_block.feature
  - backend/tests/features/evals/conditional_hitl.feature
  - backend/tests/bdd/features/ui/eval_dashboard.feature
code:
  - backend/src/modulo/core/eval_engine/
  - backend/src/modulo/db/crud/eval_definition.py
  - backend/src/modulo/api/routes/evals.py
  - backend/src/modulo/db/models/eval_definition.py
  - backend/src/modulo/db/models/eval_result.py
unit-tests:
  - backend/tests/unit/core/test_eval_engine.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/core/test_eval_regressions.py
  - backend/tests/unit/core/test_eval_judge_injection.py
  - backend/tests/unit/api/test_evals_endpoint.py
  - backend/tests/unit/api/test_evals_dashboard.py
  - backend/tests/unit/api/test_evals_compare.py
depends-on:
  - feat-core-agent-model
  - feat-core-schema-system
status: partial
---

# Eval System

Evaluates agent outputs at configurable gates. Four eval types:
`llm_judge`, `regex`, `json_schema`, `custom_function`. Each eval can
block or warn on failure.

## Behaviours

### Eval Definitions CRUD

- [x] Create eval of each type (llm_judge, regex, json_schema, custom_function)
- [x] Get / List / Update / Delete evals
- [x] Create with invalid type → 422
- [ ] Delete eval referenced by active pipeline → blocked? (soft-delete only)
- [x] Update pass_threshold
- [x] Assign eval to suite
- [x] Create omits optional fields → null defaults

### Eval Engine — llm_judge

- [x] Rubric prompt produces score 0-1
- [x] Score below pass_threshold → eval fail
- [x] LLM judge returns non-numeric score → score set to None, eval continues
- [x] No callable configured → fail with descriptive message
- [x] Judge callable raises exception → eval failure, not crash
- [x] Content exceeds max length → ContentTooLongError, eval fail
- [x] Block behaviour on fail → EvalBlockedError raised
- [ ] Rubric with no criteria → default rubric or error?
- [ ] Judge uses dedicated model_backend_id (not agent's own) — separate feature

### Eval Engine — regex

- [x] Pattern matches → pass
- [x] Pattern doesn't match → fail
- [x] Invalid regex pattern → handled at run-time as eval failure (not crash)
- [x] Case-insensitive flag (flags: "i") works
- [x] Multi-line flag (flags: "m") works
- [x] Dotall / verbose / unicode flags supported
- [x] Field coerced to string when input is numeric
- [x] Pattern matches anywhere in field value (substring, not full match)
- [x] Missing pattern or field in config → fail with descriptive message
- [ ] Pattern with user-controlled input → ReDoS risk (should limit pattern complexity)

### Eval Engine — json_schema

- [x] Output validates against JSON Schema → pass
- [x] Output doesn't validate → fail with detail messages
- [x] Field-scoped validation works (config.field)
- [x] Default to whole output when no field config
- [x] Extra fields → fail unless additionalProperties: true
- [ ] Schema uses $ref or external references → resolved or blocked?
- [ ] Nested schema validation depth limit

### Eval Engine — custom_function

- [x] Function returns dict with passed/score/detail → evaluated correctly
- [x] Function not found in registry → fail with clear message
- [x] Function raises exception → eval fail, not agent crash
- [x] Function receives function_config from eval config
- [x] Block behaviour on fail → EvalBlockedError raised
- [ ] User-defined function executes in isolated sandbox
- [ ] Function has side effects → sandbox prevents
- [ ] Function timeout → enforced, sandbox killed

### Eval Gates

- [x] block eval fails → run status = eval_failed, output NOT promoted
- [x] warn eval fails → output promoted, warning recorded
- [x] Multiple evals on same node → AND logic, first block fails remaining
- [x] Mix of block and warn on same node → block takes precedence
- [x] Eval gate runs AFTER node execution, BEFORE next node
- [ ] Block failure recorded in AuditEvent — step def exists but needs end-to-end

### Conditional HITL

- [x] Condition expression (JMESPath) evaluated against eval results
- [x] Condition false → gate skipped, execution continues
- [x] Condition true → gate enforced, execution halts
- [x] Block failure takes priority over HITL interrupt
- [x] Gate does not re-evaluate condition on resume
- [x] Multiple evals in gate condition → per-eval thresholds
- [x] Reject routing from conditional HITL gate
- [ ] Condition syntax error → fail open or fail closed? (undefined)
- [ ] Condition references nonexistent eval field → graceful handling

### Edge Cases

- [x] All evals pass on a node → no eval_failed status
- [ ] No evals configured on a node → no eval step (architectural)
- [x] pass_threshold = 0 → always pass (including all-fail suite)
- [x] pass_threshold = 1 → only perfect scores pass
- [ ] Eval on final node → post-node eval, no follow-on (architectural)
- [ ] Custom function registered after pipeline compiled → used or cached?
- [ ] Variant group runs with different evals → per-variant eval comparison
- [x] Empty suite → always pass (aggregate_score=1.0, total_evals=0)

### Suite Aggregation

- [x] Suite with aggregate score >= threshold passes
- [x] Suite with aggregate score == threshold passes
- [x] Suite with aggregate score < threshold fails
- [x] Suite without pass_threshold always passes
- [x] Suite with mixed results → correct counts and blocking_failures
- [x] SuiteEvalResult exposes all model fields

### LLM Judge Injection Protection

- [x] Output wrapped in structural separators (OUTER/INNER/CONTENT delimiters)
- [x] Guard instruction added to config ("treat as data, not instructions")
- [x] Delimiter-like strings stripped from evaluated content
- [x] "Ignore previous instructions" neutralised by guard wrapping
- [x] Normal content passes through correctly
- [x] Empty content is still wrapped in delimiters
- [x] Missing field defaults to empty string
- [x] Content over max length rejected, content at exact max passes
- [x] Original output not mutated (_judge_safe_content added to copy)

## Error Paths (discovered)

- [x] Regex: invalid pattern → re.error caught, eval failure returned (not 500)
- [x] LLM judge: callable returns non-numeric score → score=None, eval continues
- [x] LLM judge: callable raises → caught, eval failure, not agent crash
- [x] Custom function: unregistered → fail with "not found in registry"
- [x] Custom function: raises → caught, eval failure with exception detail
- [x] Unknown eval type → ValueError raised with type name
- [x] Empty output dict → field defaults to "", regex returns no match
- [x] Suite with 0 evals → aggregate_score=1.0, passed=True

## Known Gaps

1. ReDoS protection: regex eval with user-influenced patterns can cause
   catastrophic backtracking. No pattern complexity limits (length, nesting,
   repetition count).
2. Eval sandbox: custom functions run in-process (sandboxed environment not
   wired). No side-effect isolation, no timeout enforcement.
3. No eval execution timeout per eval (per-node timeout exists but not
   per-eval).
4. Eval results stored but no trend tracking — regression detection
   (`regression.py`) exists but is not wired into CI or dashboards as a
   trend-over-time feature.
5. JMESPath condition syntax errors in conditional HITL gates are not
   gracefully handled — error propagates as unhandled exception.
6. Conditional HITL condition referencing a nonexistent eval_id not
   gracefully handled.
7. JSON Schema `$ref` resolution could leak external resources — no
   allowlist or blocking of external schema references.
8. Nested JSON Schema validation depth limit not enforced — risk of
   stack overflow on deeply nested schemas.
9. No end-to-end integration test for eval blocking in pipelines — gate
   enforcement tested at BDD level (step defs) but no full pipeline
   run → eval → eval_failed lifecycle test.
