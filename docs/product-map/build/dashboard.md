---
id: feat-dashboard
prd: 8.20
adr: []
code:
  - backend/src/modulo/api/routes/dashboard.py
  - backend/src/modulo/api/routes/views.py
  - backend/src/modulo/db/crud/daily_run_count.py
unit-tests:
  - backend/tests/unit/api/test_dashboard.py
  - backend/tests/unit/api/test_daily_run_counts.py
  - backend/tests/unit/api/test_view_endpoint.py
bdd:
  - backend/tests/bdd/features/dashboard/hitl_trends.feature
  - backend/tests/bdd/features/views/views.feature
  - backend/tests/bdd/steps/test_views.py
depends-on:
  - feat-evals
status: covered
---

# Home Dashboard & Metrics Overview

The org home dashboard (`/`): a summary
of run counts, active pipelines, per-team metrics, eval pass rate, 7-day trend,
recent runs and config warnings, plus trend series (run counts, eval pass rate, token
spend, HITL volume/rejection/correlation, feedback volume), and daily run counts broken
down by status. Summary responses are cached per
org/`days` to keep the landing page fast.

## Behaviours

- [x] `GET /api/v1/dashboard/summary` returns `total_runs` (excluding pending/claimed),
      `active_pipelines`, `run_counts_by_status`, per-team metrics, `eval_pass_rate`,
      `trend` (7-day), `recent_runs`, `config_warnings`, and an optional `period` block
      when `days` is given (1..90, else 422); responses are cached
      (`test_dashboard.py`)
- [x] `GET /api/v1/dashboard/trends?days=7` returns `run_counts`, `eval_pass_rates`,
      `token_spend`, `hitl_volume`, `rejection_trend`, `correlation` and
      `feedback_volume` over the window; `days` bounded 1..90
- [x] `GET /api/v1/dashboard/daily-run-counts` returns daily counts grouped by run
      status with default/custom `days` bounds (`test_daily_run_counts.py`)
- [x] HITL volume / rejection-rate / correlation series over the trend window (§8.20)
      are asserted by `hitl_trends.feature`
- _Saved Views (`/admin/views`, `GET/POST/DELETE /api/v1/views`) deferred from the MVP
  nav (hidden via `visibility: private_preview`). Behaviour detail removed for the MVP
  cut — restore from git history when re-enabling. See FAR-546._
- [x] All summary/trend endpoints are org-RLS scoped and permission-gated
      (`dashboard.summary` / `dashboard.trends`)

## Known Gaps

- **No BDD for `/summary` / `/trends` / `/daily-run-counts`** — the dashboard
  surfaces are unit-tested only; the only dashboard BDD is `hitl_trends.feature`.
- **Eval pass rate is derived from non-guardrail eval results** — guardrail evals are
  deliberately excluded from the ratio (`non_guardrail_eval_results_clause`), so the
  headline pass rate does not reflect guardrail-blocked runs.

## QA History

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-dashboard`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/dashboard.py`,
  `api/routes/views.py`, `api/routes/daily_run_counts.py` and the dashboard/views
  unit+BDD suites. Status: covered.
