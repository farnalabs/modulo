---
id: feat-teams-org-dashboard-full
prd: 8
delivery-tasks: [task-nv7-org-dashboard-full]
bdd:
  - backend/tests/bdd/features/dashboard/hitl_trends.feature
code:
  - backend/src/modulo/api/routes/dashboard.py
  - frontend/src/views/DashboardView.vue
  - frontend/src/stores/dashboard.ts
  - frontend/src/router/index.ts
  - frontend/src/lib/api/schema.ts
  - frontend/src/components/shared/Sparkline.vue
  - frontend/src/components/StatCard.vue
  - frontend/src/components/shared/ErrorAlert.vue
  - backend/tests/unit/api/test_dashboard.py
  - frontend/src/__tests__/DashboardView.spec.ts
  - frontend/src/views/TeamComparisonView.vue
unit-tests:
  - backend/tests/unit/api/test_dashboard.py
  - backend/tests/unit/api/test_dashboard_programming_error.py
  - frontend/src/__tests__/DashboardView.spec.ts
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
- [x] Stat card: Active Pipelines
- [x] Stat card: Running count
- [x] Stat card: Awaiting Human count
- [x] Stat card: Failed count
- [x] Stat card: Idle count
- [x] Loading spinner shown during fetch (skeleton grid, 6 cards)
- [x] Error state with destructive-styled message shown on failure (ErrorAlert with retry)
- [x] Team breakdown section (per-team stats, expandable per-pipeline drill-down)
- [x] Eval pass rate display (overall + per-pipeline, with sparkline + trend direction)
- [x] Trend chart/visualisation (7/30/90-day run count, eval rate, token spend)
- [x] Trends duration selector (7d/30d/90d) calling `GET /api/v1/dashboard/trends`
- [ ] HITL volume / rejection trend visualisation (API returns data, frontend does not render)
- [ ] Feedback volume visualisation (API returns data, frontend does not render)
- [x] Auto-refresh on run/pipeline events via EventBus sync (store.handleSyncEvent)
- [x] Header says "Dashboard" with subtitle "Overview of your organisation's pipelines and runs"

### Frontend — Dashboard Store (Pinia)
- [x] Store has typed `DashboardSummary` interface matching full API response (includes teams, eval_pass_rate, trend, recent_runs, config_warnings)
- [x] Exposes reactive `summary`, `loading`, `error` state
- [x] Exposes `fetchSummary()` action
- [x] DashboardView consumes store instead of calling API directly (DashboardView.vue:289)
- [ ] TeamComparisonView does NOT consume the store — calls API directly

### Edge Cases & Error States
- [x] Empty org (zero runs, zero pipelines) renders all-zero stat cards with welcome CTA — verified in DashboardView.vue:242
- [x] Org with teams but zero runs — team table renders with zero values; no crash
- [x] Eval_pass_rate is null when no EvalResult rows exist — handled in dashboard.py:271-279 + template:77-89
- [x] Trend day with missing data — defaults to 0 run_count, null eval_pass_rate, 0.0 token_spend (dashboard.py:332-339)
- [x] API returns 500 — frontend ErrorAlert with retry button rendered (DashboardView.vue:23)
- [x] API returns 503 for SQLAlchemyError from all 3 dashboard endpoints
- [x] Network failure — frontend catch blocks in fetchSummary / fetchTrends display error via ErrorAlert
- [ ] Large number of teams (100+) — not load-tested. No pagination on team query.

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
- [x] `ProgrammingError` caught → 501 on all 3 dashboard endpoints (/summary, /trends, /daily-run-counts)
- [x] `SQLAlchemyError` caught → 503 on all 3 dashboard endpoints (fixed 2026-07-07)
- [x] `Exception`→500 catch with `_log.exception` on all 3 endpoints (Python-level errors)
- [x] API returns 401 for unauthenticated requests (both summary and trends) — verified by test_dashboard.py:186-188, 228-230
- [ ] API returns 403 for non-admin users — `get_current_user` dependency only checks auth, not role. No org-role enforcement on dashboard routes.
- [x] API returns 422 for invalid `days` parameter (0 or 91) — FastAPI `Query(ge=1, le=90)` validation
- [x] API returns 500 — frontend ErrorAlert with retry button (DashboardView.vue:23, ErrorAlert)
- [x] API returns 503 for SQLAlchemyError — tested in test_dashboard_programming_error.py
- [x] Network failure — frontend catch blocks in fetchSummary / fetchTrends display error via ErrorAlert
- [x] Empty org renders all-zero stat cards — verified at DashboardView.vue:242
- [x] `eval_pass_rate` is null when no EvalResult rows exist — verified at dashboard.py:271-279

### Future / V2 Scope
- [ ] Full eval dashboard with chart visualisation (14 V2)
- [ ] Side-by-side run comparison view
- [ ] Advanced filtering and date range picker
- [ ] Export dashboard data (CSV, chart image)
- [ ] Custom widget layout
- [ ] Grafana-native dashboard as complement

## QA History

### 2026-07-07 — Cross-cutting QA (improve-architecture index 295)

**CRITICAL fixes applied:**
- `SQLAlchemyError` on all 3 dashboard endpoints (summary, trends, daily-run-counts) returned `500 Internal Server Error` instead of the established project-wide `503 Service Unavailable` pattern. Fixed all 3: `HTTP_500_INTERNAL_SERVER_ERROR` → `HTTP_503_SERVICE_UNAVAILABLE` with descriptive detail. Updated 3 test assertions in `test_dashboard_programming_error.py` (500→503).

**MAJOR product map fixes:**
- 15 stale `[ ]`→`[x]` in Frontend — Dashboard View section: all 6 stat cards, loading spinner skeleton, error state, team breakdown table, eval pass rate display with sparkline + trend direction, trend chart with 7d/30d/90d selector, auto-refresh via EventBus, header with subtitle.
- 5 stale `[ ]`→`[x]` in Frontend — Dashboard Store section: full `DashboardSummary` interface (teams, eval_pass_rate, trend, recent_runs, config_warnings), reactive summary/loading/error state, `fetchSummary()` action, store consumed by DashboardView.
- 1 stale `[ ]`→`[x]` in Edge Cases: 503 IS now explicitly returned for SQLAlchemyError.
- 2 stale `[ ]`→`[x]` in Error Handling: SQLAlchemyError→503 with test coverage, Exception→500 with `_log.exception` on all 3 endpoints.
- Frontmatter: added `bdd:` (hitl_trends.feature), `unit-tests:` (test_dashboard_programming_error.py), `code:` (Sparkline.vue, StatCard.vue, ErrorAlert.vue).
- Known Gaps: removed 5 stale/wrong entries (DashboardView was claimed incomplete, store interface was claimed incomplete, store was claimed not consumed, trends page was claimed not consumed). Refined remaining gaps to verified-accurate state.
- Added QA History section.

**Status:** partial (7 known gaps remain — no UI dashboard BDD feature, no HITL/feedback visualisation, TeamComparisonView does not use store, no frontend loading/error unit tests, shared error ref, no cache persistence without Redis, no large-team load-test).

## Known Gaps (verified 2026-07-07)

- **No BDD feature file exists** for the main org dashboard summary/trends UI. `backend/tests/bdd/features/ui/dashboard.feature` does not exist and needs creation. `hitl_trends.feature` covers HITL trends only.
- **HITL volume / rejection trend / feedback volume visualisation**: API returns full data (hitl_volume, rejection_trend, correlation, feedback_volume) but DashboardView does not render any of it. The trends data is only consumed for the run count / eval pass rate / token spend sparklines.
- **TeamComparisonView calls API directly** — does not consume the Pinia dashboard store.
- **No frontend unit test coverage** for loading state, error state, or data rendering (only a "renders heading" smoke test exists at `frontend/src/__tests__/DashboardView.spec.ts`).
- **Shared error ref in dashboard.ts** — `error.value` is computed from `summaryError.value || trendsError.value`. A failed trends call shows the trends error even if summary loaded successfully; if trends succeeds but summary fails, the summary error is shown. Partial-data states where one succeeded and the other failed mask the successful data from the error UI.
- **No Pinecone/Elasticsearch-based dashboard caching** — dashboard uses an in-memory dict and optional Redis with 60s TTL. The `_in_memory_cache` is process-local and lost on restart. Across a multi-worker deployment with Redis, this is acceptable; without Redis, each worker has its own cache leading to inconsistent views on consecutive requests.
- **No load-testing for large teams (100+)** — team metrics have no pagination.
