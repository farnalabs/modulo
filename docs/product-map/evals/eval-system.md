---
id: feat-evals-system
prd: 8.17
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
  - backend/tests/bdd/features/evals/eval_regex.feature
  - backend/tests/bdd/features/evals/eval_llm_judge.feature
  - backend/tests/bdd/features/evals/eval_block.feature
  - backend/tests/bdd/features/evals/conditional_hitl.feature
  - backend/tests/bdd/features/ui/eval_dashboard.feature
code:
  - backend/src/modulo/core/eval_engine/
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
- [x] Judge uses dedicated model_backend_id (not agent's own)

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
- [x] Pattern with user-controlled input → ReDoS risk (partially protected: `_MAX_REGEX_PATTERN_LENGTH=1000` and `_RE_NESTED_QUANTIFIER` detection for `(a+)+`/`(a*)*` patterns. `(a|b)+` still not caught.)

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
- [x] Block failure recorded in AuditEvent — pipeline executor (executor.py:445-458) calls `append_audit_event` with event_type="eval.blocked" when eval_blocked causes eval_failed status. Audit event written to `audit_event` table. (Fixed since index 118; executor.py now wires eval_blocked→audit_event.)

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
- [x] No evals configured on a node → no eval step (architectural — executor only evaluates nodes with eval definitions)
- [x] pass_threshold = 0 → always pass (including all-fail suite)
- [x] pass_threshold = 1 → only perfect scores pass
- [x] Eval on final node → post-node eval, no follow-on (architectural — eval runs after every node incl. final, then run completes)
- [x] Custom function registered after pipeline compiled → used (registry read from config at eval time — covered by test_function_registered_after_definition_still_used)
- [ ] Variant group runs with different evals → per-variant eval comparison
- [x] Empty suite → always pass (aggregate_score=0.0, total_evals=0 — score value is 0.0, suite still passes)

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

### Error Handling

- [x] Create eval definition: ProgrammingError → 501 Not Implemented
- [x] List eval definitions: ProgrammingError → 501 Not Implemented
- [x] Get eval definition: ProgrammingError → 501 Not Implemented
- [x] Update eval definition: ProgrammingError → 501 Not Implemented
- [x] Delete eval definition: ProgrammingError → 501 Not Implemented
- [x] List run eval results: ProgrammingError → 501 Not Implemented
- [x] Eval coverage: ProgrammingError → 501 Not Implemented
- [x] Create eval from run: ProgrammingError → 501 Not Implemented
- [x] Compare evals: ProgrammingError → 501 Not Implemented (NEWLY ADDED)
- [x] Unauthenticated requests → 401/403
- [x] Non-admin CRUD operations → 403 Forbidden
- [x] Unknown eval ID → 404 Not Found
- [x] Run not found (list_run_evals) → 404 Not Found
- [x] Pipeline not found (coverage) → 404 Not Found
- [x] Run not found (from-run) → 404 Not Found
- [x] Run A not found (compare) → 404 Not Found
- [x] Run B not found (compare) → 404 Not Found
- [x] All eval routes: SQLAlchemyError → 503 Service Unavailable
- [x] All eval routes: logging on DB error
- [x] Invalid eval type in create → 422

## Known Gaps

1. RESOLVED (partial, 2026-07): ReDoS protection — pattern length limit (1000 chars) and `_RE_NESTED_QUANTIFIER` detection for nested quantifier patterns (`(a+)+`, `(a*)*`) added to eval_engine. Caveat: `(a|b)+` still not caught by the detection regex — alternation-based ReDoS vectors remain exploitable.
2. Eval sandbox: custom functions run in-process (sandboxed environment not
   wired). No side-effect isolation, no timeout enforcement.
3. No eval execution timeout per eval (per-node timeout exists but not
   per-eval).
4. Eval results stored but no trend tracking — regression detection
   endpoint `GET /api/v1/admin/evals/regressions` exists (delivered in
   feat-evals-eval-regression-alerts, index 98) but is not wired into
   dashboards or CI as a trend-over-time visualisation.
5. JMESPath condition syntax errors in conditional HITL gates — `== true`
   comparison was fixed (index 116), but other syntax errors still
   propagate as unhandled exceptions.
6. Conditional HITL condition referencing a nonexistent eval_id not
   gracefully handled.
7. JSON Schema `$ref` resolution could leak external resources — no
   allowlist or blocking of external schema references.
8. Nested JSON Schema validation depth limit not enforced — risk of
   stack overflow on deeply nested schemas.
9. No end-to-end integration test for eval blocking in pipelines — gate
   enforcement tested at BDD level (step defs) but no full pipeline
   run → eval → eval_failed lifecycle test.

## QA History

### 2026-08-15 — Coverage completion (FAR-232)

**What was fixed:**
- Marked [ ]→[x]: "No evals configured on a node → no eval step (architectural)" — the executor builds `eval_defs_by_node` and only evaluates nodes that have definitions; a node without evals has no eval step by construction.
- Marked [ ]→[x]: "Eval on final node → post-node eval, no follow-on (architectural)" — eval runs after every node (including the final one) then the run terminalizes; no follow-on exists.
- Marked [ ]→[x]: "Custom function registered after pipeline compiled → used or cached?" — the `config["functions"]` registry is read at eval time (not cached at definition time), so a late-registered function is picked up. Added `test_function_registered_after_definition_still_used` in test_eval_engine.py (first eval fails with "not found", registering the function makes the next eval pass).
- Corrected the empty-suite `aggregate_score` claim to `0.0` (matches `evaluate_suite` — the suite still passes; 1.0 was a stale value already corrected in eval-engine.md).
- Confirmed the "Block failure recorded in AuditEvent" claim is accurate — executor.py writes `append_audit_event(event_type="eval.blocked")` on both the execute and resume finalization paths (executor.py:1394-1409, 1876-1891).

**Test results:** All eval engine unit tests pass (test_eval_engine.py, test_eval_suite.py, test_eval_regressions.py). BDD eval features run via step-def files (test_eval.py / test_eval_block_steps.py) — pre-existing failures in those files are unrelated to this work (no diff vs origin/main) and are reported separately.
**Status:** partial (unchecked items are genuine gaps: custom-function sandbox, conditional-HITL syntax/nonexistent-field handling, JSON Schema $ref/depth, delete-blocking question, per-variant comparison).

### 2026-07-04 — Cross-cutting QA (index 118)
- **Fixed**: Added ProgrammingError→501 catch to `compare_evals` endpoint (was missing, could crash on un-migrated DB)
- **Added**: Error Handling section with 18 behaviour checkboxes covering all error paths
- **Verified**: All 8 DB-accessing route handlers now have ProgrammingError→501 catches
- **Updated**: Known gaps to reflect sub-feature QA work (eval-regression-alerts endpoint exists, JMESPath `== true` fixed)
- **Status**: partial

### 2026-07-08 — Cross-cutting QA (index 331)
- **Marked [x]**: "Block failure recorded in AuditEvent" — confirmed wired in executor.py:445-458 via `append_audit_event(event_type="eval.blocked")`. Previous note claiming "does NOT call append_audit_event" was outdated.
- **Marked [x]**: "Pattern with user-controlled input → ReDoS risk" — basic protection exists (length limit + nested quantifier detection). Remains partial (alternation-based ReDoS not caught).
- **Updated**: Known Gap #1 marked "RESOLVED (partial)" with caveat about `(a|b)+` gap.
- **Fixed**: Duplicate `code:` entry for evals.py removed from frontmatter.
- **Verified**: `compare_evals` results_a/results_b queries ARE inside `async with session.begin():` — no scope bug.
- **Unchanged**: 13 remaining `[ ]` checkboxes across eval definitions, custom_function sandbox, conditional HITL edge cases, JSON Schema $ref/depth, and architectural edge cases are still valid gaps.
- **Unchanged**: Known Gaps #2–9 remain open (sandbox, timeout, trend dashboard, JMESPath errors, $ref, depth, e2e test).
