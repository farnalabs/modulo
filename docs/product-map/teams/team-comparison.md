---
id: feat-teams-team-comparison
prd: 14
delivery-tasks: [task-nv7-team-comparison]
bdd:
  - backend/tests/bdd/features/personas/elena-engineering-director.feature # @goal-elena-team-comparison (step defs NOT implemented — only placeholder scenario)
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

- [ ] Dashboard summary API returns 401/403 — unauthenticated user sees login redirect (handled by api client/interceptor)
- [ ] Dashboard summary API returns 501 (ProgrammingError — table not migrated) — backend returns structured 501 with descriptive message `"Feature is not available. Run database migrations to enable it."` (dashboard.py)
- [ ] Teams list API returns 401/403 — handled by api client/interceptor
- [ ] Teams list API returns 501 — backend returns structured 501 on missing DB table (admin.py)
- [ ] Pipeline names API failure — silently falls back to `shortId(pipelineId)` as display name (TeamComparisonView.vue:243)
- [ ] Empty data (no teams exist at all) — shows empty state card (TeamComparisonView.vue:160-162)
- [ ] Network error on initial data fetch — shows error message with Retry button (TeamComparisonView.vue:318-319)
- [ ] Network error on pipeline name fetch — logged via console.warn, no user-facing error (TeamComparisonView.vue:357-359)

## Edge Cases

- [ ] Single team (no comparison needed — table renders one row, no visual issue)
- [ ] Many teams (teams API uses `page_size=100` — reasonable ceiling for org size)
- [ ] Team exists but has zero runs — shows zero in totalRuns and activePipelines columns, em-dash for eval pass rate
- [ ] All teams have zero runs — summary cards show zeros, table renders with em-dashes
- [ ] Team has runs but zero eval data — eval pass rate shows em-dash, drill-down shows "No eval data available"
- [ ] Drill-down clicked during loading state — `expandedTeam` is null until `data.value` is populated (toggleExpand early-returns if team not found)
- [ ] Double-click expand/collapse — toggle is idempotent: second click during expansion toggles back to collapse (no race condition as state is sync)

## Known Gaps

- **Standalone BDD missing**: only covered as a persona scenario in `elena-engineering-director.feature` (`@goal-elena-team-comparison`). Step definitions are NOT implemented — the scenario exists as a placeholder only. Need `backend/tests/bdd/features/team-comparison.feature` with standalone scenarios and corresponding step defs in `test_team_comparison.py`.
- **Frontend test is smoke-only**: `frontend/src/__tests__/TeamComparisonView.spec.ts` only verifies the component renders without crashing and shows "Team Comparison". No tests for: data rendering, error states, empty states, expand/collapse interaction, i18n key resolution.
- **Snapshot-only view**: this is a point-in-time comparison with no trend or historical comparison. Time-series comparison (e.g. "pass rate this week vs last week per team") is not available.
- **No cost data in comparison table**: token spend or cost per team is not shown, though the Elena persona scenario `@goal-elena-cost-by-team` describes this requirement.
- **i18n violations (now fixed)**: 11 hardcoded English strings replaced with `$t()` wrappers in cross-cutting QA (index 144). New keys added to `en-US.js` under `views.TeamComparisonView`. No regression risk — translations default to English key values.
- **PRD missing standalone spec**: Team Comparison is only listed as a sidebar route in PRD §8.26.2. No standalone PRD section exists (frontmatter references `prd: 14` which does not exist). Need a dedicated PRD spec.

## QA History

### 2026-07-05 — Cross-cutting QA feat-teams-team-comparison (index 144)

Full feature coverage review. Fixed 11 i18n violations, added silent catch logging, enriched product map with 15 behaviour checkboxes, error handling section, and edge cases section.
