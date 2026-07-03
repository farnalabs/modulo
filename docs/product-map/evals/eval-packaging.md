---
id: feat-evals-eval-packaging
prd: 8.17
delivery-tasks: [task-nv2-eval-packaging]
bdd:
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
  - backend/tests/bdd/features/eval/eval_run.feature
code:
  - backend/src/modulo/core/eval_engine/__init__.py
  - backend/src/modulo/core/eval_engine/okr.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/db/models/eval_definition.py
  - backend/src/modulo/api/routes/evals.py
unit-tests:
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/core/test_okr_progress.py
  - backend/tests/unit/api/test_evals_programming_error.py
  - backend/tests/unit/api/test_evals_okr_progress_programming_error.py
depends-on: [feat-evals-eval-definitions]
status: partial
---

# Eval Packaging

Grouping eval definitions into suites with configurable pass thresholds, including post-run suite-level aggregation and threshold enforcement.

## Behaviours

### Eval Suite Grouping
- [x] Assign evals to suites via suite_id on creation or update
- [x] List evals by suite_id
- [x] Multiple evals can share the same suite_id
- [x] Suite_id is a string field — no dedicated suite entity
- [x] Migration adds suite_id and pass_threshold columns (0023_eval_suite_threshold)

### Suite-Level Pass Threshold
- [x] pass_threshold stored on each eval definition in a suite
- [x] Suite aggregate score = passed_evals / total_evals
- [x] Suite passes when aggregate_score >= pass_threshold
- [x] Suite fails when aggregate_score < pass_threshold
- [x] Suite without pass_threshold always passes
- [x] Empty suite (no results) passes with aggregate_score=1.0
- [x] threshold = 0 — always passes
- [x] threshold = 1 — only perfect scores pass

### Post-Run Suite Enforcement
- [x] Completed run triggers _check_eval_suites()
- [x] Distinct suites with pass_threshold are queried per pipeline
- [x] Suite below threshold — run status = "failed", error_code = "eval_suite_blocked"
- [x] Suite passes — run stays "complete"
- [x] First failing suite terminates the check (no further suites evaluated)
- [x] No suite definitions with threshold — no post-run check
- [x] EvalSuiteBlockedError includes suite_id, score, and threshold
- [x] SuiteEvalResult exposes suite_id, aggregate_score, passed, blocking_failures

### Suite-Level OKR Tracking
- [x] track_okr_progress() buckets pass rates: 7d, 14d, 30d, overall
- [x] Trend direction detection (declining / stable / improving)
- [x] Breach alert when current_pass_rate < pass_threshold
- [x] Admin endpoint: GET /admin/evals/okr-progress/{suite_id}

### Edge Cases
- [x] Suite mixing block and warn evals — block failures are still counted in aggregate
- [ ] Suite with no evals matching a node — zero results for that node
- [ ] Cross-pipeline suites — suite_id isn't scoped to pipeline
- [ ] Deletion of last eval in a suite — suite conceptually disappears
- [ ] Reactor: suite_id on eval is just a string — no FK, no orphan protection

### BDD
- [x] eval_suite_crud.feature — 8 real CRUD scenarios (create, list, get, update, delete) with auth checks
- [ ] eval_run.feature — "Eval run below threshold fails" scenario tests suite threshold

### Error Handling
- [x] POST /evals returns 501 Not Implemented on ProgrammingError (missing DB table)
- [x] GET /evals returns 501 Not Implemented on ProgrammingError
- [x] GET /evals/{eval_id} returns 501 Not Implemented on ProgrammingError
- [x] PUT /evals/{eval_id} returns 501 Not Implemented on ProgrammingError
- [x] DELETE /evals/{eval_id} returns 501 Not Implemented on ProgrammingError
- [x] GET /evals/coverage returns 501 Not Implemented on ProgrammingError
- [x] GET /runs/{run_id}/evals returns 501 Not Implemented on ProgrammingError
- [x] POST /evals/from-run returns 501 Not Implemented on ProgrammingError
- [x] GET /admin/evals/okr-progress/{suite_id} returns 501 Not Implemented on ProgrammingError
- [x] GET /admin/evals/okr-progress/{suite_id} returns 404 on non-existent suite
- [x] All CRUD routes return 403 for non-admin users (existing)
- [x] All CRUD routes return 404 for non-existent entities (existing)

## Known Gaps
- No dedicated suite entity (suite_id is a free-form string, no FK, no metadata)
- No suite creation/management UI
- eval_suite_crud.feature scenarios not wired to live pipeline (mocking only)
- No suite-level permission model (any admin can create/join any suite_id)
- Suite_id not pipeline-scoped — cross-pipeline query semantics undefined
- Deleting the last eval in a suite leaves suite_id orphaned with no cleanup
- No website docs page for eval-packaging (needs creation in Website repo)
