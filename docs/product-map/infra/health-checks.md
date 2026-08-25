---
id: feat-infra-health
prd: N/A
adr: [docs/adr/021-worker-resilience.md]
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/health.py
  - fly.toml
  - .github/workflows/uptime-monitor.yml
unit-tests:
  - backend/tests/unit/api/test_health.py
bdd: []
depends-on: []
status: covered
---

# Health Checks

Liveness and readiness endpoints for deployment health monitoring, plus the production
uptime watchdog that alerts on outage (FAR-400). Liveness (`/healthz`) is advisory — it
never flips readiness. Readiness (`/healthz/ready`) aggregates database, Redis,
checkpointer schema, Alembic migration status, worker/cron/scheduler liveness, stale-run
recovery and returns 503 whenever any gate is unavailable. The AI agent can also be
redirected to this infra-health surface via `feat-infra-health`.

## Behaviours

- [x] `GET /healthz` returns `{"status": "ok"}` and is advisory only (never flips readiness)
- [x] `GET /healthz/ready` checks database connectivity (SELECT 1)
- [x] Redis connectivity check (degraded when not configured)
- [x] Checkpointer schema accessibility check (degraded on failure)
- [x] Alembic migration status check (degraded when migrations are pending)
- [x] SAQ worker liveness check — a stopped worker pool for 4+ consecutive probe ticks 503s readiness (Plan F7)
- [x] System-cron liveness watchdog — fire_due_triggers missing 2x cadence 503s readiness (Plan F8)
- [x] Stale-run recovery check — stalled/never-dispatched runs block readiness
- [x] Dispatcher reconcile staleness reported (advisory/bounded)
- [x] Fleet worker / fleet system-cron aggregation (worker process-group health, ADR 021)
- [x] Break-glass watchdog exposure is advisory and never contributes to readiness
- [x] Per-check timeout limits, configurable via `modulo_health_*_timeout_seconds` settings
- [x] Overall status: unavailable if any check is unavailable, degraded if any degraded
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
- **No BDD feature files.** Health endpoints use FastAPI `TestClient` unit tests
  (`backend/tests/unit/api/test_health.py`) with patched check functions. No pytest-bdd
  scenarios exist.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph that was lost from the public tree.
  Registered the dangling `feat-infra-health` reference: `backend/tests/unit/api/test_health.py`
  documented the per-check-timeout feature with a `feat-infra-health` tag that resolved
  nowhere. Re-verified all 19 behaviours against `backend/src/modulo/api/routes/health.py`,
  `fly.toml`, and `.github/workflows/uptime-monitor.yml`; status: covered.