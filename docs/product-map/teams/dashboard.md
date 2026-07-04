---
id: feat-teams-dashboard
prd: 14
delivery-tasks: [task-nv0-org-dashboard-basic]
bdd:
  - backend/tests/bdd/features/ui/eval_dashboard.feature
code:
  - backend/src/modulo/api/routes/dashboard.py
  - frontend/src/views/DashboardView.vue
  - frontend/src/stores/dashboard.ts
  - frontend/src/router/index.ts
  - backend/tests/unit/api/test_dashboard.py
  - frontend/src/__tests__/DashboardView.spec.ts
  - backend/tests/bdd/features/ui/eval_dashboard.feature
unit-tests:
  - backend/tests/unit/api/test_dashboard.py
  - frontend/src/__tests__/DashboardView.spec.ts
depends-on: []
status: partial
---
# Teams Dashboard

Basic org-level dashboard with run count summary and status breakdown. Root route (`/`) of the application.

## Behaviours

### API — Dashboard Summary (`GET /api/v1/dashboard/summary`)
- [x] Returns `total_runs` (org-wide run count)
- [x] Returns `active_pipelines` (count of pipelines)
- [x] Returns `run_counts_by_status` with keys: running, awaiting_human, failed, idle
- [x] Returns `teams` array with per-team: id, name, total_runs, active_pipelines, run_counts_by_status
- [x] Returns `eval_pass_rate` with overall_pass_rate, total_evals, passed_evals, per_pipeline breakdown
- [x] Returns `eval_pass_rate` as null when no evals exist (zero-data edge case)
- [x] Returns `trend` array (exactly 7 days) with date, run_count, eval_pass_rate, token_spend_usd per day
- [x] All queries scoped to organisation via set_rls_org()

### API — Dashboard Trends (`GET /api/v1/dashboard/trends`)
- [x] Returns `run_counts` as daily series
- [x] Returns `eval_pass_rates` with total_evals, passed_evals, pass_rate per day
- [x] Returns `token_spend` as daily USD series
- [x] Returns `hitl_volume` with total_decisions, approved_count, rejected_count, rejection_rate, avg_time_to_approve_ms per day
- [x] Returns `rejection_trend` with rolling 3-day average and raw rate per day
- [x] Returns `correlation` with eval_pass_rate + rejection_rate aligned by day
- [x] Returns `feedback_volume` with feedback_count, resolved_count, correcting_count per day
- [x] Defaults to 7 days when no `days` param
- [x] Accepts configurable 1–90 day range
- [x] Rejects `days=0` (422)
- [x] Rejects `days=91` (422)
- [x] All trend series have identical length matching requested `days`
- [x] All queries scoped to organisation via set_rls_org()

### API — Auth & Security
- [x] Both endpoints require authentication (401 for missing token)
- [x] RLS enforced on all queries
- [x] No cross-org data leakage

### Frontend — Dashboard View
- [x] Route at `/` with name `dashboard`
- [x] Redirect from `/dashboard` to `/`
- [x] Header says "Dashboard" with subtitle "Overview of your organisation's pipelines and runs"
- [x] Total Runs stat card with count
- [x] Active Pipelines stat card with count
- [x] Running stat card with success-coloured count
- [x] Awaiting Human stat card with warning-coloured count
- [x] Failed stat card with destructive-coloured count
- [x] Idle stat card with muted-coloured count
- [x] Loading spinner shown during fetch
- [x] Error state with destructive-styled message displayed on failure

### Frontend — Dashboard Store (Pinia)
- [x] Exposes reactive `summary`, `loading`, `error` state
- [x] Exposes `fetchSummary()` action
- [x] `DashboardSummary` interface includes all API response fields (teams, eval_pass_rate, trend, recent_runs, config_warnings)
- [x] DashboardView consumes store (useDashboardStore()) via onMounted

### Edge Cases & Error States
- [x] Empty org (zero runs, zero pipelines) renders all-zero stat cards and empty state CTA (welcome/create/browse)
- [x] API returns 500/503 — frontend shows ErrorAlert with retry
- [x] Network failure — frontend catches and displays ErrorAlert

### Testing
- [x] Unit test: dashboard_summary returns expected keys
- [x] Unit test: dashboard_summary includes team_metrics
- [x] Unit test: dashboard_summary includes eval_pass_rate
- [x] Unit test: dashboard_summary includes trend (7 entries)
- [x] Unit test: dashboard_summary requires auth
- [x] Unit test: dashboard_trends returns all trend families
- [x] Unit test: dashboard_trends defaults to 7 days
- [x] Unit test: dashboard_trends accepts 30 and 90 days
- [x] Unit test: dashboard_trends rejects 0 and 91 days
- [x] Unit test: dashboard_trends requires auth
- [x] Unit test: dashboard_trends HITL volume structure
- [x] Unit test: dashboard_trends rejection trend structure
- [x] Unit test: dashboard_trends correlation structure
- [x] Unit test: dashboard_trends feedback volume structure
- [x] Unit test: dashboard_trends all series aligned by day count
- [x] Frontend test: empty org state (zero data) shows Welcome CTA
- [x] Frontend test: loading state (6 skeleton cards with animate-pulse)
- [x] Frontend test: error state (ErrorAlert on fetch failure)
- [x] Frontend test: stat card data binding (Total Runs, Active Pipelines, Running, Awaiting Human, Failed, Idle)
- [x] Frontend integration test: store to API wiring (mock GET /api/v1/dashboard/summary)
- [ ] BDD scenario: org dashboard layout and content (eval_dashboard.feature has 4 eval-focused scenarios, not org dashboard)
- [ ] BDD scenario: empty org state
- [ ] BDD scenario: error state display
- [ ] BDD scenario: navigate from dashboard to run detail

### Error Handling

- [x] ProgrammingError caught → returned as 501 Not Implemented with migration hint (all 3 endpoints)
- [x] SQLAlchemyError caught → returned as 500 Internal Server Error with descriptive message
- [x] Generic Exception caught → logged with traceback, returned as 500 Internal Server Error
- [x] Frontend: loading skeleton shown during API fetch
- [x] Frontend: ErrorAlert with retry shown on fetch failure
- [x] Frontend: empty state CTA shown when total_runs === 0 and active_pipelines === 0
- [x] Frontend: no-runs-yet message shown when recent_runs is empty
- [x] Frontend: no-eval-data-yet message shown when eval_pass_rate is null

## Known Gaps

- **BDD feature file (`eval_dashboard.feature`) has 4 real scenarios** but they test eval run results, not the org dashboard summary. No BDD scenarios exist for the main org dashboard (`/api/v1/dashboard/summary` or `/api/v1/dashboard/trends` or the frontend `/` route).
- **`GET /api/v1/dashboard/trends` endpoint is fully implemented** but has no dedicated frontend page consuming it — only the 7-day sparkline on the main dashboard uses trend data.
- **No ProgrammingError→501 unit tests exist** for any of the 3 dashboard endpoints (summary, trends, daily-run-counts).
- **Sibling entry `feat-teams-org-dashboard-full`** (`docs/product-map/teams/org-dashboard-full.md`) tracks the full-feature dashboard (team breakdown, eval pass rate visualisation, trend charts, HITL/feedback analytics) that builds on this basic dashboard. 