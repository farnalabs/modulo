---
id: feat-core-analytics
prd: 8.32
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/api/routes/analytics.py
  - backend/src/modulo/core/analytics/
  - backend/src/modulo/db/models/run_daily_facts.py
  - backend/src/modulo/db/migrations/versions/0067_run_daily_facts.py
  - backend/src/modulo/core/cost_controller/finalize.py
unit-tests:
  - backend/tests/unit/test_analytics_builder.py
  - backend/tests/unit/test_analytics_delta.py
  - backend/tests/integration/test_analytics_endpoint.py
  - backend/tests/integration/test_run_daily_facts.py
depends-on: [feat-core-cost-breakdown, feat-core-run-retention]
status: covered
---

# Analytics

Run analytics over a retained facts table (`run_daily_facts`, ADR 020): one row
per terminal run, written on every finalize path and backfilled by a daily
maintenance cron. Facts survive the 90-day run purge, so dimensioned run history
outlives the `runs` rows it was derived from.

## Behaviours

### API — Analytics Query (`GET /api/v1/analytics/query`)
- [x] Typed-params surface (`group_by`, optional dimension, filters, date range ≤ 365d, limit ≤ 1000)
- [x] Backend is the sole bucketing authority: day/ISO-week bucketing and zero-fill server-side
- [x] Rolling windows: Last 24h / 7d / 30d / 90d, period-scoped deltas from same-source same-window
- [x] Timezone fixed UTC; bounded `statement_timeout` (QueryCanceled → 503)
- [x] Feature-gated via `analytics_page` flag (default off → 402) + `analytics.query` permission (viewer)
- [x] Two-org isolation through the endpoint; explicit org predicate is the isolation control on Postgres
- [x] Predicate-strip → RLS zero rows

### Facts Writer (`record_run_facts`)
- [x] Called from every terminal finalize path in the same transaction
- [x] Fail-open via savepoint rollback — never breaks cost/ledger
- [x] `run_id` has a UNIQUE index but no FK to `runs` (facts survive the 90-day purge, ADR 020)

### Maintenance Cron (`analytics_facts_maintenance`)
- [x] Backfill: per-day anti-join INSERT...SELECT
- [x] Reconcile: ledger vs facts, direction-aware, auto-repair + cooldown alerts
- [x] Retention: 13-month chunked day-slice delete
- [x] `modulo_facts_*` gauges in `core/analytics/metrics.py`

### Run-Count Denominators
- [x] Facts count (`run_daily_facts`) for run volume and success rate
- [x] Ledger count (`org_daily_run_counts`) for cost dashboards
- [x] Summary count (`runs`) for live dashboard widgets

## Known Gaps

- **Frontend analytics page not shipped** — endpoint is feature-gated off by default until the frontend ships (§8.32.6).
- **No BDD feature file** — coverage is via integration tests only.
