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
  - backend/src/modulo/core/eval_engine/policy_gate.py
  - backend/src/modulo/core/pipeline_engine/eval_persist_order.py
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
  - backend/tests/unit/core/test_eval_persist_order_failopen.py
  - backend/tests/unit/core/eval_engine/test_policy_gate_decision_row.py
  - backend/tests/unit/db/test_eval_suite_run.py
  - frontend/src/__tests__/EvalEditorView.spec.ts
  - frontend/src/__tests__/EvalProposalsQueueView.spec.ts
bdd:
  - backend/tests/bdd/features/eval/eval_run.feature
  - backend/tests/bdd/features/eval/eval_scorer.feature
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
  - backend/tests/bdd/features/evals/eval_block.feature
  - backend/tests/bdd/features/evals/eval_llm_judge.feature
  - backend/tests/bdd/features/evals/eval_regex.feature
  - backend/tests/bdd/steps/test_eval.py
  - backend/tests/bdd/steps/test_eval_block_steps.py
  - backend/tests/bdd/steps/test_eval_scorer_gates.py
depends-on: []
status: covered
---

# Evals

Evaluation definitions, the eval engine that scores node outputs, eval suites
with regression alerting, pipeline coverage gap analysis, leaderboards, and the
eval-proposals queue. An eval is a typed definition (`llm_judge`, `regex`,
`json_schema`, `custom_function`, or `human_set`) carrying an engine-internal
`failure_behaviour` of `warn` or `block` (retired from the public REST/MCP
surface + generated types on 2026-09-26 — FAR-1103 chunk 5a; it is no longer
writable/readable through the API or the generated frontend types); blocked
evals raise `EvalBlockedError` and are the mechanism engine-side guardrails
build on (`feat-guardrails` depends on this engine).
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
- [x] Each eval carries an internal-only `failure_behaviour` (`warn` | `block`);
      a blocked eval raises `EvalBlockedError` which terminalizes the run as
      `eval_failed`, and eval-generated guardrail blocks surface in the run
      detail UI
- [x] Policy-gate decision records (`PolicyGateDecision`, FAR-1060 / FAR-1102):
      the eval loop persists one decision-record row per policy-gate evaluation,
      built by `build_decision_row` (`core/eval_engine/policy_gate.py`) with the
      six payload columns `resolved_action` (`continue`|`warn`|`block`) /
      `error_detail` / `node_id` / `eval_result_id` / `run_id` /
      `policy_gate_version`; writes go fail-open through
      `eval_persist_order.py` (transient vs referential failure classified,
      the run continues when persistence fails) and a purged guardrail eval whose
      decision rows still reference it hard-deletes to 409 (RESTRICT FK mapped
      to 409); org-deletion purges decision rows child-most
      (`test_policy_gate_decision_row.py`, `test_eval_persist_order_failopen.py`,
      `test_admin_housekeeping_decision_block.py`)
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
      type selector, JSON config editor, and pass threshold, save / edit /
      delete

## Known Gaps

- **`llm_judge` is a soft signal, injection-prone by design** — the guarded
  delimiters reduce prompt-injection risk but the trustworthy path for
  deterministic gating is `human_set` / regex / json_schema.
- **No long-horizon eval-run scheduler in this surface** — suite execution is
  triggered/run via the suite machinery, not a standalone cron in the eval API.

## QA History
- 2026-09-28: **Improve Architecture product-map walk** — archived the stale
  `ui/eval_dashboard.feature` UI-journey BDD draft (pinned `@awaiting-implementation`
  since 2026-08, never ran). Its steps referenced testids that exist nowhere in the
  frontend (`eval-result-item`, `eval-results-list`, `filter-failed`,
  `compare-run-checkbox`, `compare-button`, `eval-comparison`, `empty-state`,
  `theme-toggle`, `node-output`, `canvas-node`, `approval-banner`, ... — verified
  0 hits across `frontend/src`), navigated to fictional run ids, and described a
  surface the product maps to different real testids. The executing BDD citations
  (`eval/` + `evals/` features) and the real UI-journey Playwright coverage
  (`frontend/tests/e2e/evals.spec.ts`, `eval-pages-empty-states.spec.ts`,
  `journeys/eval-journey.spec.ts`) are unaffected; this tracker now cites only
  executing BDD features (guarded by
  `test_no_bdd_citations_for_fully_deselected_features`).
- 2026-09-26: **Improve Architecture product-map walk** — reconciled the
  tracker with the FAR-1103 chunk 5a retirement: `failure_behaviour` was
  removed from the public surface (REST payloads + MCP params + generated
  frontend types) and is now engine-internal only (verified against
  `api/routes/evals.py` "public fields only, failure_behaviour excluded" and
  the absence of the field in `frontend/src/types/`); the `/evals/editor`
  behaviour dropped the "failure-warn / failure-block thresholds" the retired
  UI had removed along with the `eval-editor-failure-*` elements. Tracked the
  FAR-1102/1060 `PolicyGateDecision` surface (write + purge + delete-block)
  that shipped without a product-map home.
- 2026-09-25: **Improve Architecture product-map walk** — closed the stale
  `feat-evals` registry gap: manifest now `status: covered` and ticks the
  comparison surface that #972 had left unchecked — `GET /api/v1/runs/{run_id}/evals`
  + `POST /api/v1/evals/compare` (verified against `test_evals_compare.py`) and the
  variant eval-score/prompt-diff/coverage comparison surfaces owned by
  `feat-variants`. The genuinely missing per-token breakdown sub-surface stays
  tracked under `feat-variants`.
- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/evals/editor`, `/evals/proposals`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/evals/proposals`: the whole-page view(s) `EvalProposalsQueueView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/evals/editor`: the whole-page view(s) `EvalEditorView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-30: **product-map review pass** — closed the
  "no executing BDD surface for the `llm_judge` / `regex` scorer types" gap:
  `evals/eval_llm_judge.feature` and `evals/eval_regex.feature` now execute
  through the new step module `tests/bdd/steps/test_eval_scorer_gates.py`
  (14 scenarios, registered in CI alongside the repaired step-rot modules).
  The steps drive the real `EvalEngine` so they lock the scorer contracts:
  regex scoring against an output field (incl. numeric coercion and nested
  patterns), `warn` vs `block` internal `failure_behaviour`
  (`EvalBlockedError` + `eval_failed` run transition), the llm_judge callable
  wiring (pass-below-threshold verdicts, no-callable fail path), a dedicated
  judge `model_backend_id`, and the guarded rubric prompt with the
  data-not-instructions delimiter wrapping.

- 2026-08-30: **product-map review pass** — new behaviour
  tracker for the registered `feat-evals` manifest feature (routes `/evals/editor`,
  `/evals/proposals`, previously absent from the feature graph). Behaviours
  verified against `api/routes/evals.py` + `feedback.py`, `core/eval_engine/*`
  (engine, suite-run, coverage gap, regression), the admin/eval/suite
  unit + integration suites, and the `eval/` + `evals/` BDD features. Status:
  covered.
