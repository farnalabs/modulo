---
id: feat-teams-org-dashboard-full
prd: 14
delivery-tasks: [task-nv7-org-dashboard-full]
bdd: []
code:
  - backend/src/modulo/api/routes/dashboard.py
  - frontend/src/views/DashboardView.vue
  - frontend/src/stores/dashboard.ts
  - frontend/src/router/index.ts
  - frontend/src/lib/api/schema.ts
  - backend/tests/unit/api/test_dashboard.py
  - frontend/src/__tests__/DashboardView.spec.ts
  - frontend/src/views/TeamComparisonView.vue
unit-tests:
  - backend/tests/unit/api/test_dashboard.py
depends-on:
  - feat-teams-dashboard
  - feat-teams-team-crud
  - feat-evals-eval-engine
  - feat-core-cost-breakdown
status: partial
---
# Org Dashboard (Full)

Org-level dashboard with run overview, team breakdown, eval quality metrics, trend data, HITL analytics, and feedback volume. Built on top of the basic dashboard with per-team drill-down.

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
- [x] Route at `/` (root) with name `dashboard`
- [x] Redirect from `/dashboard` to `/`
- [x] Stat card: Total Runs
- [ ] Stat card: Active Pipelines
- [ ] Stat card: Running count
- [ ] Stat card: Awaiting Human count
- [ ] Stat card: Failed count
- [ ] Stat card: Idle count
- [ ] Loading spinner shown during fetch
- [ ] Error state with destructive-styled message shown on failure
- [ ] Team breakdown section (per-team stats)
- [ ] Eval pass rate display (overall + per-pipeline)
- [ ] Trend chart/visualisation (7-day run count, eval rate, token spend)
- [ ] Trends page consuming `GET /api/v1/dashboard/trends`
- [ ] HITL volume / rejection trend visualisation
- [ ] Feedback volume visualisation
- [ ] Auto-refresh or periodic polling
- [ ] Header says "Dashboard" with subtitle "Overview of your organisation's pipelines and runs"

### Frontend — Dashboard Store (Pinia)
- [ ] Store has typed `DashboardSummary` interface matching full API response (currently missing teams, eval_pass_rate, trend)
- [ ] Exposes reactive `summary`, `loading`, `error` state
- [ ] Exposes `fetchSummary()` action
- [ ] DashboardView consumes store instead of calling API directly
- [ ] TeamComparisonView consumes store instead of calling API directly

### Edge Cases & Error States
- [ ] Empty org (zero runs, zero pipelines) renders all-zero stat cards
- [ ] Org with teams but zero runs shows zero-team metrics
- [ ] Eval_pass_rate is null when no EvalResult rows exist
- [ ] Trend day with missing data shows 0 run_count, null eval_pass_rate, 0.0 token_spend
- [ ] API returns 500/503 — frontend shows graceful error message
- [ ] Network failure — frontend catches and displays error
- [ ] Large number of teams (100+) renders without degradation

### Testing
- [x] Unit test: dashboard_summary returns expected keys
- [x] Unit test: dashboard_summary includes team_metrics with correct structure
- [x] Unit test: dashboard_summary includes eval_pass_rate with per-pipeline breakdown
- [x] Unit test: dashboard_summary includes trend with 7 entries
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
- [ ] Unit test: empty org state (zero data)
- [ ] Unit test: many-teams performance
- [ ] Frontend unit test: loading state rendering
- [ ] Frontend unit test: error state rendering
- [ ] Frontend unit test: stat card data binding
- [ ] Frontend integration test: store → API wiring
- [ ] BDD feature: org dashboard layout and content (`features/ui/dashboard.feature`)
- [ ] BDD scenario: team breakdown display
- [ ] BDD scenario: empty state (no runs)
- [ ] BDD scenario: eval quality dip visible on dashboard (Elena persona)
- [ ] BDD scenario: navigate from dashboard to run detail

### Error Handling
- [ ] ProgrammingError caught → 501 on all DB-accessing dashboard endpoints
- [ ] API returns 401 for unauthenticated requests (both summary and trends)
- [ ] API returns 403 for non-admin users on org-level operations
- [ ] API returns 422 for invalid `days` parameter (0 or 91)
- [ ] API returns 500/503 — frontend shows graceful error message with retry
- [ ] Network failure — frontend catches and displays ErrorAlert
- [ ] Empty org (zero runs, zero pipelines) renders all-zero stat cards (no crash)
- [ ] Eval_pass_rate is null when no EvalResult rows exist

### Future / V2 Scope
- [ ] Full eval dashboard with chart visualisation (14 V2)
- [ ] Side-by-side run comparison view
- [ ] Advanced filtering and date range picker
- [ ] Export dashboard data (CSV, chart image)
- [ ] Custom widget layout
- [ ] Grafana-native dashboard as complement

## Known Gaps

- **No BDD feature file exists** for the main org dashboard UI. `backend/tests/bdd/features/ui/dashboard.feature` does not exist and needs creation.
- **Frontend DashboardView is incomplete**: only shows basic stat cards (Total Runs, Active Pipelines, Running, Awaiting Human, Failed, Idle). Does not render team breakdown, eval pass rate, trend chart, or HITL/feedback metrics.
- **DashboardView `DashboardSummary` interface is incomplete** — missing `teams`, `eval_pass_rate`, `trend` fields from the API response.
- **Pinia store (`dashboard.ts`) has incomplete `DashboardSummary` interface** — same missing fields.
- **DashboardView does not use the Pinia store** — calls API directly with inline fetch logic. Store exists but is not consumed.
- **TeamComparisonView calls API directly** instead of consuming the store.
- **No frontend unit test coverage** for loading state, error state, or data rendering (only a "renders heading" smoke test exists).
- **`GET /api/v1/dashboard/trends` endpoint is fully implemented** but has no frontend page or component consuming it.
