---
id: feat-infra-health
prd: N/A
code:
  - backend/src/modulo/api/routes/health.py
unit-tests:
  - backend/tests/unit/api/test_health.py
depends-on: []
status: partial
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
- [ ] Per-check timeout limits
- [ ] Integration with deployment orchestrator

## Known Gaps

- **No PRD section reference.** PRD section 10.5 is "Opt-In Telemetry" — not related to health checks. The health endpoints are an internal infrastructure concern spanning deployment, monitoring, and operations docs (PRD §§5–6). No single PRD section covers liveness/readiness.
- **No BDD feature files.** Health endpoints use FastAPI `TestClient` unit tests (`backend/tests/unit/api/test_health.py`) with patched check functions. No pytest-bdd scenarios exist.
- **Per-check timeout limits not implemented.** Each dependency check (`_check_database`, `_check_redis`, `_check_checkpointer`) uses a hard-coded internal timeout (`timeout=5`, `socket_connect_timeout=2`) but there is no configurable per-check timeout or config-driven limit.
- **No integration with deployment orchestrator.** The health endpoint does not report to any orchestrator (e.g. Fly.io bluegreen, K8s readiness gate) beyond serving the HTTP status code and JSON body.
