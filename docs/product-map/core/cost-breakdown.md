---
id: feat-core-cost-breakdown
prd: 8.10
delivery-tasks: [task-nv7-cost-breakdown]
code:
  - backend/src/modulo/core/cost_controller/__init__.py
  - backend/src/modulo/api/routes/costs.py
depends-on: [feat-teams-team-crud]
unit-tests:
  - backend/tests/unit/core/cost_controller/test_cost_controller.py
  - backend/tests/unit/api/test_costs.py
  - backend/tests/integration/crud/test_cost_attribution.py
status: partial
---
# Cost Breakdown

Discovered from 1 completed delivery tasks.

## Behaviours

### Spend Limit Enforcement (`check_and_record_spend`)

- [ ] Happy path: spend approved when no org limit set (no limit = allow any)
- [ ] Happy path: spend under both org and team limits approved
- [ ] Boundary: spend exactly at org limit approved
- [ ] Boundary: spend exactly at team limit approved
- [ ] Error: spend over org daily limit rejected with reason
- [ ] Error: spend over team daily limit rejected (org count not modified)
- [ ] Edge case: both limits null allows any spend amount
- [ ] Edge case: no `team_id` skips all team-level checks
- [ ] Concurrency: `SELECT FOR UPDATE` used for atomic check-and-increment
- [ ] Happy path: `run_count` and `total_spend_usd` both incremented on approval
- [ ] Edge case: no mutation of counts when spend rejected ### Daily Count Management (`get_or_create_daily_count`) - [ ] Happy path: returns existing `OrgDailyRunCount` row when present
- [ ] Happy path: creates new row with zero defaults when missing
- [ ] Edge case: creates new team-scoped row when `team_id` provided
- [ ] Concurrency: both read and create paths use `SELECT FOR UPDATE` ### Cost Reporting (`get_cost_report`) - [ ] Happy path: report by team aggregates correctly
- [ ] Happy path: report by org aggregates correctly (excludes team rows)
- [ ] Boundary: all period values accepted (day, week, month, year)
- [ ] Edge case: soft-deleted or unknown team name shown as "Unknown"
- [ ] Edge case: zero spend returns 0.0 / 0 in report
- [ ] Edge case: no rows in period returns empty report
- [ ] Error: invalid `group_by` value raises `ValueError`
- [ ] Error: invalid `period` value raises `ValueError` ### API — Cost Report Endpoints (`GET /api/v1/admin/costs`) - [ ] Happy path: returns cost report with period, group_by, items
- [ ] Happy path: defaults to `group_by=team, period=month`
- [ ] Error: invalid `group_by` returns 422
- [ ] Error: invalid `period` returns 422
- [ ] Auth: unauthenticated returns 401/403
- [ ] Auth: operator (non-admin) returns 403 ### API — Spend Limit Endpoints - [ ] `GET /limits` returns org + team spend limits
- [ ] `GET /limits` returns `None` when limits not set, empty list when no teams
- [ ] `PUT /limits/org` sets org daily spend limit
- [ ] `PUT /limits/org` clears limit (set to null)
- [ ] `PUT /limits/org` with negative value returns 422
- [ ] `PUT /limits/org` when org not found returns 404
- [ ] `PUT /limits/teams/{id}` sets team daily spend limit
- [ ] `PUT /limits/teams/{id}` clears team limit
- [ ] `PUT /limits/teams/{id}` with invalid UUID returns 422
- [ ] `PUT /limits/teams/{id}` with negative value returns 422
- [ ] `PUT /limits/teams/{id}` when team not found returns 404
- [ ] Auth: all limit endpoints admin-only (operator returns 403) ### API — Cost Export (`GET /api/v1/admin/costs/export`) - [ ] Happy path: returns CSV with headers and row data
- [ ] Error: invalid period returns 422
- [ ] Auth: unauthenticated returns 401/403 ### API — Scheduled Reports - [ ] `POST /reports` creates a scheduled report
- [ ] `POST /reports` with empty recipients returns 422
- [ ] `GET /reports` lists reports for the org
- [ ] `GET /reports` returns empty list when none exist
- [ ] `DELETE /reports/{id}` deletes a report
- [ ] `DELETE /reports/{id}` when not found returns 404
- [ ] Auth: all report endpoints admin-only ### API — Spend Anomalies - [ ] `GET /anomalies` returns computed anomalies (spend >2x rolling 7-day avg)
- [ ] `GET /anomalies` includes stored anomalies merged with computed
- [ ] `GET /anomalies` returns empty list when no anomalies
- [ ] `GET /anomalies/dismiss/{id}` dismisses an anomaly
- [ ] `GET /anomalies/dismiss/{id}` when not found returns 404
- [ ] Auth: anomaly endpoints admin-only ### PRD — Missing from code (future scope) - [ ] Per-agent `token_budget` enforcement → `budget_exceeded` terminal state
- [ ] Per-run `run_budget` hard stop
- [ ] Per-trigger `daily_spend_limit` pauses trigger for the day
- [ ] Circuit breaker permanently pauses trigger until admin re-enables
- [ ] Configurable currency per organisation (default USD)
- [ ] `cost_tracking: disabled` skips token accumulation for self-hosted models
- [ ] Token-level cost accumulation via `on_llm_end` callback
- [ ] Pricing table in `config/model_pricing.yaml` ## Known Gaps - No BDD feature files exist for cost controls or spend limits
- Per-agent `token_budget` and per-run `run_budget` (PRD 8.10) not yet implemented
- Per-trigger `daily_spend_limit` pause behaviour not implemented
- Circuit breaker not implemented
- `config/model_pricing.yaml` does not exist yet
- Token-level accumulation via LLM callback not wired up
- `_index.md` references `core/cost-controls.md` but file is named `core/cost-breakdown.md` 