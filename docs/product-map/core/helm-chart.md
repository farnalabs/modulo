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

- [x] `docker compose up` starts Postgres 16, Redis 7, backend (uvicorn), and frontend (Vite dev server)
- [x] Backend connects to Postgres via `DATABASE_URL` and Redis via `REDIS_URL`
- [x] Backend auto-migrates schema via Alembic + AsyncPostgresSaver on startup
- [ ] Frontend proxies `/api` to backend at port 8000; WebSocket passthrough works for real-time updates — **Helm nginx config missing WebSocket headers (`Upgrade`, `Connection`); Vite dev proxy handles it but nginx does not**
- [ ] SQLite mode available for zero-dependency local dev (no Postgres/Redis required)
- [x] MariaDB override via `docker compose -f docker-compose.yml -f docker-compose.mariadb.yml up`
- [x] `docker compose -f docker-compose.test.yml up db-test` provides isolated Postgres for pytest
- [x] Observability stack (otel-collector, Prometheus, Grafana) available via `--profile observability`
- [x] Docker healthcheck on Postgres prevents backend start dependency race
- [x] Hot-reload: backend `src/` and frontend `src/` bind-mounted for live iteration

### Docker build — container images

- [x] Backend image builds from `python:3.12-slim` via uv-based install
- [ ] Frontend image builds as nginx serving the Vue 3 SPA — **current frontend Dockerfile uses Vite dev server, not production nginx** (create `Dockerfile.prod`)
- [ ] Images publishable to `ghcr.io/anomalyco/modulo` (or custom registry)

### Helm chart — production Kubernetes deployment

- [x] Chart deploys backend (FastAPI) and frontend (nginx/Vue) as separate Deployments
- [x] Bitnami Postgres 16 sub-chart for database (can be disabled for external DB)
- [x] Bitnami Redis 7 sub-chart for task queue/pub-sub (can be disabled for external Redis)
- [x] Backend pod has liveness, readiness, and startup probes pointed at `/healthz`
- [x] Frontend pod has liveness and readiness probes pointed at nginx root
- [x] Security context: non-root user (UID 1000 backend / 101 frontend), read-only root filesystem, all capabilities dropped
- [x] Secrets (SECRET_KEY, FERNET_KEY, DATABASE_URL, REDIS_URL) stored as k8s `Secret` — never in pod spec env
- [x] Secrets auto-generated via `randAlphaNum` when not explicitly provided
- [ ] Existing secrets preserved on upgrade (not regenerated) — **BUG: secrets.yaml uses `pre-install,pre-upgrade` hook, causing regeneration on every upgrade**
- [x] Ingress with TLS termination and optional `MODULO_PUBLIC_URL` host routing
- [ ] HorizontalPodAutoscaler for backend and frontend with CPU/memory thresholds — **HPA template only covers backend (deployment-backend), missing frontend HPA**
- [ ] PodDisruptionBudget for HA setups — **values.yaml has `podDisruptionBudget` blocks but no template (pdb.yaml) renders them**
- [ ] NetworkPolicy restricting ingress/egress per component — **no networkpolicy.yaml template exists**
- [x] Helm chart version follows semver with AppVersion tag
- [x] `helm test` runs `/healthz` connectivity checks

### Edge cases

- [x] Docker Compose observability profile disabled by default (no resource overhead)
- [ ] SQLite mode skips Redis entirely — rate limiter falls back to in-memory — **backend code not verified from infra files**
- [ ] Frontend dev proxy works with both HTTP and WebSocket — **Helm nginx config needs WebSocket headers; Vite dev proxy handles it**
- [x] Helm chart can deploy to namespaces other than `modulo`
- [x] Existing Postgres/Redis can be used instead of sub-charts (external DB mode)

## Known Gaps

- No end-to-end Helm deployment test in CI
- No Helm chart repository or CI-published chart artifact
- No documented upgrade path between chart versions
- No automated backup/restore hooks in Helm chart
- No multi-replica backend deployment tested (advisory locks, rate limiter, scheduler)
- No NetworkPolicy template — Kubernetes network isolation unenforced
- No PodDisruptionBudget template — HA setups lack graceful disruption budgets
- Helm chart secrets.yaml regenerates secrets on upgrade (pre-upgrade hook bug)
- Frontend HPA only covers backend; frontend lacks autoscaling
- No production nginx-based frontend Dockerfile (`Dockerfile.prod`)
- No CI/CD workflow for building/publishing Docker images to ghcr.io
- Nginx config lacks WebSocket proxy headers (`Upgrade`, `Connection`)
