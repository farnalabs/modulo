---
id: feat-costs
prd: 8.10, 9.3
adr: []
code:
  - backend/src/modulo/api/routes/costs.py
  - backend/src/modulo/api/routes/cost_components.py
  - backend/src/modulo/core/cost_controller
  - backend/src/modulo/core/cost_settings.py
  - backend/src/modulo/core/spend_ceiling.py
  - backend/src/modulo/db/crud/scheduled_report.py
  - backend/src/modulo/db/crud/spend_anomaly.py
unit-tests:
  - backend/tests/unit/api/test_costs.py
  - backend/tests/unit/api/test_cost_controls_bdd.py
  - backend/tests/unit/api/test_admin_spend_limits_gating.py
  - backend/tests/unit/core/test_cost_settings.py
  - backend/tests/unit/core/test_spend_ceiling.py
  - backend/tests/unit/core/cost_controller/test_cost_components_crud.py
  - backend/tests/unit/core/cost_controller/test_cost_finalize.py
  - backend/tests/unit/core/cost_controller/test_cost_finalize_ceiling.py
bdd:
  - backend/tests/bdd/features/costs/cost_controls.feature
depends-on:
  - feat-pipelines
status: covered
---

# Cost Tracking, Spend Limits & Cost Controls

Admin cost management: per-entity cost reporting over a period, org and per-team
daily spend limits, FAR-391 hard spend ceilings (`max_run_cost` / `spend_ceiling`,
stored as integer cents and enforced at the run gate), alert thresholds, a
per-pipeline cost circuit breaker (§8.10), cost-component attribution, CSV export,
scheduled cost reports, rolling spend-anomaly detection, and terminal-only spend
recording / refusal windows (spec §9.3 / §4.6) on the `core/cost_controller/*`
side. Surfaces: `/admin/costs`, `/admin/costs/limits`, `/admin/costs/controls`,
`/admin/costs/components`.

## Behaviours

- [x] `GET /api/v1/admin/costs?group_by=team|org&period=day|week|month|year` returns
      per-entity cost-report rows (`total_spend_usd`, `total_runs`, component
      breakdown, refused/clamped annotations) plus Decimal-string reporting buckets
      (`org_total`, `legacy_total`, `org_unassigned_components`) and `has_more`
      (`test_costs.py::TestGetCostsReport`, `cost_controller/breakdown/*`)
- [x] `GET/PUT /limits/org` and `PUT /limits/teams/{id}` read and set daily spend
      limits (null clears, negative rejected 422, cross-org team 404s untouched)
- [x] `GET/PUT /controls` read/update the full cost-controls payload: budget,
      `max_run_cost` + `spend_ceiling` (explicit null clears a ceiling, 0 is a
      kill-switch that blocks all runs), cumulative spend, alert thresholds (written
      via a 1..100 validator, read through a corrupted-value-defensive fallback),
      circuit-breaker toggle, currency (USD/EUR/GBP), billing period
      (monthly/quarterly/annual)
- [x] `GET/PUT /ceiling` expose the dedicated FAR-391 hard-ceiling surface with
      `remaining_budget_usd = max(spend_ceiling - cumulative, 0)`
- [x] Terminal-only spend recording: `check_and_record_spend` (spec §9.3) records on
      terminal statuses and enforces refusal windows (spec §4.6) returning
      `daily_limit_exceeded: organisation` / `daily_limit_exceeded: team` reasons
      without incrementing the org run count (`cost_controller/__init__.py`,
      `cost_controls.feature`)
- [x] Per-agent token budget: a run crossing its agent token budget transitions to the
      `budget_exceeded` terminal state with "This run exceeded its token budget."
      (`cost_controller/finalize.py`, `cost_controls.feature`)
- [x] Pipeline cost circuit breaker (§8.10): a pipeline crossing its monthly spend
      threshold trips the breaker and permanently pauses triggers until an admin
      re-enables it via `POST /circuit-breaker/{pipeline_id}/reset`
- [x] `GET /export?period=this_month|last_month|7d|30d|90d&group_by=team|pipeline|model`
      streams a CSV attachment with a `costs-export-{period}.csv` disposition
- [x] Scheduled cost reports: `POST/GET/DELETE /reports` manage org-owned
      daily/weekly/monthly, team/org, csv/json, one-time/recurring reports with
      `recipients` (email) required (min 1)
- [x] Rolling spend-anomaly detection: days whose org spend exceeds 2x the trailing
      7-day average are detected from `OrgDailyRunCount`, merged with persisted
      anomalies so dismissals survive, and dismissible via
      `POST /anomalies/dismiss/{id}`
- [x] Cost-component admin CRUD (attribution of spend to named components) in
      `api/routes/cost_components.py` + `cost_controller/test_cost_components_crud.py`
- [x] The verification canary (`cost_controller/probe.py`, spec §4.7) and system
      config are unit-covered alongside the gate itself

## Known Gaps

- **Export grouping is a façade for `pipeline` / `model`** — the export handler maps
  any `group_by != "team"` to the team report, so pipeline- and model-level export
  granularity is not independently implemented.
- **No BDD for the ceiling / scheduled-report / anomaly / cost-component surfaces** —
  `cost_controls.feature` covers only token budget, org/team spend limits and the
  circuit breaker; the rest are unit-only.
- **Anomaly detection has no persistence on first detection path in the endpoint** —
  freshly detected anomalies carry an empty `id` and are merged with stored rows, so
  a dismissal can only target previously persisted anomalies.

## QA History

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-costs`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/costs.py`,
  `api/routes/cost_components.py`, `core/cost_controller/*` and the costs unit/BDD
  suites. Status: covered.