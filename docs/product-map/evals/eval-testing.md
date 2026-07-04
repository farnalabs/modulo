---
id: feat-evals-eval-testing
prd: 8.17
delivery-tasks: [task-nv2-eval-bdd-tests]
bdd:
  - backend/tests/features/evals/eval_regex.feature
  - backend/tests/features/evals/eval_llm_judge.feature
  - backend/tests/features/evals/eval_block.feature
  - backend/tests/bdd/features/eval/eval_run.feature
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
  - backend/tests/bdd/features/eval/eval_scorer.feature
  - backend/tests/bdd/features/ui/eval_dashboard.feature
  - backend/tests/bdd/features/eval/feedback_system.feature
  - backend/tests/bdd/features/eval/conditional_hitl.feature
code:
  - backend/src/modulo/core/eval_engine/
  - backend/src/modulo/api/routes/admin.py
unit-tests:
  - backend/tests/unit/api/test_evals_endpoint.py
  - backend/tests/unit/api/test_evals_dashboard.py
  - backend/tests/unit/api/test_evals_compare.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/core/test_eval_regressions.py
  - backend/tests/unit/core/test_eval_engine.py
  - backend/tests/unit/api/test_evals_programming_error.py
  - backend/tests/unit/api/test_evals_compare_programming_error.py
  - backend/tests/unit/api/test_evals_okr_progress_programming_error.py
  - backend/tests/unit/api/test_evals_admin_programming_error.py
  - backend/tests/bdd/steps/test_eval.py
  - backend/tests/bdd/steps/test_eval_block_steps.py
depends-on: [feat-evals-eval-engine, feat-evals-eval-gates]
status: partial
---

# Eval Testing

Discovered from 1 completed delivery task (task-nv2-eval-bdd-tests). Tests validate the Eval System (8.17) across BDD acceptance and unit levels, covering eval definitions, run lifecycle, regex/LLM-judge/block eval types, suite aggregation, dashboard, comparison, coverage, regression alerts, and gate enforcement.

## Behaviours

### Eval definition CRUD
- [x] Create eval definition with all fields (pipeline_id, name, eval_type, config_json, failure_behaviour, pass_threshold, suite_id) returns 201
- [x] Create eval definition with optional fields omitted returns 201 with null defaults
- [x] Create eval definition requires admin role (runner gets 403)
- [x] Create eval definition with invalid eval_type returns 422
- [x] List eval definitions returns paginated results with total, items, page, page_size
- [x] List eval definitions filtered by pipeline_id
- [x] List eval definitions empty returns total 0, items []
- [x] Get eval definition by ID returns 200
- [x] Get eval definition returns 404 for unknown ID
- [x] Update eval definition returns 200 with updated fields
- [x] Update eval definition returns 404 for unknown ID
- [x] Update eval definition requires admin role
- [x] Delete eval definition returns 204
- [x] Delete eval definition returns 404 for unknown ID
- [x] Delete eval definition requires admin role
- [x] Unauthenticated requests return 401/403 on all CRUD endpoints

### Eval run lifecycle
- [x] Trigger eval run (POST /api/pipelines/{name}/evals) returns 202 with status "pending"
- [x] Eval run scores each test case individually
- [x] Eval run computes aggregate score from per-case scores
- [x] Eval run below pass_threshold fails with status "failed"
- [ ] Eval results page shows per-case scores and aggregate score

### Regex eval
- [x] Regex pattern matches output field → passed=true, score=1.0
- [x] Regex pattern does not match → passed=false, score=0.0
- [x] Regex eval on a nested JSON field
- [x] Regex eval with block behaviour on match raises EvalBlockedError
- [x] Regex eval with warn behaviour on match logs warning and continues
- [x] Regex eval with missing config returns passed=false, score=0.0
- [x] Regex pattern matches anywhere in the field value (substring, not full match)
- [x] Regex eval field coerced to string when input is numeric

### LLM judge eval
- [x] Rubric-based scoring passes above pass_threshold
- [x] Score below pass_threshold fails the eval
- [x] LLM judge uses dedicated model_backend_id (not the agent's own)
- [x] Custom rubric_prompt is sent to the judge model
- [x] LLM judge with no callable configured returns passed=false, score=0.0
- [x] LLM judge block behaviour stops pipeline: EvalBlockedError raised, run transitions to eval_failed

### Eval gate enforcement
- [x] Block behaviour raises EvalBlockedError with eval detail
- [x] Block behaviour transitions run to eval_failed status with error_code eval_blocked
- [x] Warn behaviour logs warning and does not halt pipeline
- [x] Suite-level pass_threshold blocks run on aggregate failure (eval_suite_blocked)
- [x] Suite-level pass_threshold passes on aggregate success
- [ ] Block failure recorded in AuditEvent with type eval_blocked — not wired to AuditEvent DB table (BDD step defs are stubs)
- [x] Multiple evals on one node: first failure blocks remaining evals, EvalBlockedError raised

### Eval suite aggregation
- [x] Suite with aggregate score >= threshold passes
- [x] Suite with aggregate score == threshold passes
- [x] Suite with aggregate score < threshold fails
- [x] Suite without pass_threshold always passes
- [x] Suite with mixed pass/fail results returns correct counts and blocking_failures list
- [x] Empty suite always passes (aggregate_score=1.0, total_evals=0)
- [x] SuiteEvalResult exposes all model fields: suite_id, total_evals, passed_evals, aggregate_score, passed, blocking_failures

### Eval dashboard
- [x] GET /api/v1/admin/evals/dashboard returns 200 for admin
- [x] Dashboard requires admin role (operator and runner get 403)
- [x] Unauthenticated returns 401
- [x] Summary section: total_results, passed, failed, pass_rate, total_definitions
- [x] Trend section: bucketed daily totals with pass/fail counts
- [x] By-type breakdown: totals per eval_type (llm_judge, regex, json_schema)
- [x] Coverage gaps: nodes without eval definitions listed with pipeline_id, pipeline_name, node_id
- [x] Coverage gaps empty when all nodes have eval definitions
- [x] Recent results section with eval_name, eval_type, passed, score
- [x] All five response keys present: summary, trend, by_type, coverage_gaps, recent_results
- [x] Empty database returns zeroed-out response (pass_rate=0.0, empty arrays)

### Eval compare
- [x] POST /api/v1/evals/compare returns side-by-side eval results with delta
- [x] Run not found returns 404
- [x] Compare with no shared results returns empty results list

### Eval coverage
- [x] GET /api/v1/evals/coverage returns node-level eval coverage with has_evals flag per node, covered/uncovered counts, coverage_pct
- [x] Pipeline not found returns 404
- [x] Empty pipeline (no nodes) returns zero coverage

### Eval from-run (eval proposal creation from run output)
- [x] POST /api/v1/evals/from-run returns 201 with sample_output and config_json
- [x] Requires admin role
- [x] Run not found returns 404
- [x] Prepopulates config_json by eval type (regex gets field, json_schema gets field, etc.)

### Eval regression alerts
- [x] No results returns empty alerts list
- [x] Declining pass rate (drop above threshold) triggers alert with trend "declining", drop_pct, affected_run_ids
- [x] Improving pass rate still returned with trend "improving"
- [x] Stable pass rate returned with trend "stable"
- [x] Drop below configurable threshold returns trend "stable"
- [x] No baseline data (baseline_total=0) → alert skipped
- [x] No recent data (recent_total=0) → alert skipped
- [x] Multiple evals with mixed trends each returned correctly
- [x] Affected_run_ids empty when no recent failures
- [x] GET /api/v1/admin/evals/regressions returns expected shape: alerts, total_regressions, threshold, lookback_days
- [x] Custom days and threshold query parameters accepted
- [x] Empty database returns total_regressions 0, alerts []

### Error Handling
- [x] All 9 CRUD routes in evals.py catch ProgrammingError→501
- [x] Compare endpoint catches ProgrammingError in both session blocks
- [x] OKR progress endpoint catches ProgrammingError→501
- [x] Eval dashboard endpoint catches ProgrammingError→501 (admin.py)
- [x] Eval regressions endpoint catches ProgrammingError→501 (admin.py)
- [x] Engine-internal errors (invalid regex, custom function exception, LLM judge failure) return failed EvalResult, not 500
- [x] ContentTooLongError caught in LLM judge → returned as failed EvalResult
- [x] Missing eval type raises ValueError (propagated as 500 — deferred fix)
- [x] Unauthenticated requests return 401 on all eval endpoints
- [x] Non-admin users receive 403 on eval CRUD/dashboard/regressions/from-run

### Edge Cases
- [x] Empty eval definitions list returns total:0, items:[]
- [x] Empty dashboard returns zeroed-out response
- [x] Empty eval suite always passes (aggregate_score=1.0)
- [x] Suite without pass_threshold always passes
- [x] Drop below configurable threshold returns trend "stable"
- [x] No baseline data → alert skipped
- [x] No recent data → alert skipped
- [x] Compare with no shared results returns empty list
- [x] Empty pipeline returns zero coverage
- [x] Suite not found in DB returns 404
- [x] Suite_id with special characters — endpoint handles encoding

## Known Gaps
- **eval_scorer.feature is a placeholder**: 6 abstract scenarios — not executable without concrete step values.
- **eval_dashboard.feature is a placeholder**: 4 UI scenarios with no concrete selectors, routes, or assertions.
- **eval_run.feature "results UI" scenario is a placeholder**: navigation step references eval_dashboard which is stub-only.
- **No dashboard regression alert unit test**: 2 ProgrammingError→501 catches added at index 119 but no corresponding unit test for eval_dashboard or eval_regressions.
- **Missing eval_type ValueError propagates as 500**: unknown eval type raises ValueError which is not caught at the route handler level, producing a raw 500 traceback instead of a structured 422.
- **No integration test for eval blocking in pipelines**: eval gate enforcement tested at BDD level (eval_block.feature) but no end-to-end pipeline run → eval block → eval_failed lifecycle test.
