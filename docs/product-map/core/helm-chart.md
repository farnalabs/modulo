---
id: feat-core-helm-chart
prd: 11, 13
delivery-tasks: [task-nv9-helm-chart]
code:
  - helm/
  - docker-compose.yml
  - docker-compose.local.yml
  - docker-compose.test.yml
  - docker-compose.mariadb.yml
  - backend/Dockerfile
status: partial
---

# Helm Chart / Docker Compose — Deployment Packaging

Packages Modulo for self-hosted deployment via Docker Compose (dev/alpha/poc) and Helm (production Kubernetes). Docker Compose is the alpha delivery vehicle ("walkable in under 5 minutes from `docker compose up`"). The Helm chart is the production-grade target for enterprise self-hosting.

## Behaviours

### Docker Compose — local dev / alpha demo

- [ ] `docker compose up` starts Postgres 16, Redis 7, backend (uvicorn), and frontend (Vite dev server)
- [ ] Backend connects to Postgres via `DATABASE_URL` and Redis via `REDIS_URL`
- [ ] Backend auto-migrates schema via Alembic + AsyncPostgresSaver on startup
- [ ] Frontend proxies `/api` to backend at port 8000; WebSocket passthrough works for real-time updates
- [ ] SQLite mode available for zero-dependency local dev (no Postgres/Redis required)
- [ ] MariaDB override via `docker compose -f docker-compose.yml -f docker-compose.mariadb.yml up`
- [ ] `docker compose -f docker-compose.test.yml up db-test` provides isolated Postgres for pytest
- [ ] Observability stack (otel-collector, Prometheus, Grafana) available via `--profile observability`
- [ ] Docker healthcheck on Postgres prevents backend start dependency race
- [ ] Hot-reload: backend `src/` and frontend `src/` bind-mounted for live iteration

### Docker build — container images

- [ ] Backend image builds from `python:3.12-slim` via uv-based install
- [ ] Frontend image builds as nginx serving the Vue 3 SPA
- [ ] Images publishable to `ghcr.io/anomalyco/modulo` (or custom registry)

### Helm chart — production Kubernetes deployment

- [ ] Chart deploys backend (FastAPI) and frontend (nginx/Vue) as separate Deployments
- [ ] Bitnami Postgres 16 sub-chart for database (can be disabled for external DB)
- [ ] Bitnami Redis 7 sub-chart for task queue/pub-sub (can be disabled for external Redis)
- [ ] Backend pod has liveness, readiness, and startup probes pointed at `/healthz`
- [ ] Frontend pod has liveness and readiness probes pointed at nginx root
- [ ] Security context: non-root user (UID 1000 backend / 101 frontend), read-only root filesystem, all capabilities dropped
- [ ] Secrets (SECRET_KEY, FERNET_KEY, DATABASE_URL, REDIS_URL) stored as k8s `Secret` — never in pod spec env
- [ ] Secrets auto-generated via `randAlphaNum` when not explicitly provided
- [ ] Existing secrets preserved on upgrade (not regenerated)
- [ ] Ingress with TLS termination and optional `MODULO_PUBLIC_URL` host routing
- [ ] HorizontalPodAutoscaler for backend and frontend with CPU/memory thresholds
- [ ] PodDisruptionBudget for HA setups
- [ ] NetworkPolicy restricting ingress/egress per component
- [ ] Helm chart version follows semver with AppVersion tag
- [ ] `helm test` runs `/healthz` connectivity checks

### Edge cases

- [ ] Docker Compose observability profile disabled by default (no resource overhead)
- [ ] SQLite mode skips Redis entirely — rate limiter falls back to in-memory
- [ ] Frontend dev proxy works with both HTTP and WebSocket
- [ ] Helm chart can deploy to namespaces other than `modulo`
- [ ] Existing Postgres/Redis can be used instead of sub-charts (external DB mode)

## Known Gaps

- No end-to-end Helm deployment test in CI
- No Helm chart repository or CI-published chart artifact
- No documented upgrade path between chart versions
- No automated backup/restore hooks in Helm chart
- No multi-replica backend deployment tested (advisory locks, rate limiter, scheduler)
