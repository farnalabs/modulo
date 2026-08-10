# ADR 020 — Analytics: run_daily_facts + typed-params query surface

**Date:** 2026-08-05
**Status:** Accepted (FAR-91 foundation: facts table + live writer + maintenance
cron + typed-params endpoint; the frontend ships in a later ticket)

---

## Context

Run history is currently only queryable live from `runs` (which is purged after
~90 days), and spend is aggregated day-granular into `org_daily_run_counts`
(the spend ledger — cost-tracking only). We need dimensioned, retained run
analytics: "how many runs, at what cost, by trigger/status/pipeline/folder/team,
over rolling windows" — with history that outlives the 90-day run purge.

## Decision 1 — Facts (denormalised, append-only-ish) over live aggregation

Analytics read from a **facts table** (`run_daily_facts`) — one row per terminal
run, written on every finalize path — rather than aggregating `runs` live.

Rationale:

- **Retention independence**: `runs` is purged at 90 days
  (`batch_delete_old_terminal_runs`); facts are retained 13 months (configurable,
  25 months later for YoY). Live aggregation can never answer "what happened 8
  months ago".
- **Dimension snapshots**: pipeline/team/folder names are snapshot on the fact
  at write time, so history stays legible even after renames/deletes.
- **Stable cost**: the fact stores `total_cost_usd`/`total_tokens` as finalized —
  re-aggregating runs later would recompute with today's component rates.

Trade-off: facts are a projection and can drift from `runs`/ledger — hence the
maintenance cron's reconcile (Decision 7).

## Decision 2 — `run_id` is deliberately NOT a foreign key

`run_daily_facts.run_id` has a UNIQUE index but **no FK to `runs`**. A future
"fix" that adds an FK breaks retention: deleting old runs (the 90-day purge)
would either cascade-delete the facts (destroying the analytics history the
feature exists for) or be blocked by the FK.

## Decision 3 — No query language in this delivery; ECharts decision context

The query surface is **structured typed params** (`group_by`, `dimension`,
filters, date range, limit). A query/expression syntax mode is deferred until a
pre-rolled, review-passed dependency slots in — the hand-rolled-parser discipline
of ADR 019 applies to any future syntax.

The frontend will render series with **ECharts** (the decision was taken during
planning for the analytics dashboards). It is recorded here because the dependency
is added in a later ticket: ECharts was chosen over hand-rolled SVG/canvas
charts because the rolling-window dashboards need time-series line/area charts,
tooltips, and period arrows out of the box, and over D3 because D3's composability
comes at a steep maintenance cost for charting that is 90% "series over time".
No charting dependency is added in this PR.

## Decision 4 — Rolling-window semantics (Last 24h / 7d / 30d / 90d)

Dashboards use **rolling windows** (industry convention): "Last 7 days" = the
7×24h ending now, timezone-agnostic, value + period arrow computed
period-scoped and same-source/same-window (a 7d delta compares the last 7d
against the previous 7d). This eliminates partial-period bias (a calendar-month
widget shows 3 days of data on the 3rd).

`run_daily_facts.created_at` (the source run's created-at instant) is kept for
"last 24h" rolling precision; `run_date` (UTC started-or-created day) is the
day-level bucket key.

## Decision 5 — Tenant isolation truth: the explicit org predicate is the control

On Postgres, `modulo_app` is BYPASSRLS and the ORM tenant filter
(`_inject_tenant_filter`) is NOT registered. The **explicit
`organisation_id = :org` predicate injected by the SQL builder is the ONLY
isolation control**. `set_rls_org` in the endpoint is defense-in-depth, not the
control. Consequences:

- The builder must carry the predicate on EVERY statement, always.
- A predicate-strip regression test asserts RLS still returns zero rows without
  the predicate under a non-superuser role (belt-and-braces), but the load-bearing
  guard is the predicate.
- The maintenance cron runs WITHOUT `set_rls_org` (modulo_app BYPASSRLS,
  cross-org scans work) — matching every existing system cron.

## Decision 6 — Facts-retention config and drop go/no-go

- Facts retention defaults to **13 months** (`analytics_facts_retention_months`),
  keeping one full year of history plus a margin month. **25 months** is the
  documented future option for YoY comparisons.
- **Go/no-go trigger for dropping facts entirely**: if after >90 days of
  production no dimensioned (>90d) analytics are ever requested, the facts
  table + writer + cron are candidates for removal (the 90-day `runs` purge then
  suffices). The trigger is a review point, not an automatic removal.

## Decision 7 — Reconcile role and composite-index escalation

- **Reconcile role**: "reconcile detects irrecoverable post-purge loss; backfill
  heals recoverable pre-purge loss." `reconcile_facts` compares per-(org, day)
  facts vs the org-level ledger row; a gap within source availability
  (runs still present) is auto-repaired by backfill; a gap beyond the purge
  window is an alert (structured log + `modulo_facts_reconcile_alert_total`
  counter + cooldown keyed (org, drift_type)).
- **Escalation triggers for composite indexes**: `ix_run_daily_facts_org_date`
  serves the org+day access path. If dashboard query latency degrades (P95 over
  budget for two consecutive probe windows), add composite indexes matching the
  hot dimension paths (e.g. `(organisation_id, run_date, trigger_type)`) via a
  normal migration — never ad-hoc DDL.

## Decision 8 — Metrics ownership: `modulo_facts_*` lives in core/analytics

The facts gauges/counters (`modulo_facts_write_failed_total`,
`modulo_facts_backfill_last_run_ts`, `modulo_facts_backfill_rows`,
`modulo_facts_reconcile_alert_total`, `modulo_facts_retention_lag`) live in
`modulo.core.analytics.metrics` — NOT in
`modulo.core.cost_controller.breakdown.metrics` (that module is the single
owning module for the COST engine's inventory only). Naming decision: analytics
metrics are `modulo_facts_*`-prefixed so they are identifiable as the facts
subsystem, and they follow the same lazy-handle pattern so a missing meter
provider never breaks the facts path.

## Consequences

- Every terminal finalize path writes a fact in the SAME transaction as the run
  status write, fail-open (a facts failure never affects the cost result).
- The typed-params endpoint is the sole bucketing authority; the client renders.
- Facts survive the run purge; reconcile keeps the projection honest.

## Decision 9 — Concurrency/slot-utilization columns + Python overlap bucketing (FAR-134)

`run_daily_facts` gains the absolute run-lifecycle instants (`dispatched_at`,
`started_at`, `completed_at`) and `total_queue_wait_ms` (`started_at −
created_at`, the full wait from creation to execution start) so slot
utilization can be reconstructed without reading live `runs` (purged at 90
days). The instants are deliberately NOT FKs — facts survive the purge
(Decision 2). Overlap counting (`[started_at, completed_at)` per bucket) is
done in Python via a line-sweep over interval start/end events (exact peak +
time-weighted mean), NOT a SQL GROUP BY — interval-overlap counting is not
expressible as a bucket aggregate, and the facts table stores no per-instant
run state. The concurrency surface (`GET /api/v1/analytics/concurrency` +
`query_analytics_concurrency` MCP tool) shares the same service patterns as
the bucketed query (org predicate, rate limit, statement timeout, typed
errors), and the live writer + backfill populate the new columns identically.
`pool_reference` exposes the binding concurrency cap for the query scope (org
`run_concurrency_limit`, or a single filtered pipeline's
`max_concurrent_runs`).
