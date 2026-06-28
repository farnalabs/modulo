---
id: feat-teams-team-comparison
prd: 14
prd: 14

delivery-tasks: [task-nv7-team-comparison]
  - backend/tests/features/personas/elena-engineering-director.feature (@goal-elena-team-comparison)
code:
  - frontend/src/views/TeamComparisonView.vue
  - frontend/src/router/index.ts
  - backend/src/modulo/api/routes/dashboard.py
  - backend/src/modulo/api/routes/admin.py
  - backend/tests/unit/api/test_dashboard.py
depends-on: [feat-teams-team-crud, feat-evals-eval-engine]
status: partial
---
# Team Comparison Side-by-side eval pass rates and pipeline metrics across teams. Built on the org dashboard summary API. Route at `/admin/teams/comparison`. Discovered from 1 completed delivery tasks. ## Behaviours - [x] Side-by-side eval pass rates across teams with color-coded progress bars (green ≥80%, amber 50–79%, red <50%)
- [ ] Org-wide summary cards: total runs, active pipelines, org eval pass rate, team count
- [ ] Team run status breakdown in table columns (running, awaiting_human, failed, idle)
- [ ] Team member count shown in comparison table
- [ ] Drill-down row expansion per team showing pipeline-level eval breakdown (total evals, passed, pass rate)
- [ ] Pipeline eval rows sorted by pass rate descending in drill-down
- [ ] No teams exist — shows empty state: "No teams found. Create teams in Settings to see comparison data."
- [ ] Dashboard summary API fails — shows error message with Retry button
- [ ] Teams list API fails — shows error message with Retry button
- [ ] Team has runs but no eval data — shows em-dash for eval pass rate
- [ ] Org with teams but zero runs — shows zero-team metrics
- [ ] Expanding a team with no pipeline eval data — shows "No eval data available for this team's pipelines."
- [ ] Loading state — shows spinner during data fetch
- [ ] Pipeline names API fails — silently falls back to truncated pipeline ID as display name
- [ ] Toggle expand/collapse of team drill-down — clicking expanded team collapses; clicking different team switches drill-down
- [ ] Expand/collapse chevron rotates 180° on open state ## Known Gaps
- **Per-team eval pass rate now computed**: Backend (dashboard.py:118-141) computes per-team eval pass rates via `per_team_eval_query` grouping EvalResults by `Run.owner_team_id`. The dashboard summary API returns `teams[].eval_pass_rate` with `total_evals`, `passed_evals`, and `pass_rate` per team. Frontend (TeamComparisonView.vue:291) reads `team.eval_pass_rate?.pass_rate` — each team now displays its own pass rate, not the org-wide average. The drill-down per-pipeline data lives in `eval_pass_rate.per_team_pipeline` (TeamComparisonView.vue:301). This gap is **CLOSED**.
- **No dedicated BDD feature file**: only covered as a persona scenario in `elena-engineering-director.feature` (`@goal-elena-team-comparison`). No standalone BDD in `backend/tests/bdd/features/`.
- **No frontend unit tests**: `TeamComparisonView.vue` has no corresponding spec file.
- **Snapshot-only view**: this is a point-in-time comparison with no trend or historical comparison. Time-series comparison (e.g. "pass rate this week vs last week per team") is not available.
- **No cost data in comparison table**: token spend or cost per team is not shown, though the Elena persona scenario `@goal-elena-cost-by-team` describes this requirement. 