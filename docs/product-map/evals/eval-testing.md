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
code:
  - backend/src/modulo/core/eval_engine/
  - backend/src/modulo/api/admin/evals/
unit-tests:
  - backend/tests/unit/api/test_evals_endpoint.py
  - backend/tests/unit/api/test_evals_dashboard.py
  - backend/tests/unit/api/test_evals_compare.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/core/test_eval_regressions.py
  - backend/tests/bdd/steps/test_eval.py
depends-on: [feat-evals-eval-engine, feat-evals-eval-gates]
status: partial
---

# Eval Testing

Discovered from 1 completed delivery task (task-nv2-eval-bdd-tests). Tests validate the Eval System (8.17) across BDD acceptance and unit levels, covering eval definitions, run lifecycle, regex/LLM-judge/block eval types, suite aggregation, dashboard, comparison, coverage, regression alerts, and gate enforcement.

## Behaviours

### Eval definition CRUD
- [ ] Create eval definition with all fields (pipeline_id, name, eval_type, config_json, failure_behaviour, pass_threshold, suite_id) returns 201
- [ ] Create eval definition with optional fields omitted returns 201 with null defaults
- [ ] Create eval definition requires admin role (runner gets 403)
- [ ] Create eval definition with invalid eval_type returns 422
- [ ] List eval definitions returns paginated results with total, items, page, page_size
- [ ] List eval definitions filtered by pipeline_id
- [ ] List eval definitions empty returns total 0, items []
- [ ] Get eval definition by ID returns 200
- [ ] Get eval definition returns 404 for unknown ID
- [ ] Update eval definition returns 200 with updated fields
- [ ] Update eval definition returns 404 for unknown ID
- [ ] Update eval definition requires admin role
- [ ] Delete eval definition returns 204
- [ ] Delete eval definition returns 404 for unknown ID
- [ ] Delete eval definition requires admin role
- [ ] Unauthenticated requests return 401/403 on all CRUD endpoints

### Eval run lifecycle
- [ ] Trigger eval run (POST /api/pipelines/{name}/evals) returns 202 with status "pending"
- [ ] Eval run scores each test case individually
- [ ] Eval run computes aggregate score from per-case scores
- [ ] Eval run below pass_threshold fails with status "failed"
- [ ] Eval results page shows per-case scores and aggregate score

### Regex eval
- [ ] Regex pattern matches output field → passed=true, score=1.0
- [ ] Regex pattern does not match → passed=false, score=0.0
- [ ] Regex eval on a nested JSON field
- [ ] Regex eval with block behaviour on match raises EvalBlockedError
- [ ] Regex eval with warn behaviour on match logs warning and continues
- [ ] Regex eval with missing config returns passed=false, score=0.0
- [ ] Regex pattern matches anywhere in the field value (substring, not full match)
- [ ] Regex eval field coerced to string when input is numeric

### LLM judge eval
- [ ] Rubric-based scoring passes above pass_threshold
- [ ] Score below pass_threshold fails the eval
- [ ] LLM judge uses dedicated model_backend_id (not the agent's own)
- [ ] Custom rubric_prompt is sent to the judge model
- [ ] LLM judge with no callable configured returns passed=false, score=0.0
- [ ] LLM judge block behaviour stops pipeline: EvalBlockedError raised, run transitions to eval_failed

### Eval gate enforcement
- [ ] Block behaviour raises EvalBlockedError with eval detail
- [ ] Block behaviour transitions run to eval_failed status with error_code eval_blocked
- [ ] Warn behaviour logs warning and does not halt pipeline
- [ ] Suite-level pass_threshold blocks run on aggregate failure (eval_suite_blocked)
- [ ] Suite-level pass_threshold passes on aggregate success
- [ ] Block failure recorded in AuditEvent with type eval_blocked
- [ ] Multiple evals on one node: first failure blocks remaining evals, EvalBlockedError raised

### Eval suite aggregation
- [ ] Suite with aggregate score >= threshold passes
- [ ] Suite with aggregate score == threshold passes
- [ ] Suite with aggregate score < threshold fails
- [ ] Suite without pass_threshold always passes
- [ ] Suite with mixed pass/fail results returns correct counts and blocking_failures list
- [ ] Empty suite always passes (aggregate_score=1.0, total_evals=0)
- [ ] SuiteEvalResult exposes all model fields: suite_id, total_evals, passed_evals, aggregate_score, passed, blocking_failures

### Eval dashboard
- [ ] GET /api/v1/admin/evals/dashboard returns 200 for admin
- [ ] Dashboard requires admin role (operator and runner get 403)
- [ ] Unauthenticated returns 401
- [ ] Summary section: total_results, passed, failed, pass_rate, total_definitions
- [ ] Trend section: bucketed daily totals with pass/fail counts
- [ ] By-type breakdown: totals per eval_type (llm_judge, regex, json_schema)
- [ ] Coverage gaps: nodes without eval definitions listed with pipeline_id, pipeline_name, node_id
- [ ] Coverage gaps empty when all nodes have eval definitions
- [ ] Recent results section with eval_name, eval_type, passed, score
- [ ] All five response keys present: summary, trend, by_type, coverage_gaps, recent_results
- [ ] Empty database returns zeroed-out response (pass_rate=0.0, empty arrays)

### Eval compare
- [ ] POST /api/v1/evals/compare returns side-by-side eval results with delta
- [ ] Run not found returns 404
- [ ] Compare with no shared results returns empty results list

### Eval coverage
- [ ] GET /api/v1/evals/coverage returns node-level eval coverage with has_evals flag per node, covered/uncovered counts, coverage_pct
- [ ] Pipeline not found returns 404
- [ ] Empty pipeline (no nodes) returns zero coverage

### Eval from-run (eval proposal creation from run output)
- [ ] POST /api/v1/evals/from-run returns 201 with sample_output and config_json
- [ ] Requires admin role
- [ ] Run not found returns 404
- [ ] Prepopulates config_json by eval type (regex gets field, json_schema gets field, etc.)

### Eval regression alerts
- [ ] No results returns empty alerts list
- [ ] Declining pass rate (drop above threshold) triggers alert with trend "declining", drop_pct, affected_run_ids
- [ ] Improving pass rate still returned with trend "improving"
- [ ] Stable pass rate returned with trend "stable"
- [ ] Drop below configurable threshold returns trend "stable"
- [ ] No baseline data (baseline_total=0) → alert skipped
- [ ] No recent data (recent_total=0) → alert skipped
- [ ] Multiple evals with mixed trends each returned correctly
- [ ] Affected_run_ids empty when no recent failures
- [ ] GET /api/v1/admin/evals/regressions returns expected shape: alerts, total_regressions, threshold, lookback_days
- [ ] Custom days and threshold query parameters accepted
- [ ] Empty database returns total_regressions 0, alerts []

## Known Gaps
- **eval_suite_crud.feature is a placeholder**: 3 scenarios (create, provide config, persist) have stub step definitions in test_eval.py — no real BDD coverage for suite CRUD.
- **eval_scorer.feature is a placeholder**: multiple-scorer-type dispatch not implemented; step definitions are stubs.
- **eval_dashboard.feature is a placeholder**: frontend eval dashboard scenarios not yet written — no Playwright BDD coverage for the eval results UI beyond the backend steps in eval_run.feature.
- **No Python unit tests for eval_engine core logic**: the eval engine's per-type dispatch (regex, llm_judge, json_schema, custom_function) is tested only through BDD features, not standalone unit tests. test_eval_suite.py tests aggregation only, not individual eval type execution.
- **No regression benchmark suite**: detect_regressions is unit-tested but there is no CI regression benchmark that compares eval pass rates against a stored baseline.
- **No integration test for eval blocking in pipelines**: eval gate enforcement is tested at the BDD level (eval_block.feature step definitions) but there is no end-to-end integration test where a pipeline run encounters an eval block and transitions through the full eval_failed lifecycle. 