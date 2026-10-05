---
id: feat-infra-health
prd: N/A
adr: [ADR 021 (worker-resilience)]
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/health.py
  - fly.toml
  - .github/workflows/uptime-monitor.yml
unit-tests:
  - backend/tests/unit/api/test_health.py
bdd:
  - backend/tests/bdd/features/infra/health.feature
  - backend/tests/bdd/features/infra/test_health_steps.py
depends-on: []
status: covered
---

# Health Checks

Liveness and readiness endpoints for deployment health monitoring, plus the production
uptime watchdog that alerts on outage (FAR-400). Liveness (`/healthz`) is advisory — it
never flips readiness. Readiness (`/healthz/ready`) aggregates database, Redis,
checkpointer schema, Alembic migration status, database hygiene (dead-tuple bloat +
wraparound age, when the probe completes), worker/cron/scheduler liveness and returns
503 whenever any gate is unavailable. The AI agent can also be
redirected to this infra-health surface via `feat-infra-health`.

## Behaviours

- [x] `GET /healthz` returns `{"status": "ok"}` and is advisory only (never flips readiness)
- [x] `GET /healthz/ready` checks database connectivity (SELECT 1)
- [x] Redis connectivity check (degraded when not configured)
- [x] Checkpointer schema accessibility check (degraded on failure)
- [x] Alembic migration status check (degraded when migrations are pending)
- [x] Database-hygiene sub-check (FAR-1445) — worst-table dead-tuple ratio
      (`n_dead_tup / (n_live_tup + n_dead_tup)` from `pg_stat_user_tables`, over a
      configurable dead-tuple floor, zero-size relations excluded from the pick) plus
      `age(datfrozenxid)` against the server's own `autovacuum_freeze_max_age`;
      degrades (never `unavailable`, so never 503s readiness alone) on bloat at/above
      the configured ratio or on freeze age past 50% of the ceiling; thresholds via
      `MODULO_HEALTH_DB_HYGIENE_MIN_DEAD_TUPLES` / `MODULO_HEALTH_DB_HYGIENE_DEAD_RATIO`
- [x] A database-hygiene probe that does NOT complete within its budget (timeout, or the
      probe raising) reports `degraded` — "database-hygiene probe did not complete within
      Ns (likely an event-loop stall; see event_loop_lag) — hygiene not measured" — but is
      ADVISORY (FAR-1510): still listed in the body, never gating the aggregate and never
      firing the readiness alert, because no hygiene reading was taken. Only a completed
      reading graded over a threshold gates.
- [x] SAQ worker liveness check — a stopped worker pool for 4+ consecutive probe ticks 503s readiness (Plan F7)
- [x] System-cron liveness watchdog — fire_due_triggers missing 2x cadence 503s readiness (Plan F8)
- [x] Stale-run recovery sweep outcome — ADVISORY, never gates readiness: a missing or
      >15min-stale sweep reports `degraded` to alert operators while the app stays
      healthy (`_check_stale_run_recovery`, health.py)
- [x] Dispatcher reconcile staleness — two tiers: `degraded` after a single missed 60s tick
      is advisory (never flips readiness), `unavailable` past 5 minutes — the system
      worker's cron is silently dead and the fleet can no longer terminalize
      stalled/never-dispatched runs — 503s readiness
- [x] Fleet worker / fleet system-cron aggregation (worker process-group health, ADR 021)
- [x] Break-glass watchdog exposure is advisory and never contributes to readiness
- [x] Per-check timeout limits, configurable via `modulo_health_*_timeout_seconds` settings
- [x] Overall status: `unavailable` if any gate is `unavailable`, `degraded` if any gate
      is `degraded`. Gates are `database`, `redis`, `checkpointer`, `migrations`,
      `saq_workers`, `system_crons`, a COMPLETED `db_hygiene` reading, and
      `dispatcher_reconcile` at its `unavailable` tier only. Advisory checks are listed
      in the body but excluded from the aggregate: `break_glass`, `event_loop_lag`,
      `stale_run_recovery`, `slot_reconciliation`, `hitl_park_sweep`,
      `runner_workspace_reconcile`, `runner_marker_sweep`, `runner_health_probe`, the
      `degraded` tier of `dispatcher_reconcile`, and a database-hygiene probe that did
      not complete (FAR-1510). Source of truth: `evaluate_readiness` in health.py
- [x] 503 status code when overall unavailable
- [x] Latency tracked per check
- [x] Fly.io deployment wiring — `fly.toml` `[[http_service.checks]]` probes `/healthz/ready`
- [x] Worker process-group health check via top-level `[checks]` (ADR 021)
- [x] Production uptime monitor — `.github/workflows/uptime-monitor.yml` probes
      `app.modulo.run/healthz/ready` every 10 minutes and fails + opens a ticket on outage

## Known Gaps

- **No PRD section reference.** The health endpoints are an internal infrastructure
  concern spanning deployment, monitoring, and operations; no single PRD section covers
  liveness/readiness.

## QA History

- 2026-08-26: **product-map review pass** — closed the "No BDD feature
  files" gap: added `backend/tests/bdd/features/infra/health.feature` + `test_health_steps.py`
  (7 scenarios, self-contained: real health router in a fresh app with only the per-check
  probes patched). Covers liveness advisory, ok/degraded/unavailable aggregation, the 503
  gate, and the FAR-199 dispatcher-reconcile two-tier (unavailable gates, degraded stays
  advisory). Status: covered.
- 2026-08-25: **product-map review pass** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph that was lost from the public tree.
  Registered the dangling `feat-infra-health` reference: `backend/tests/unit/api/test_health.py`
  documented the per-check-timeout feature with a `feat-infra-health` tag that resolved
  nowhere. Re-verified all 19 behaviours against `backend/src/modulo/api/routes/health.py`,
  `fly.toml`, and `.github/workflows/uptime-monitor.yml`; status: covered.
