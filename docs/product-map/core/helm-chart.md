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
  - Dockerfile.backend
  - frontend/Dockerfile.prod
  - frontend/Dockerfile
  - .github/workflows/publish-images.yml
  - .github/workflows/docker-build.yml
  - backend/entrypoint.sh
bdd: []
depends-on: []
unit-tests: []
status: partial
---

# Helm Chart / Docker Compose — Deployment Packaging

Packages Modulo for self-hosted deployment via Docker Compose (dev/alpha/poc) and Helm (production Kubernetes). Docker Compose is the alpha delivery vehicle ("walkable in under 5 minutes from `docker compose up`"). The Helm chart is the production-grade target for enterprise self-hosting.

## Behaviours

### Docker Compose — local dev / alpha demo

- [x] `docker compose up` starts Postgres 16, Redis 7, backend (uvicorn), and frontend (Vite dev server)
- [x] Backend connects to Postgres via `DATABASE_URL` and Redis via `REDIS_URL`
- [x] Backend auto-migrates schema via Alembic + AsyncPostgresSaver on startup
- [x] Frontend proxies `/api` to backend at port 8000; WebSocket passthrough works for real-time updates — **Fixed: nginx config in `_helpers.tpl` now includes WebSocket proxy headers; Vite dev proxy also handles it**
- [ ] SQLite mode available for zero-dependency local dev (no Postgres/Redis required)
- [x] MariaDB override via `docker compose -f docker-compose.yml -f docker-compose.mariadb.yml up`
- [x] `docker compose -f docker-compose.test.yml up db-test` provides isolated Postgres for pytest
- [x] Observability stack (otel-collector, Prometheus, Grafana) available via `--profile observability`
- [x] Docker healthcheck on Postgres prevents backend start dependency race
- [x] Hot-reload: backend `src/` and frontend `src/` bind-mounted for live iteration

### Docker build — container images

- [x] Backend image builds from `python:3.12-slim` via uv-based install
- [x] Frontend image builds as nginx serving the Vue 3 SPA — **Fixed: `Dockerfile.prod` created as multi-stage build (Node build → nginx serve)**
- [x] Images publishable to `ghcr.io/anomalyco/modulo` (or custom registry)

### Helm chart — production Kubernetes deployment

- [x] Chart deploys backend (FastAPI) and frontend (nginx/Vue) as separate Deployments
- [x] Bitnami Postgres 16 sub-chart for database (can be disabled for external DB)
- [x] Bitnami Redis 7 sub-chart for task queue/pub-sub (can be disabled for external Redis)
- [x] Backend pod has liveness, readiness, and startup probes pointed at `/healthz`
- [x] Frontend pod has liveness and readiness probes pointed at nginx root
- [x] Security context: non-root user (UID 1000 backend / 101 frontend), read-only root filesystem, all capabilities dropped
- [x] Secrets (SECRET_KEY, FERNET_KEY, DATABASE_URL, REDIS_URL) stored as k8s `Secret` — never in pod spec env
- [x] Secrets auto-generated via `randAlphaNum` when not explicitly provided
- [x] Existing secrets preserved on upgrade (not regenerated) — **Fixed: secrets.yaml hook changed from `pre-install,pre-upgrade` to `pre-install` only**
- [x] Ingress with TLS termination and optional `MODULO_PUBLIC_URL` host routing
- [x] HorizontalPodAutoscaler for backend and frontend with CPU/memory thresholds — **Fixed: hpa.yaml now creates separate HPAs for both backend and frontend**
- [x] PodDisruptionBudget for HA setups — **Fixed: pdb.yaml template created that renders backend and frontend PDBs from values**
- [x] NetworkPolicy restricting ingress/egress per component — **Fixed: networkpolicy.yaml template created, gated by `networkPolicy.enabled`**
- [x] Helm chart version follows semver with AppVersion tag
- [x] `helm test` runs `/healthz` connectivity checks

### Edge cases

- [x] Docker Compose observability profile disabled by default (no resource overhead)
- [ ] SQLite mode skips Redis entirely — rate limiter falls back to in-memory — **backend code not verified from infra files**
- [x] Frontend dev proxy works with both HTTP and WebSocket — **Fixed: nginx config in `_helpers.tpl` now includes WebSocket proxy headers**
- [x] Helm chart can deploy to namespaces other than `modulo`
- [x] Existing Postgres/Redis can be used instead of sub-charts (external DB mode)
- [ ] Helm configmap only exposes 4 env vars (`CORS_ORIGINS`, `MODULO_PUBLIC_URL`, `LOG_LEVEL`, `MODULO_ENV`) vs 73+ env vars the backend reads — **many non-sensitive env vars (MODULO_DB, OTEL/observability vars, auth rate limiting, SSE config, SAML/SSO vars) are not configurable via helm values and must be added to configmap.data and values.yaml**
- [ ] Helm secret only stores `SECRET_KEY`, `FERNET_KEY`, `DATABASE_URL`, `REDIS_URL` — **missing sensitive env vars: MODULO_ADMIN_PASSWORD, MODULO_SCIM_TOKEN, MODULO_E2B_API_KEY, MODULO_RATELIMIT_BYPASS_TOKEN, MODULO_USERS, Vault/AWS secrets-backend credentials**
- [ ] `$(DATABASE_PASSWORD)` and `$(REDIS_PASSWORD)` shell-style variable references in `secrets.yaml` URL construction are **static text at Secret creation time** — Kubernetes does NOT interpolate `$(VAR)` inside Secret data values. When Bitnami sub-charts auto-generate passwords, the DATABASE_URL/REDIS_URL will contain the literal string `$(DATABASE_PASSWORD)` instead of the actual password. The URL should be constructed at runtime via deployment env vars with `secretKeyRef` to the Bitnami-generated Secret. **This blocks production use with password-auto-generation.**
- [ ] Helm chart has no MariaDB/MySQL support — docker-compose.mariadb.yml exists but chart only offers postgresql sub-chart
- [ ] Helm chart has no observability stack — docker-compose.local.yml has otel-collector + Prometheus + Grafana profile, but no helm equivalent
- [ ] Backend `startupProbe.enabled` defaults to `false` — should be `true` for production to allow slow-starting pods
- [ ] NetworkPolicy has hardcoded sub-chart pod selector labels (`{{ .Release.Name }}-postgresql`, `{{ .Release.Name }}-redis-master`) — may not match Bitnami chart label output if `nameOverride` or `fullnameOverride` is used
- [ ] nginx `proxy_set_header Connection "upgrade"` is set for ALL `/api/` requests unconditionally — could interfere with non-WebSocket proxied requests
- [ ] Frontend pod has no readiness/liveness probe on the actual nginx status endpoint — probes hit `/` which always returns 200 even if the SPA isn't fully built

### QA History
- 2026-07-03: Cross-cutting QA (feat-core-helm-chart, index 96): Fixed stale ghcr.io-publishing checkbox ([ ]→[x]). Removed stale "no CI/CD workflow for ghcr.io" known gap. Added 4 new known gaps (no docker-build CI gate, no SQLite Compose profile, Dockerfile.prod hardcoded nginx config, dual backend Dockerfile divergence risk). Added code paths to frontmatter (Dockerfile.backend, frontend/Dockerfile.prod, frontend/Dockerfile, publish-images.yml, docker-build.yml, entrypoint.sh). Status: partial.
- 2026-07-06: Cross-cutting QA: Added 7 new edge cases (env var gaps in configmap, env var gaps in secrets, DATABASE_URL password interpolation bug, missing MariaDB chart, missing observability chart, startupProbe default, NetworkPolicy label reliability). Checked all helm templates and cross-referenced against backend settings.py (73 env vars). Created website docs stub at `Website/modulo-website/src/docs/deployment.md`.

## Known Gaps

- No end-to-end Helm deployment test in CI
- No Helm chart repository or CI-published chart artifact
- No documented upgrade path between chart versions
- No automated backup/restore hooks in Helm chart
- No multi-replica backend deployment tested (advisory locks, rate limiter, scheduler)
- No `docker-build` CI gate — the `docker-build.yml` workflow only runs on push-to-main and tag, not on PR/merge. Broken Dockerfiles could reach main without feedback.
- No SQLite Docker Compose profile for zero-dependency local dev — PRD §13 requires "SQLite fallback" in "Docker-compose: Postgres + API + UI; SQLite fallback"
- Frontend `Dockerfile.prod` nginx config hardcodes `backend:8000` — works only in Docker Compose context; Helm chart uses its own nginx config from `_helpers.tpl` template
- Two backend Dockerfiles (`backend/Dockerfile` for dev, `Dockerfile.backend` for CI/prod) — risk of divergence; the CI build uses multi-stage with `uv sync --frozen` while the dev file uses `uv pip install --system -e .`
- **$(DATABASE_PASSWORD) shell variable interpolation bug**: `secrets.yaml` embeds `$(DATABASE_PASSWORD)` as static text in the DATABASE_URL. K8s does not interpolate shell variables inside Secret data values. When postgresql sub-chart auto-generates a password, the URL contains the literal string `$(DATABASE_PASSWORD)` instead of the actual password. Same issue for `$(REDIS_PASSWORD)` in REDIS_URL.
- **73 backend env vars vs 4 helm configmap vars**: Helm chart only exposes CORS_ORIGINS, MODULO_PUBLIC_URL, LOG_LEVEL, and MODULO_ENV via ConfigMap. All other non-sensitive settings (MODULO_DB, auth limits, SSE config, CSRF, telemetry, SAML/SSO vars, OTel endpoint, plugin discovery, etc.) are fixed to defaults. Adding them to configmap.data without corresponding values.yaml entries creates discoverability issues.
- **Missing secrets support**: Helm secret only handles 4 core secrets. No support for MODULO_ADMIN_PASSWORD, MODULO_SCIM_TOKEN, MODULO_E2B_API_KEY, MODULO_RATELIMIT_BYPASS_TOKEN, MODULO_USERS, Vault credentials, or AWS credentials. Enterprise deployments need these for full functionality.
