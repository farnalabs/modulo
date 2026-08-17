---
id: feat-infra-health
prd: N/A
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/health.py
unit-tests:
  - backend/tests/unit/api/test_health.py
bdd:
  - backend/tests/bdd/features/operations/health_checks.feature
depends-on: []
status: covered
---

# Health Checks

Liveness and readiness endpoints for deployment health monitoring. Liveness (`/healthz`) returns a simple OK. Readiness (`/healthz/ready`) checks database, Redis, checkpoint schema, and Alembic migration status.

## Behaviours

- [x] GET /healthz returns `{"status": "ok"}`
- [x] GET /healthz/ready checks database connectivity via SELECT 1
- [x] Redis connectivity check (degraded if not configured)
- [x] Checkpointer schema accessibility check (degraded on failure)
- [x] Alembic migration status check (degraded if pending)
- [x] Overall status: unavailable if any check is unavailable, degraded if any degraded
- [x] 503 status code when overall unavailable
- [x] Latency tracking per check
- [x] Per-check timeout limits
- [x] Integration with deployment orchestrator — `/healthz/ready` is wired as the Fly.io `http_service.checks` probe in `fly.toml`; the aggregate status + 503 semantics are exactly what the bluegreen orchestrator consumes for cutover (unit-tested in `backend/tests/unit/api/test_health.py`)

## Known Gaps

- **No PRD section reference.** PRD section 10.5 is "Opt-In Telemetry" — not related to health checks. The health endpoints are an internal infrastructure concern spanning deployment, monitoring, and operations docs (PRD §§5–6). No single PRD section covers liveness/readiness.
- [x] **RESOLVED (2026-08-17) — No BDD feature files**: Added `backend/tests/bdd/features/operations/health_checks.feature` with step definitions in `backend/tests/bdd/steps/test_health_checks.py`. Covers liveness (`/healthz`), readiness aggregation (ok/degraded/unavailable), per-check status exposure, and the FAR-199 dispatcher_reconcile two-tier gating (unavailable→503, degraded stays advisory) using the same patched-`_check_*` technique as the FastAPI unit tests.
- [x] **RESOLVED (2026-08-15) — Integration with deployment orchestrator**: Verified. `fly.toml` wires `GET /healthz/ready` as an `[[http_service.checks]]` probe consumed by Fly.io's bluegreen deployment strategy (any `unavailable` check → 503 → no cutover, per the health.py aggregation). The deployment metadata endpoint (`/api/v1/deployment`) remains a separate read-only surface; no orchestrator push channel is needed for the Fly integration.

## QA History

- 2026-08-15: Coverage-verification sweep. Marked [x] "Integration with deployment orchestrator" — verified `fly.toml` `http_service.checks` targets `/healthz/ready`, readiness aggregates DB/Redis/checkpointer/migrations/SAQ/system-cron/dispatcher-reconcile checks and returns 503 when any gates, and `test_health.py` unit-tests the aggregation + 503 semantics the orchestrator consumes. Status: partial → covered (10/10 behaviours).
