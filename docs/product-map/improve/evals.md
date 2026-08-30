---
id: feat-evals
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/evals.py
  - backend/src/modulo/core/eval_engine/__init__.py
  - backend/src/modulo/core/eval_engine/suite_run.py
  - backend/src/modulo/core/eval_engine/execute_suite_run.py
  - backend/src/modulo/core/eval_engine/coverage_gap.py
  - backend/src/modulo/core/eval_engine/regression.py
  - backend/src/modulo/api/routes/feedback.py
  - frontend/src/views/EvalEditorView.vue
  - frontend/src/views/EvalProposalsQueueView.vue
unit-tests:
  - backend/tests/unit/api/test_evals_endpoint.py
  - backend/tests/unit/api/test_evals_compare.py
  - backend/tests/unit/api/test_evals_coverage_gap.py
  - backend/tests/unit/api/test_eval_regression_alert.py
  - backend/tests/unit/api/test_eval_leaderboards.py
  - backend/tests/unit/core/test_eval_engine.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/core/test_eval_suite_phase1.py
  - backend/tests/unit/db/test_eval_suite_run.py
  - frontend/src/__tests__/EvalEditorView.spec.ts
  - frontend/src/__tests__/EvalProposalsQueueView.spec.ts
bdd:
  - backend/tests/bdd/features/eval/eval_run.feature
  - backend/tests/bdd/features/eval/eval_scorer.feature
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
  - backend/tests/bdd/features/evals/eval_block.feature
  - backend/tests/bdd/features/ui/eval_dashboard.feature
  - backend/tests/bdd/steps/test_eval.py
  - backend/tests/bdd/steps/test_eval_block_steps.py
depends-on: []
status: covered
---

# Evals

Evaluation definitions, the eval engine that scores node outputs, eval suites
with regression alerting, pipeline coverage gap analysis, leaderboards, and the
eval-proposals queue. An eval is a typed definition (`llm_judge`, `regex`,
`json_schema`, `custom_function`, or `human_set`) with a `failure_behaviour` of
`warn` or `block`; blocked evals raise `EvalBlockedError` and are the mechanism
engine-side guardrails build on (`feat-guardrails` depends on this engine).
Surfaces: `/evals/editor` and `/evals/proposals`.

## Behaviours

- [x] Five eval types: `llm_judge` (LLM-as-judge via ModelBackendHub),
      `regex` (pattern match against an output field), `json_schema` (validate
      output against JSON Schema), `custom_function` (user-defined function),
      and `human_set` (registered, versioned, human-authored eval sets — the
      deterministic trustworthy path)
- [x] Eval outputs are evaluated against delimited `---BEGIN/END EVALUATED
      CONTENT---` framing with a data-not-instructions guard instruction, a
      content-length cap, and an embedded-judge-injection + ReDoS guard on
      regex patterns
- [x] Each eval carries a configurable `failure_behaviour` (`warn` | `block`);
      a blocked eval raises `EvalBlockedError` which terminalizes the run as
      `eval_failed`, and eval-generated guardrail blocks surface in the run
      detail UI
- [x] Eval definitions are org-scoped admin CRUD (`POST/GET/PUT/DELETE
      /api/v1/evals`, `GET /api/v1/evals/{eval_id}`) with pagination and
      pipeline / eval_type filters, plus `POST /api/v1/evals/from-run` to
      author a definition from run data
- [x] Results are queryable per run (`GET /api/v1/runs/{run_id}/evals`) and
      comparable side-by-side between two runs (`POST /api/v1/evals/compare`)
- [x] Leaderboards aggregate pass/fail over a window grouped by pipeline, node,
      or agent (`GET /api/v1/evals/leaderboard`)
- [x] Coverage-gap analysis produces an eval coverage map for a pipeline
      (`GET /api/v1/evals/coverage`) and flags uncovered/gapping surfaces using
      divergence/threshold and minimum-runs parameters (`coverage_gap.py`)
- [x] Suite orchestration resolves an immutable baseline snapshot and a
      deterministic "latest completed same-tuple prior run" baseline, persists
      per-case outcomes into `eval_results` with a `suite_run_id` FK, and
      aggregates pass-rate per `eval_type` — never cross-combining raw scores
      across differing eval types (type-incorrect refusal)
- [x] Suite regression detection delegates to `detect_regressions` and routes
      comparison postings through the existing Notifier
      (`EVENT_EVAL_REGRESSION`); suite alerting is configurable
      (`PUT /api/v1/evals/suites/{suite_id}/alerting`, admin-only)
- [x] Proposal queue: eval-gap feedback records are listed as eval proposals
      (`GET /api/v1/feedback/proposals`) and a proposal can be published into a
      real eval definition (`POST /api/v1/feedback/proposals/{record_id}/
      publish`) — a non-eval-gap feedback record is refused — while the
      `/evals/proposals` view supports publish / dismiss
- [x] The `/evals/editor` view authors evals against a pipeline + node with a
      type selector, JSON config editor, pass threshold and failure-warn /
      failure-block thresholds, save / edit / delete

## Known Gaps

- **`llm_judge` is a soft signal, injection-prone by design** — the guarded
  delimiters reduce prompt-injection risk but the trustworthy path for
  deterministic gating is `human_set` / regex / json_schema.
- **No long-horizon eval-run scheduler in this surface** — suite execution is
  triggered/run via the suite machinery, not a standalone cron in the eval API.

## QA History

- 2026-08-30: **improve-architecture (product-map walk)** — new behaviour
  tracker for the registered `feat-evals` manifest feature (routes `/evals/editor`,
  `/evals/proposals`, previously absent from the feature graph). Behaviours
  verified against `api/routes/evals.py` + `feedback.py`, `core/eval_engine/*`
  (engine, suite-run, coverage gap, regression), the admin/eval/suite
  unit + integration suites, and the `eval/` + `evals/` BDD features. Status:
  covered.
