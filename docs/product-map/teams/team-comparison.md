---
id: feat-teams-team-comparison
prd: 8.26
delivery-tasks: [task-nv7-team-comparison]
bdd:
  - backend/tests/bdd/features/personas/elena-engineering-director.feature
code:
  - frontend/src/views/TeamComparisonView.vue
  - frontend/src/router/index.ts
  - backend/src/modulo/api/routes/dashboard.py
  - backend/src/modulo/api/routes/admin.py
  - backend/tests/unit/api/test_dashboard.py
unit-tests:
  - frontend/src/__tests__/TeamComparisonView.spec.ts
depends-on: [feat-teams-dashboard, feat-teams-team-crud, feat-evals-eval-engine]
status: partial
---
# Team Comparison

Side-by-side eval pass rates and pipeline metrics across teams. Built on the org dashboard summary API. Route at `/admin/teams/comparison`.

## Behaviours
- [x] Side-by-side eval pass rates across teams with color-coded progress bars (green ≥80%, amber 50–79%, red <50%)
- [x] Org-wide summary cards: total runs, active pipelines, org eval pass rate, team count
- [x] Team run status breakdown in table columns (running, awaiting_human, failed, idle)
- [x] Team member count shown in comparison table
- [x] Drill-down row expansion per team showing pipeline-level eval breakdown (total evals, passed, pass rate)
- [x] Pipeline eval rows sorted by pass rate descending in drill-down
- [x] No teams exist — shows empty state: "No teams found. Create teams in Settings to see comparison data."
- [x] Dashboard summary API fails — shows error message with Retry button
- [x] Teams list API fails — shows error message with Retry button
- [x] Team has runs but no eval data — shows em-dash for eval pass rate
- [x] Org with teams but zero runs — shows zero-team metrics
- [x] Expanding a team with no pipeline eval data — shows "No eval data available for this team's pipelines."
- [x] Loading state — shows spinner during data fetch
- [x] Pipeline names API fails — silently falls back to truncated pipeline ID as display name
- [x] Toggle expand/collapse of team drill-down — clicking expanded team collapses; clicking different team switches drill-down
- [x] Expand/collapse chevron rotates 180° on open state

## Error Handling

- [x] Dashboard summary API returns 401/403 — unauthenticated user sees login redirect (handled by api client/interceptor)
- [x] Dashboard summary API returns 501 (ProgrammingError — table not migrated) — backend returns structured 501 with descriptive message `"Feature is not available. Run database migrations to enable it."` (dashboard.py:443-447)
- [x] Teams list API returns 401/403 — handled by api client/interceptor
- [x] Teams list API returns 501 — backend returns structured 501 on missing DB table (admin.py:1249-1253)
- [x] Pipeline names API failure — silently falls back to `shortId(pipelineId)` as display name (TeamComparisonView.vue:242)
- [x] Empty data (no teams exist at all) — shows empty state card (TeamComparisonView.vue:157-159)
- [x] Network error on initial data fetch — shows error message with Retry button (TeamComparisonView.vue:7)
- [x] Network error on pipeline name fetch — logged via console.warn, no user-facing error (TeamComparisonView.vue:333-335)

## Edge Cases

- [x] Single team (no comparison needed — table renders one row, no visual issue)
- [x] Many teams (teams API uses `page_size=100` — reasonable ceiling for org size) (TeamComparisonView.vue:257)
- [x] Team exists but has zero runs — shows zero in totalRuns and activePipelines columns, em-dash for eval pass rate
- [x] All teams have zero runs — summary cards show zeros, table renders with em-dashes
- [x] Team has runs but zero eval data — eval pass rate shows em-dash, drill-down shows "No eval data available"
- [x] Drill-down clicked during loading state — `expandedTeam` is null until `data.value` is populated (toggleExpand uses optional chaining) (TeamComparisonView.vue:316)
- [x] Double-click expand/collapse — toggle is idempotent: second click during expansion toggles back to collapse (no race condition as state is sync) (TeamComparisonView.vue:307-312)

## Known Gaps

- **Standalone BDD missing**: only covered as a persona scenario in `elena-engineering-director.feature` (`@goal-elena-team-comparison`). Step definitions are NOT implemented — the scenario exists as a placeholder only. Need `backend/tests/bdd/features/team-comparison.feature` with standalone scenarios and corresponding step defs in `test_team_comparison.py`.
- **Frontend tests expanded (2026-07-12)**: expanded from smoke-only (25 lines) to 6 tests covering error state, empty state, team data rendering, pipeline name fetch failure, and expand/collapse interaction. Still lacks i18n key resolution tests and Playwright E2E coverage.
- **Snapshot-only view**: this is a point-in-time comparison with no trend or historical comparison. Time-series comparison (e.g. "pass rate this week vs last week per team") is not available.
- **No cost data in comparison table**: token spend or cost per team is not shown, though the Elena persona scenario `@goal-elena-cost-by-team` describes this requirement.
- **i18n violations (now fixed)**: 11 hardcoded English strings replaced with `$t()` wrappers in cross-cutting QA (index 144). New keys added to `en-US.js` under `views.TeamComparisonView`. No regression risk — translations default to English key values.
- **PRD missing standalone spec**: Team Comparison is only listed as a sidebar route in PRD §8.26.2. No standalone PRD section exists (frontmatter uses `prd: 8` but the correct targeted reference would be `prd: 8.26`). Need a dedicated PRD spec.

## QA History

### 2026-07-12 — Round 2 cross-cutting QA feat-teams-team-comparison (index 394)

Verified all 8 error handling checkboxes and all 7 edge case checkboxes against actual code paths — all confirmed implemented. Fixed `fetchAndMerge` return type mismatch: was returning raw `ViewData` instead of `{data: ViewData}` expected by `useDataFetch` composable, causing the data template to never render (silent rendering bug). Expanded frontend test suite from 1 smoke test (25 lines) to 6 tests (160+ lines) covering error state rendering, empty state, team data rendering, pipeline name fetch failure, and expand/collapse interaction. Verified 3 existing behaviour checkboxes against code. Backend error handling confirmed correct (ProgrammingError→501, SQLAlchemyError→503, Exception→500 with B904 `from exc`) in both `dashboard.py` and `admin.py` teams route. No i18n violations found — all template strings use `$t()` wrappers. Known gaps updated.

### 2026-07-05 — Cross-cutting QA feat-teams-team-comparison (index 144)

Full feature coverage review. Fixed 11 i18n violations, added silent catch logging, enriched product map with 15 behaviour checkboxes, error handling section, and edge cases section.
