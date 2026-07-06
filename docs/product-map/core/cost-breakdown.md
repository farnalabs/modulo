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
  - backend/tests/unit/api/test_cost_controls_bdd.py
  - backend/tests/unit/api/test_costs_programming_error.py
  - backend/tests/integration/crud/test_cost_attribution.py
bdd:
  - backend/tests/bdd/features/costs/cost_controls.feature
status: partial
---

# Cost Breakdown

Discovered from 1 completed delivery tasks.

## Behaviours

### Spend Limit Enforcement (`check_and_record_spend`)

- [x] Happy path: spend approved when no org limit set (no limit = allow any)
- [x] Happy path: spend under both org and team limits approved
- [x] Boundary: spend exactly at org limit approved
- [x] Boundary: spend exactly at team limit approved
- [x] Error: spend over org daily limit rejected with reason
- [x] Error: spend over team daily limit rejected (org count not modified)
- [x] Edge case: both limits null allows any spend amount
- [x] Edge case: no `team_id` skips all team-level checks
- [x] Concurrency: `SELECT FOR UPDATE` used for atomic check-and-increment
- [x] Happy path: `run_count` and `total_spend_usd` both incremented on approval
- [x] Edge case: no mutation of counts when spend rejected

### Daily Count Management (`get_or_create_daily_count`)

- [x] Happy path: returns existing `OrgDailyRunCount` row when present
- [x] Happy path: creates new row with zero defaults when missing
- [x] Edge case: creates new team-scoped row when `team_id` provided
- [x] Concurrency: both read and create paths use `SELECT FOR UPDATE`

### Cost Reporting (`get_cost_report`)

- [x] Happy path: report by team aggregates correctly
- [x] Happy path: report by org aggregates correctly (excludes team rows)
- [x] Boundary: all period values accepted (day, week, month, year)
- [x] Edge case: soft-deleted or unknown team name shown as "Unknown"
- [x] Edge case: zero spend returns 0.0 / 0 in report
- [x] Edge case: no rows in period returns empty report
- [x] Error: invalid `group_by` value raises `ValueError`
- [x] Error: invalid `period` value raises `ValueError`

### API — Cost Report Endpoints (`GET /api/v1/admin/costs`)

- [x] Happy path: returns cost report with period, group_by, items
- [x] Happy path: defaults to `group_by=team, period=month`
- [x] Error: invalid `group_by` returns 422
- [x] Error: invalid `period` returns 422
- [x] Auth: unauthenticated returns 401/403
- [x] Auth: operator (non-admin) returns 403

### API — Spend Limit Endpoints

- [x] `GET /limits` returns org + team spend limits
- [x] `GET /limits` returns `None` when limits not set, empty list when no teams
- [x] `PUT /limits/org` sets org daily spend limit
- [x] `PUT /limits/org` clears limit (set to null)
- [x] `PUT /limits/org` with negative value returns 422
- [x] `PUT /limits/org` when org not found returns 404
- [x] `PUT /limits/teams/{id}` sets team daily spend limit
- [x] `PUT /limits/teams/{id}` clears team limit
- [x] `PUT /limits/teams/{id}` with invalid UUID returns 422
- [x] `PUT /limits/teams/{id}` with negative value returns 422
- [x] `PUT /limits/teams/{id}` when team not found returns 404
- [x] Auth: all limit endpoints admin-only (operator returns 403)

### API — Cost Export (`GET /api/v1/admin/costs/export`)

- [x] Happy path: returns CSV with headers and row data
- [x] Error: invalid period returns 422
- [x] Auth: unauthenticated returns 401/403

### API — Scheduled Reports

- [x] `POST /reports` creates a scheduled report
- [x] `POST /reports` with empty recipients returns 422
- [x] `GET /reports` lists reports for the org
- [x] `GET /reports` returns empty list when none exist
- [x] `DELETE /reports/{id}` deletes a report
- [x] `DELETE /reports/{id}` when not found returns 404
- [x] Auth: all report endpoints admin-only

### API — Spend Anomalies

- [x] `GET /anomalies` returns computed anomalies (spend >2x rolling 7-day avg)
- [x] `GET /anomalies` includes stored anomalies merged with computed
- [x] `GET /anomalies` returns empty list when no anomalies
- [x] `GET /anomalies/dismiss/{id}` dismisses an anomaly
- [x] `GET /anomalies/dismiss/{id}` when not found returns 404
- [x] Auth: anomaly endpoints admin-only

### Error Handling

- [x] ProgrammingError on GET /api/v1/admin/costs returns 501
- [x] ProgrammingError on GET /api/v1/admin/costs/limits returns 501
- [x] ProgrammingError on PUT /api/v1/admin/costs/limits/org returns 501
- [x] ProgrammingError on PUT /api/v1/admin/costs/limits/teams/{id} returns 501
- [x] ProgrammingError on GET /api/v1/admin/costs/controls returns 501
- [x] ProgrammingError on PUT /api/v1/admin/costs/controls returns 501
- [x] ProgrammingError on GET /api/v1/admin/costs/export returns 501
- [x] ProgrammingError on POST /api/v1/admin/costs/reports returns 501
- [x] ProgrammingError on GET /api/v1/admin/costs/reports returns 501
- [x] ProgrammingError on DELETE /api/v1/admin/costs/reports/{id} returns 501
- [x] ProgrammingError on GET /api/v1/admin/costs/anomalies returns 501
- [x] ProgrammingError on GET /api/v1/admin/costs/anomalies/dismiss/{id} returns 501
- [x] SQLAlchemyError on GET /api/v1/admin/costs returns 503
- [x] SQLAlchemyError on GET /api/v1/admin/costs/limits returns 503
- [x] SQLAlchemyError on PUT /api/v1/admin/costs/limits/org returns 503
- [x] SQLAlchemyError on PUT /api/v1/admin/costs/limits/teams/{id} returns 503
- [x] SQLAlchemyError on GET /api/v1/admin/costs/controls returns 503
- [x] SQLAlchemyError on PUT /api/v1/admin/costs/controls returns 503
- [x] SQLAlchemyError on GET /api/v1/admin/costs/export returns 503
- [x] SQLAlchemyError on POST /api/v1/admin/costs/reports returns 503
- [x] SQLAlchemyError on GET /api/v1/admin/costs/reports returns 503
- [x] SQLAlchemyError on DELETE /api/v1/admin/costs/reports/{id} returns 503
- [x] SQLAlchemyError on GET /api/v1/admin/costs/anomalies returns 503
- [x] SQLAlchemyError on GET /api/v1/admin/costs/anomalies/dismiss/{id} returns 503
- [x] Warning logged with org_id context on every SQLAlchemyError path

### Resilience & Integration Robustness

- [x] All route handlers catch both ProgrammingError→501 and SQLAlchemyError→503
- [x] Warning log with org_id context on all DB error paths
- [x] 12 unit tests covering SQLAlchemyError→503 for all cost routes

### PRD — Missing from code (future scope)

- [ ] Per-agent `token_budget` enforcement → `budget_exceeded` terminal state
- [ ] Per-run `run_budget` hard stop
- [ ] Per-trigger `daily_spend_limit` pauses trigger for the day
- [ ] Circuit breaker permanently pauses trigger until admin re-enables
- [ ] Configurable currency per organisation (default USD)
- [ ] `cost_tracking: disabled` skips token accumulation for self-hosted models
- [ ] Token-level cost accumulation via `on_llm_end` callback
- [ ] Pricing table in `config/model_pricing.yaml`

## Known Gaps
- Per-agent `token_budget` and per-run `run_budget` (PRD 8.10) not yet implemented
- Per-trigger `daily_spend_limit` pause behaviour not implemented
- Circuit breaker not implemented
- `config/model_pricing.yaml` does not exist yet
- Token-level accumulation via LLM callback not wired up
- Anomalies route uses GET for dismiss action (should be POST/PATCH per REST conventions)
- Integration tests skipped (awaiting fixture repair)
- `get_cost_controls` and `update_cost_controls` return hardcoded defaults for alert_thresholds, circuit_breaker_enabled, currency, billing_period — never persisted to DB

## QA History

- 2026-07-07: feat-core-cost-breakdown → partial, cross-cutting QA (index 241): Fixed CRITICAL — added SQLAlchemyError→503 catches to all 12 cost route handlers (previously only caught ProgrammingError→501). Fixed CRITICAL — frontend AdminCostControlsView.vue read/wrote wrong field names (`daily_limit_usd`→`daily_spend_limit`, `teams`→`team_limits`, `org_daily_limit_usd`→`org_daily_spend_limit`) making team budget editing non-functional. Fixed CRITICAL — frontend AdminCostBreakdownView.vue called POST for dismiss anomaly but backend uses GET (405 Method Not Allowed). Fixed MAJOR — ~35 hardcoded English strings across both cost views wrapped in $t() with 33 new i18n keys. Added Resilience & Integration Robustness section to product map with 3 checkboxes. Added SQLAlchemyError→503 error handling section (12 checkboxes). Added 12 unit tests for SQLAlchemyError→503. Created test_costs_sqlalchemy_error.py. All tests pass. Merged to main. Status: partial.