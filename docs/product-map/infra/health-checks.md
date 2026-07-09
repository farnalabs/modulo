---
id: feat-infra-health
prd: 10.5
code:
  - backend/src/modulo/api/routes/health.py
bdd: []
unit-tests: []
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
