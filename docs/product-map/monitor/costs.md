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
  - backend/src/modulo/db/models/spend_anomaly.py
  - backend/src/modulo/db/migrations/versions/0201_spend_anomaly_unique_org_date.py
unit-tests:
  - backend/tests/unit/api/test_costs.py
  - backend/tests/unit/api/test_cost_controls_bdd.py
  - backend/tests/unit/api/test_admin_spend_limits_gating.py
  - backend/tests/unit/api/test_costs_routes_coverage.py
  - backend/tests/unit/core/test_cost_settings.py
  - backend/tests/unit/core/test_spend_ceiling.py
  - backend/tests/unit/core/cost_controller/test_cost_components_crud.py
  - backend/tests/unit/core/cost_controller/test_cost_finalize.py
  - backend/tests/unit/core/cost_controller/test_cost_finalize_ceiling.py
  - backend/tests/unit/db/crud/test_spend_anomaly.py
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
      streams a CSV attachment with a `costs-export-{period}.csv` disposition;
      `team` reuses the daily-ledger grouping (historical shape), `pipeline`
      aggregates `Σ runs.total_cost_usd` per pipeline over the window
      (`cost_controller/_cost_export_by_pipeline`), and `model` aggregates the
      runs' `cost_breakdown` per `self_reported` cost component (the model/LLM
      spend sources; `cost_controller/_cost_export_by_model`). The pre-2026-09
      explicit-422 refusal for `pipeline`/`model` is retired — the enum values
      now stream real rows.
- [x] Scheduled cost reports: `POST/GET/DELETE /reports` manage org-owned
      daily/weekly/monthly, team/org, csv/json, one-time/recurring reports with
      `recipients` (email) required (min 1)
- [x] Rolling spend-anomaly detection: days whose org spend exceeds 2x the trailing
      7-day average are detected from `OrgDailyRunCount`, each fresh detection is
      persisted on first sight (`record_or_get_anomaly`, unique per detected
      org-day via `uq_spend_anomalies_org_date`) so it carries a real id and is
      dismissible via `POST /anomalies/dismiss/{id}`, repeat detections inherit
      the saved dismissal state, and previously stored still-flagged rows are
      merged into the response (`test_costs_routes_coverage.py`,
      `test_spend_anomaly.py`)
- [x] Cost-component admin CRUD (attribution of spend to named components) in
      `api/routes/cost_components.py` + `cost_controller/test_cost_components_crud.py`
- [x] The verification canary (`cost_controller/probe.py`, spec §4.7) and system
      config are unit-covered alongside the gate itself

## Known Gaps

- **`model` export granularity is per cost-component, not per model-backend
  identifier** — the runs table records spend via `cost_breakdown` components
  (the org's named spend contributors), so `group_by=model` aggregates per
  `self_reported` component (the model/LLM spend sources). A truly per-model
  split requires the producer to record a stable model identifier per run,
  which is not yet shipped; orgs that configure per-model `self_reported`
  components get that granularity today. `pipeline` granularity is fully
  implemented from `runs` (per-pipeline sums of `total_cost_usd`).
- **No BDD for the ceiling / scheduled-report / anomaly / cost-component surfaces** —
  `cost_controls.feature` covers only token budget, org/team spend limits and the
  circuit breaker; the rest are unit-only.

## QA History
- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/costs`, `/admin/costs/components`, `/admin/costs/controls`,
  `/admin/costs/limits`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/costs, /admin/costs/components, /admin/costs/controls`: the whole-page view(s) `AdminCostBreakdownView.vue, CostComponentsView.vue, AdminCostControlsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-costs`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/costs.py`,
  `api/routes/cost_components.py`, `core/cost_controller/*` and the costs unit/BDD
  suites. Status: covered.
- 2026-09-08: **improve-architecture (product-map walk)** — closed the anomaly
  persistence gap: freshly detected anomalies are now written on first sight
  (`record_or_get_anomaly`) and uniqueness per detected org-day is enforced
  (`uq_spend_anomalies_org_date`, migration 0201_spend_anomaly_unique_org_date), so every returned anomaly has a
  stable id that `POST /anomalies/dismiss/{id}` can target and dismissal state
  survives repeat detection. Endpoint + CRUD unit suites updated.
- 2026-09-09: **improve-architecture (product-map walk)** — closed the export
  façade gap: `GET /export` no longer silently maps `model` -> team nor crashes
  (`500`) on `pipeline`; unimplemented granularities now fail with an explicit
  422 naming `team` as the supported export grouping. `api/routes/costs.py` +
  `test_costs.py` / `test_costs_routes_coverage.py` updated.
- 2026-09-10: **improve-architecture (product-map walk)** — closed the export
  granularity gap: `GET /export` now implements `pipeline` (per-pipeline
  `Σ runs.total_cost_usd` aggregation over the window) and `model` (per
  `self_reported` cost-component aggregation over the runs' `cost_breakdown`)
  on top of the existing `team` ledger export; the retired 422 for those enum
  values streams real CSV rows instead. `cost_controller.get_cost_export_rows`
  (+ `_cost_export_by_pipeline` / `_cost_export_by_model`) is unit-covered
  (`test_cost_controller.py::TestGetCostExportRows`); `test_costs.py` and
  `test_costs_routes_coverage.py` updated for the new granularities and the
  error matrix now patches `get_cost_export_rows`. The residual narrowing gap
  ("model granularity is per cost-component, not per model-backend identifier")
  is tracked in Known Gaps above.
