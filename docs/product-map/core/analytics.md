---
id: feat-core-analytics
prd: 8.32
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/api/routes/analytics.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/core/analytics/
  - backend/src/modulo/core/mcp/scope_validator.py
  - backend/src/modulo/db/models/run_daily_facts.py
  - backend/src/modulo/db/migrations/versions/0067_run_daily_facts.py
  - backend/src/modulo/db/migrations/versions/0071_analytics_facts_enrich.py
  - backend/src/modulo/core/cost_controller/finalize.py
unit-tests:
  - backend/tests/unit/test_analytics_builder.py
  - backend/tests/unit/test_analytics_delta.py
  - backend/tests/unit/mcp/test_query_analytics.py
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
- [x] Multi-value `pipeline_id` filter — "A vs B" in a single request (allowlisted, parameterised)
- [x] `error_code` filter + dimension (the stall dimension)
- [x] Per-bucket stall metrics: `failure_count`, `stall_count`, `avg_queue_wait_ms`, `avg_final_idle_ms`, `avg_output_bytes` (stall = failed + `error_code` in `STALL_ERROR_CODES`)

### API — Analytics Export (`GET /api/v1/analytics/export`)
- [x] Raw fact rows (no bucketing), all fact columns, ordered by `run_date`/`created_at`
- [x] Paginated (`offset`/`limit`, default 500, max 5000), org-scoped, rate-limited, permission + feature gated
- [x] `format=json` (default) and `format=csv` (Content-Disposition attachment)

### MCP — `query_analytics`
- [x] Same typed params as REST (incl. repeated `pipeline_id`, `error_code`, date range, limit)
- [x] Enforces `analytics.query` permission and the `analytics_page` feature gate
- [x] Shares the analytics service (org predicate, rate limit, statement timeout, bucketing identical to REST)

### Facts Writer (`record_run_facts`)
- [x] Called from every terminal finalize path in the same transaction
- [x] Fail-open via savepoint rollback — never breaks cost/ledger
- [x] `run_id` has a UNIQUE index but no FK to `runs` (facts survive the 90-day purge, ADR 020)
- [x] Snapshots FAR-102 enrichment columns (error_code, claim_count, queue/final-idle ms, dispatcher, graph-derived node stats, parent_run_id/snapshot_id no-FK, run_number, output_bytes, rate_limited) — graph-derived fields NULL-safe

### Maintenance Cron (`analytics_facts_maintenance`)
- [x] Backfill: per-day anti-join INSERT...SELECT
- [x] Backfill selects the FAR-102 enrichment columns (incl. graph-derived via the snapshot join) — backfilled facts never carry NULL where the source provides a value
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
