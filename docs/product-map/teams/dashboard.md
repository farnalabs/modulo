---
id: feat-teams-dashboard
prd: §14 (Future Roadmap — Dashboard)
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
- [ ] `DashboardSummary` interface includes all API response fields (missing teams, eval_pass_rate, trend)
- [ ] DashboardView consumes store instead of calling API directly

### Edge Cases & Error States

- [ ] Empty org (zero runs, zero pipelines) renders all-zero stat cards
- [ ] API returns 500/503 — frontend shows graceful error message
- [ ] Network failure — frontend catches and displays error

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
- [ ] Unit test: empty org state (zero data)
- [ ] Frontend unit test: loading state rendering
- [ ] Frontend unit test: error state rendering
- [ ] Frontend unit test: stat card data binding
- [ ] Frontend integration test: store to API wiring
- [ ] BDD scenario: basic dashboard layout and content
- [ ] BDD scenario: empty org state
- [ ] BDD scenario: error state display
- [ ] BDD scenario: navigate from dashboard to run detail

## Known Gaps

- **BDD feature file (`eval_dashboard.feature`) is a placeholder** — no real scenarios exist.
- **Frontend `DashboardSummary` interface is incomplete** in both DashboardView and the store — missing `teams`, `eval_pass_rate`, `trend` despite the API returning them.
- **DashboardView does not use the Pinia store** — calls API directly with inline fetch logic instead of consuming `useDashboardStore()`.
- **Frontend unit test coverage is minimal** — only a "renders the heading" smoke test exists (`DashboardView.spec.ts`).
- **`GET /api/v1/dashboard/trends` endpoint is fully implemented** but has no frontend page consuming it.
- **Sibling entry `feat-teams-org-dashboard-full`** tracks the full-feature dashboard (team breakdown, eval pass rate visualisation, trend charts, HITL/feedback analytics) that builds on this basic dashboard.
