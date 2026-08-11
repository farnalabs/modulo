---
id: feat-teams-team-comparison
prd: 8.26
delivery-tasks: [task-nv7-team-comparison]
bdd:
  - backend/tests/bdd/features/personas/elena-engineering-director.feature
code:
  - frontend/src/router/index.ts
  - backend/src/modulo/api/routes/dashboard.py
  - backend/src/modulo/api/routes/admin.py
  - backend/tests/unit/api/test_dashboard.py
unit-tests: []
depends-on: [feat-teams-dashboard, feat-teams-team-crud, feat-evals-eval-engine]
status: gap
---
# Team Comparison

**REMOVED.** The Team Comparison feature — side-by-side eval pass rates and pipeline metrics across teams — was removed end-to-end in PR #1018. There is no longer a route, view, or API for it. This entry is retained for historical context only; none of the behaviours below describe the current product.

## Behaviours

- (removed) Side-by-side eval pass rates across teams with color-coded progress bars (green ≥80%, amber 50–79%, red <50%)
- (removed) Org-wide summary cards: total runs, active pipelines, org eval pass rate, team count
- (removed) Team run status breakdown in table columns (running, awaiting_human, failed, idle)
- (removed) Team member count shown in comparison table
- (removed) Drill-down row expansion per team showing pipeline-level eval breakdown (total evals, passed, pass rate)
- (removed) Pipeline eval rows sorted by pass rate descending in drill-down
- (removed) No teams exist — empty state: "No teams found. Create teams in Settings to see comparison data."
- (removed) Dashboard summary API fails — error message with Retry button
- (removed) Teams list API fails — error message with Retry button
- (removed) Team has runs but no eval data — em-dash for eval pass rate
- (removed) Org with teams but zero runs — zero-team metrics
- (removed) Expanding a team with no pipeline eval data — "No eval data available for this team's pipelines."
- (removed) Loading state — spinner during data fetch
- (removed) Pipeline names API fails — silently falls back to truncated pipeline ID as display name
- (removed) Toggle expand/collapse of team drill-down — clicking expanded team collapses; clicking different team switches drill-down
- (removed) Expand/collapse chevron rotates 180° on open state

## Known Gaps

- **Feature removed**: Team Comparison was removed end-to-end in PR #1018 (route, view, and API deleted). This product-map entry is retained as a historical record only.

## QA History

### 2026-07-12 — Round 2 cross-cutting QA feat-teams-team-comparison (index 394)

Historical QA record for the now-removed feature.

### 2026-07-05 — Cross-cutting QA feat-teams-team-comparison (index 144)

Historical QA record for the now-removed feature.
