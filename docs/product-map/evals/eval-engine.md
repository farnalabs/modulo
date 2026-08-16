---
id: feat-evals-eval-engine
prd: 8.17
delivery-tasks: [task-nv2-eval-custom-function, task-nv2-eval-engine, task-nv2-eval-llm-judge, task-nv2-eval-regex-schema]
bdd:
  - backend/tests/bdd/features/evals/eval_regex.feature
  - backend/tests/bdd/features/evals/eval_llm_judge.feature
  - backend/tests/bdd/features/evals/eval_block.feature
  - backend/tests/bdd/features/evals/conditional_hitl.feature
code:
  - backend/src/modulo/core/eval_engine/__init__.py
  - backend/src/modulo/core/eval_engine/regression.py
  - backend/src/modulo/core/eval_engine/okr.py
unit-tests:
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/core/test_eval_regressions.py
  - backend/tests/unit/core/test_eval_engine.py
  - backend/tests/unit/api/test_evals_endpoint.py
depends-on: [feat-evals-eval-definitions]
status: partial
---

# Eval Engine

Core eval engine that evaluates node outputs against eval definitions. Supports four eval types, suite-level aggregation with pass_threshold, and two failure behaviours (warn/block). Includes regression detection and OKR-aligned progress tracking.

## Behaviours

### Happy paths

- [x] Regex eval matches output field — passed=true, score=1.0
- [x] Regex eval on nested field via config.field
- [x] Regex eval field is coerced to string (non-string output like numeric)
- [x] Regex pattern matches anywhere in the field value (not just anchored)
- [x] LLM judge returns score above pass_threshold — passed=true
- [x] LLM judge returns score below pass_threshold — passed=false
- [x] LLM judge uses dedicated model_backend_id (not agent's own)
- [x] JSON Schema validation passes — passed=true, score=1.0
- [x] JSON Schema validation fails — passed=false, score=0.0
- [x] Custom function returns passed=true — eval passes
- [x] Custom function returns passed=false — eval fails
- [x] Suite aggregation: all evals pass — suite passes
- [x] Suite aggregation: aggregate score at or above pass_threshold — suite passes
- [x] Suite aggregation: aggregate score below pass_threshold — suite blocks
- [x] Suite with no pass_threshold never blocks (always passes)
- [x] Empty suite always passes (aggregate_score=0.0, total=0)
- [x] Eval with failure_behaviour="warn" logs warning on failure, continues
- [x] Eval with failure_behaviour="block" raises EvalBlockedError on failure
- [x] Block failure transitions run to eval_failed terminal state
- [x] Standalone evaluate (no persisted EvalDefinition) for Feedback System
- [x] Regression detection: identifies pass-rate decline between baseline and recent windows
- [x] Regression detection: skips evals with no baseline or no recent data
- [x] OKR progress tracking: pass rates per time window (7d, 14d, 30d, overall)
- [x] OKR breach detection: flags when current pass rate falls below threshold

### Error states
- [x] Regex eval missing "pattern" in config — passed=false, detail describes issue
- [x] Regex eval missing "field" in config — passed=false, detail describes issue
- [x] Regex eval pattern exceeds max length (1000) — passed=false, detail describes issue
- [x] Regex eval nested quantifier (ReDoS) pattern rejected — passed=false, detail describes issue
- [x] Regex eval invalid pattern (regex compile error) — passed=false, detail describes issue
- [x] LLM judge callable not provided — passed=false, score=0.0
- [x] LLM judge callable raises exception — caught gracefully, passed=false
- [x] LLM judge callable returns non-dict — caught gracefully, passed=false
- [x] Custom function name not found in registry — passed=false, detail describes issue
- [x] Custom function raises exception — caught gracefully, passed=false
- [x] Custom function returns non-dict — caught gracefully, passed=false
- [x] Custom function "functions" config is not a dict — handled gracefully, passed=false
- [x] Custom function missing "functions" config key — handled gracefully, passed=false
- [x] JSON Schema missing "schema" in config — passed=false, detail describes issue
- [x] JSON Schema field configured but not in output — passed=false, detail describes issue
- [x] JSON Schema malformed schema definition — caught SchemaError, passed=false
- [x] Unknown eval type — raises UnknownEvalTypeError (inherits ValueError)
- [x] Suite not found in DB — track_okr_progress raises ValueError
- [x] EvalBlockedError includes eval name and detail message
- [x] EvalSuiteBlockedError raised for suite-level threshold failure (in executor, not evaluate_suite)
- [x] Block failure written to AuditEvent with type eval_blocked — handled at executor level (_handle_node_eval in executor.py writes audit events for eval.blocked on both execute() and resume() paths)

### Edge cases
- [x] Suite with mixed pass/fail — correct counts and blocking_failures list
- [x] Suite with pass_threshold exactly at aggregate boundary (equal passes)
- [x] Regex eval with missing config — returns failed (graceful degradation)
- [x] Regex eval with empty config — returns failed (missing pattern)
- [x] Regex eval with None field value — coerced to empty string, match fails
- [x] Regex eval with unknown flag character — ignored with warning, eval proceeds
- [x] Regex eval with excessive pattern length (>1000) — rejected with detail
- [x] Regex eval with nested quantifier pattern — ReDoS protection rejects
- [x] Non-string output field coerced to string in regex eval
- [x] Multiple evals on one node: first block failure stops remaining evals
- [x] LLM judge block behaviour takes priority over HITL interrupt
- [x] Regression: evals with zero baseline_total or zero recent_total are skipped
- [x] Regression: drop below threshold classified as stable (not alerted)
- [x] OKR: target_date parsing failure returns None days_to_target
- [x] OKR: fewer than 2 periods with data → trend is stable
- [x] Empty results for regression API return zero alerts

### Concurrency
- [x] EvalEngine is stateless — safe for concurrent use
- [x] Each evaluate() call generates fresh run_id via uuid4()
- [x] Suite aggregation is pure function — no mutable shared state

### Security
- [x] LLM judge prompt treats agent output as untrusted (structural separators)
- [x] LLM judge uses independently configured model_backend_id
- [x] Custom functions looked up from explicit registry dict — no arbitrary imports
- [x] Create/update/delete eval definitions requires admin role (403 for runner)
- [x] Unauthenticated requests return 401 on all eval API endpoints
- [x] RLS scopes eval definitions and results by organisation_id

### Backward compatibility
- [x] standalone_evaluate provides non-persisted path for Feedback System (8.20)
- [x] SuiteEvalResult exposes all expected fields as public attributes
- [x] Regression alert shape matches API contract (eval_id, eval_name, pass rates, drop_pct, trend, affected_run_ids)
- [x] CRUD endpoints accept optional fields without requiring them

### Error Handling
- [x] Regression route catches ProgrammingError → 501
- [x] Regression route catches SQLAlchemyError → 503
- [x] Regression route catches TimeoutError → 503
- [x] Regression route catches generic Exception → 500
- [x] Non-admin user → 403
- [x] Unauthenticated request → 401

### Resilience & Integration Robustness
- [x] EvalEngine is stateless - safe for concurrent use
- [x] Each evaluate() call uses fresh uuid4 for run_id
- [x] Suite aggregation is a pure function - no mutable shared state
- [x] standalone_evaluate is @classmethod - supports subclassing
- [x] Regression query wrapped in timeout-guarded try/except chain
- [x] Regression query uses SQLAlchemy text() with bind params (no SQL injection)

## QA History
### 2026-08-15 — Coverage completion (FAR-231/FAR-233 distribute batch)

- Confirmed the three Known Gaps are genuine and accurately scoped: (1) eval definition CRUD UI — eval_dashboard.feature is a stub with no concrete selectors; (2) standalone_evaluate creates an ephemeral EvalDefinition per call — no eval-run lifecycle persistence for the standalone path; (3) "No eval run trigger via API" — re-verified that pipelines.py has NO `POST /api/pipelines/{name}/evals` route (the eval_run.feature "Trigger an eval run" scenario is step-stubbed only; this also corrected an overclaim in eval-testing.md). All 89 checked behaviours were spot-re-verified against eval_engine/__init__.py, regression.py, okr.py and the unit tests; no new [x] items this pass.

### 2026-08-15 — Coverage completion (FAR-232)

**What was fixed:**
- Resolved Known Gaps: (1) "Eval scorer dispatch (eval_scorer.feature is placeholder)" — FALSE; eval_scorer.feature has 7 real Gherkin scenarios wired with step definitions in test_eval.py. (2) "No eval results API endpoint for querying historical results" — FALSE; `GET /runs/{run_id}/evals` (list_run_evals) exists and is covered by the ProgrammingError→501 error-handling tests. (3) "LLM judge untrusted-output prompt enforcement is documented in PRD but not validated at the engine layer" — FALSE; the engine wraps output in structural separators + guard instruction (`_build_safe_judge_input`) and test_eval_judge_injection.py validates it (17 tests).
- Confirmed the "Block failure written to AuditEvent" claim — executor writes event_type="eval.blocked" on both execute and resume finalization paths.
- All unchecked items elsewhere in the map verified; remaining Known Gaps below are genuine.

**Test results:** All eval engine unit tests pass (test_eval_engine.py, test_eval_suite.py, test_eval_regressions.py).
**Status:** partial (remaining Known Gaps are genuine: CRUD UI, eval-run lifecycle persistence for standalone path, no eval-run trigger API).

## Known Gaps
- [ ] Eval definition CRUD UI (eval_dashboard.feature is placeholder)
- [ ] No eval run lifecycle persistence — standalone_evaluate creates ephemeral EvalDefinition per call
- [ ] No eval run trigger via API (eval_run.feature scenarios not fully wired to real endpoints)

Resolved items (removed 2026-08-15, see QA History): eval scorer dispatch (eval_scorer.feature has 7 real scenarios + step defs), eval results API endpoint (GET /runs/{run_id}/evals exists), LLM judge untrusted-output enforcement (structural separators + guard instruction, test_eval_judge_injection.py), eval suite CRUD feature, feedback system integration.
