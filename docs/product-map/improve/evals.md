---
id: feat-evals
prd: 8.17, 8.20
adr: []
code:
  - backend/src/modulo/api/routes/evals.py
  - backend/src/modulo/api/routes/feedback.py
  - backend/src/modulo/core/eval_engine
unit-tests:
  - backend/tests/unit/api/test_evals_endpoint.py
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/core/test_eval_engine.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/core/test_eval_regressions.py
bdd:
  - backend/tests/bdd/features/evals
  - backend/tests/bdd/features/eval/eval_run.feature
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
  - backend/tests/bdd/features/eval/eval_scorer.feature
  - backend/tests/bdd/features/eval/feedback_system.feature
depends-on:
  - feat-runs
  - feat-feedback
status: covered
---

# Evaluation Editor & Proposals

Evaluation definition management (`/evals/editor`), run-level eval results, coverage
and regression views, and the eval **proposals queue** (`/evals/proposals`, served by
`/api/v1/feedback/proposals`) that promotes in-flow eval-gap detections into live eval
definitions (§8.20 "Eval suite growth"). Eval definitions target pipelines and nodes,
are org-scoped, support plain and guardrail (`detection` envelope, §8.17) forms, soft-
or hard-delete depending on guardrail ownership, and feed the engine-side suite runner
/ scorer (`core/eval_engine/*`).

## Behaviours

- [x] `POST /api/v1/evals` creates an eval definition (admin-gated), accepting a
      guardrail `detection` envelope or rejecting a forbidden envelope combination
      (422); create is denied under break-glass mint (`deny_break_glass_mint`)
- [x] `GET /api/v1/evals` lists org-scoped definitions with pagination and pipeline /
      eval-type filters (invalid eval-type 422)
- [x] `GET /evals/{id}`, `PUT /evals/{id}` and `DELETE /evals/{id}` provide detail,
      update and delete; delete is 204, 404 when missing, and guardrail evals
      soft-delete (audited) vs non-guardrail hard-delete
- [x] `GET /evals/coverage` reports pipeline/node eval coverage; `GET
      /evals/leaderboard` and `GET /evals/{id}/timeseries` surface per-eval
      pass-rate leaderboards and time-bucketed results
- [x] `GET /eval-coverage-gap` (409 on conflict) detects coverage gaps; the
      orphaned-definition guard rejects unstable coverage-gap eval definitions
      (`core/eval_engine/coverage_gap.py`)
- [x] `PUT /evals/suites/{suite_id}/alerting` configures regression alerting for an
      eval suite (admin-only)
- [x] `GET /runs/{run_id}/evals` lists the eval results attached to a run (org-scoped)
- [x] `POST /evals/compare` and `POST /evals/from-run` provide side-by-side run
      comparison and creating an eval definition from a run
- [x] Proposals: `GET /api/v1/feedback/proposals` lists the eval proposals queue and
      `POST /feedback/proposals/{record_id}/publish` promotes a proposal into a live
      eval definition (PRD §8.20 "Eval suite growth #3")
- [x] Engine coverage: regex/block evals transition runs to `eval_failed`
      (`eval_block.feature`), LLM-judge scored evals, suite CRUD + run scoring with
      aggregate scores and pass thresholds (`eval_run.feature`, `eval_suite_crud.feature`,
      `eval_scorer.feature`), and regression alerting (`test_eval_regressions.py`)

## Known Gaps

- **`eval_run.feature` drives a suite-run surface that lives outside `evals.py`** —
  the "POST /api/pipelines/{id}/evals → 202 pending suite run" scenario maps to the
  SuiteRun runner (FAR-377); the shipped `/api/v1/evals` surface tracks eval
  *definitions* and results, and suite-run execution coverage is unit/integration-level.
- **Guardrail evals' soft-delete + purge semantics differ per org** — soft vs hard
  delete depends on org purge settings; the endpoints expose it but the distinction is
  audit-driven, not feature-gated per tenant.

## QA History

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-evals`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/evals.py`,
  `api/routes/feedback.py`, `core/eval_engine/*` and the eval BDD/unit suites.
  Status: covered.