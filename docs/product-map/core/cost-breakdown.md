---
id: feat-core-cost-breakdown
prd: 8.10
delivery-tasks: [task-nv7-cost-breakdown]
code:
  - backend/src/modulo/core/cost_controller/__init__.py
  - backend/src/modulo/api/routes/costs.py
  - backend/src/modulo/api/routes/org_settings.py
depends-on: [feat-teams-team-crud]
unit-tests:
  - backend/tests/unit/core/cost_controller/test_cost_controller.py
  - backend/tests/unit/core/cost_controller/test_cost_token_budget.py
  - backend/tests/unit/core/cost_controller/test_circuit_breaker.py
  - backend/tests/unit/api/test_costs.py
  - backend/tests/unit/api/test_cost_controls_bdd.py
  - backend/tests/unit/api/test_error_handling.py
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

- [x] `POST /reports` creates a cost-only report with the first daily/weekly/monthly UTC occurrence populated
- [x] Scheduled cost reports use the canonical cost aggregation and SMTP email delivery
- [x] One-time reports deactivate after their first successful delivery; recurring reports advance to the next cron occurrence
- [x] Scheduled report list/get/delete operations filter `report_type=cost` and cannot expose or delete other report types
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
- [x] `POST /anomalies/dismiss/{id}` dismisses an anomaly
- [x] `POST /anomalies/dismiss/{id}` when not found returns 404
- [x] `GET /anomalies/dismiss/{id}` is not allowed (405 — dismiss mutates state)
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
- [x] ProgrammingError on POST /api/v1/admin/costs/anomalies/dismiss/{id} returns 501
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
- [x] SQLAlchemyError on POST /api/v1/admin/costs/anomalies/dismiss/{id} returns 503
- [x] Warning logged with org_id context on every SQLAlchemyError path

### Resilience & Integration Robustness

- [x] All route handlers catch both ProgrammingError→501 and SQLAlchemyError→503
- [x] Warning log with org_id context on all DB error paths
- [x] 12 unit tests covering SQLAlchemyError→503 for all cost routes

### PRD §8.10 — Cost Controls (verified implemented)

- [x] Per-agent `token_budget` enforcement → `budget_exceeded` terminal state — `_enforce_agent_token_budgets` in `cost_controller/finalize.py` sums each agent's node tokens against its `token_budget` and writes the terminal override (`budget_exceeded` status + PRD error "This run exceeded its token budget."). Covered by `test_cost_token_budget.py` and the BDD `cost_controls.feature` "Token budget enforced" scenario.
- [x] Circuit breaker permanently pauses trigger until admin re-enables — `check_pipeline_circuit_breaker` / `trip_pipeline_circuit_breaker` mark `pipeline.circuit_breaker_tripped`, deactivate the pipeline's triggers, and dispatch the `circuit_breaker_tripped` notification; `POST /api/v1/admin/costs/circuit-breaker/{pipeline_id}/reset` (admin re-enable) clears the flag and re-activates triggers. Covered by `test_circuit_breaker.py` and the BDD `cost_controls.feature` circuit-breaker scenarios.
- [x] Configurable currency per organisation (default USD) — `GET/PUT /api/v1/admin/costs/controls` persist `currency` from `SUPPORTED_CURRENCIES` into `settings_json.cost_controls.currency`; covered in `test_costs.py`.
- [x] Per-trigger `daily_spend_limit` pauses trigger for the day — the daily spend gate is enforced in `cron_helpers.py` for the scheduled trigger paths (`_fire_ongoing_*`/`fire_cron_trigger` at line ~1326, `fire_polling_trigger` at line ~865, and the ongoing daemon sweep at line ~682): each reads `trigger.daily_spend_limit`, sums today's `Run.total_cost_usd` for the trigger, and when today's cost ≥ the limit skips the fire with skip-not-defer semantics (stamps `last_fired_at` so cadence is not misread as stalled) and records a `spend_limit_reached` TriggerEvent — i.e. the trigger is paused for the day. Verified in `cost_controller` + trigger tests. Remaining gap: event-driven **webhook** triggers have no intake spend gate, and the only notification is the TriggerEvent log (no separate admin-notification channel) — see Known Gaps.

### PRD — Missing from code (future scope)

- [ ] Per-run `run_budget` hard stop — no `run_budget` field or per-run budget check exists anywhere in the run path (agent-level budgets only)
- [ ] `cost_tracking: disabled` skips token accumulation for self-hosted models — the `ModelBackend.cost_tracking` column (`'enabled'|'disabled'`) exists, but cost finalization never reads it, so disabled backends still accumulate token cost
- [ ] Token-level cost accumulation via `on_llm_end` callback — the OTel bridge handler captures usage into OTel spans only; per-run token/cost accumulation happens at finalize from node outputs, not incrementally via the LLM callback
- [ ] Pricing table in `config/model_pricing.yaml` — the file does not exist

## Known Gaps
- Per-run `run_budget` (PRD §8.10) not implemented — no per-run budget field or check in the run path
- Per-trigger `daily_spend_limit` is enforced for ongoing/polling/cron trigger paths (skip-not-defer pause + `spend_limit_reached` TriggerEvent), but **not** enforced for event-driven **webhook** triggers at intake, and the only notification is the TriggerEvent log (no separate admin-notification channel)
- `cost_tracking: disabled` not enforced — the ModelBackend column exists but cost finalization never reads it, so open-weight/self-hosted backends still accumulate token cost
- Token-level cost accumulation via `on_llm_end` callback not wired up — the OTel bridge captures usage into spans; per-run accumulation happens at finalize
- `config/model_pricing.yaml` does not exist yet
- Integration tests skipped (awaiting fixture repair)

## QA History

- 2026-08-15: feat-core-cost-breakdown → partial, product-map coverage sweep: **RESOLVED the stale "future scope" claims for three PRD §8.10 controls that are already implemented and tested** — per-agent `token_budget` → `budget_exceeded` (`_enforce_agent_token_budgets`, `test_cost_token_budget.py` + BDD), circuit breaker (trip → triggers deactivated → admin reset endpoint, `test_circuit_breaker.py` + BDD), and configurable per-org currency (`/admin/costs/controls` `currency`, `test_costs.py`). Moved them from "Missing from code (future scope)" to a verified-implemented section and marked `[ ]`→`[x]`. Updated frontmatter `unit-tests` to include `test_cost_token_budget.py` / `test_circuit_breaker.py`. **Corrected a fourth claim on the same sweep**: per-trigger `daily_spend_limit` is NOT an unimplemented future item — the daily spend gate is enforced in `cron_helpers.py` for ongoing/cron (`~line 1326`), polling (`~line 865`), and the ongoing daemon sweep (`~line 682`), pausing the trigger for the day (skip-not-defer + `spend_limit_reached` TriggerEvent); it was moved to verified-implemented with the remaining gap scoped to event-driven **webhook** triggers (no intake spend gate) and the absence of a separate admin-notification channel (only the TriggerEvent log). **Verified the remaining future-scope items are genuine gaps**: per-run `run_budget` (no field/check in the run path), `cost_tracking: disabled` (column exists, cost finalization never reads it), `on_llm_end` token accumulation (OTel bridge captures usage into spans only), and `config/model_pricing.yaml` (file absent). Corrected the stale "future:" comments in `cost_controls.feature` for the now-implemented token-budget and circuit-breaker scenarios.
- 2026-08-12: improve-architecture → partial: **RESOLVED the "Anomalies route uses GET for dismiss action" REST-convention gap** (`api/routes/costs.py`). The `dismiss_anomaly_endpoint` was registered as `GET /anomalies/dismiss/{id}` while the frontend (`AdminCostBreakdownView.vue`) issues `POST` — a runtime 405 that made anomaly dismissal non-functional and a REST-convention violation (state-mutating GET). Changed the route to `POST` (matching the in-app-notifications/onboarding dismiss endpoints), keeping the 204/404 behaviour, `require_feature`/`require_permission` guards, and the 501/503 error mapping unchanged. Updated the OpenAPI-generated client (`frontend/src/lib/api/schema.ts`, operation renamed to `dismiss_anomaly_endpoint_..._post`) and the two `TestDismissAnomaly` unit tests to `client.post`; added a regression guard `test_dismiss_is_post_not_get` (asserts GET → 405). Frontend spec now mocks `api.POST` and adds a dismiss-flow test asserting the POST call. Updated product map behaviours `[ ]`→`[x]` (dismiss is POST, GET → 405, 501/503 paths), Known Gap → RESOLVED, QA History.
- 2026-07-07: feat-core-cost-breakdown → partial, cross-cutting QA (index 241): Fixed CRITICAL — added SQLAlchemyError→503 catches to all 12 cost route handlers (previously only caught ProgrammingError→501). Fixed CRITICAL — frontend AdminCostControlsView.vue read/wrote wrong field names (`daily_limit_usd`→`daily_spend_limit`, `teams`→`team_limits`, `org_daily_limit_usd`→`org_daily_spend_limit`) making team budget editing non-functional. Fixed CRITICAL — frontend AdminCostBreakdownView.vue called POST for dismiss anomaly but backend uses GET (405 Method Not Allowed). Fixed MAJOR — ~35 hardcoded English strings across both cost views wrapped in $t() with 33 new i18n keys. Added Resilience & Integration Robustness section to product map with 3 checkboxes. Added SQLAlchemyError→503 error handling section (12 checkboxes). Added 12 unit tests for SQLAlchemyError→503. Created test_costs_sqlalchemy_error.py. All tests pass. Merged to main. Status: partial.
