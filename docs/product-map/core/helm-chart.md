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
- [x] Helm configmap now exposes 22 env vars (up from 4) — **added MODULO_DB, MODULO_DEBUG, MODULO_TELEMETRY_ENABLED, MODULO_OTEL_SERVICE_NAME, MODULO_AUTH_RATE_LIMIT_ENABLED, MODULO_AUTH_MAX_ATTEMPTS, MODULO_AUTH_WINDOW_SECONDS, INACTIVITY_TIMEOUT_MINUTES, MODULO_SSE_MAX_CONNECTIONS_PER_ORG, MODULO_SSE_MAX_CONNECTIONS_PER_USER, MODULO_CSRF_ENABLED, MODULO_CSRF_EXEMPT_PATHS, MODULO_PLUGIN_DISCOVERY, MODULO_MAX_LOCAL_CONCURRENCY, MODULO_SECRETS_BACKEND, MODULO_LOG_LEVEL** — remaining unexposed vars (OIDC/SAML vars, Vault/AWS creds) are less commonly needed or already in secrets **
- [x] Helm secret now stores up to 10 secrets (up from 4) — **added MODULO_ADMIN_PASSWORD, MODULO_SCIM_TOKEN, MODULO_RATELIMIT_BYPASS_TOKEN, MODULO_USERS, MODULO_LICENSE_KEY** — Vault/AWS secrets-backend credentials remain unexposed (edge-case backends) **
- [x] `$(DATABASE_PASSWORD)` and `$(REDIS_PASSWORD)` shell-style variable references in `secrets.yaml` URL construction — **Fixed: secrets.yaml no longer constructs URLs with shell variable placeholders. DATABASE_URL/REDIS_URL are only stored in the Secret when explicitly provided. When using Bitnami Postgres/Redis sub-charts, URLs are constructed at runtime in deployment-backend.yaml via K8s env var expansion (`$(VAR)` syntax) with `secretKeyRef` to the Bitnami-generated secrets. Requires `.Values.postgresql.auth.password` / `.Values.redis.auth.password` to be explicitly set.**
- [ ] Helm chart has no MariaDB/MySQL support — docker-compose.mariadb.yml exists but chart only offers postgresql sub-chart
- [ ] Helm chart has no observability stack — docker-compose.local.yml has otel-collector + Prometheus + Grafana profile, but no helm equivalent
- [x] Backend `startupProbe.enabled` defaults to `true` (was `false`) — **Fixed: values.yaml changed to `true` with adjusted periodSeconds=10 to match readiness probe pattern**
- [ ] NetworkPolicy has hardcoded sub-chart pod selector labels (`{{ .Release.Name }}-postgresql`, `{{ .Release.Name }}-redis-master`) — may not match Bitnami chart label output if `nameOverride` or `fullnameOverride` is used
- [ ] nginx `proxy_set_header Connection "upgrade"` is set for ALL `/api/` requests unconditionally — could interfere with non-WebSocket proxied requests. Mitigation: FastAPI/Uvicorn ignores `Connection: upgrade` for non-WebSocket requests; low impact in practice.
- [ ] Frontend pod has no readiness/liveness probe on the actual nginx status endpoint — probes hit `/` which always returns 200 even if the SPA isn't fully built

### QA History
- 2026-07-03: Cross-cutting QA (feat-core-helm-chart, index 96): Fixed stale ghcr.io-publishing checkbox ([ ]→[x]). Removed stale "no CI/CD workflow for ghcr.io" known gap. Added 4 new known gaps (no docker-build CI gate, no SQLite Compose profile, Dockerfile.prod hardcoded nginx config, dual backend Dockerfile divergence risk). Added code paths to frontmatter (Dockerfile.backend, frontend/Dockerfile.prod, frontend/Dockerfile, publish-images.yml, docker-build.yml, entrypoint.sh). Status: partial.
- 2026-07-06: Cross-cutting QA: Added 7 new edge cases (env var gaps in configmap, env var gaps in secrets, DATABASE_URL password interpolation bug, missing MariaDB chart, missing observability chart, startupProbe default, NetworkPolicy label reliability). Checked all helm templates and cross-referenced against backend settings.py (73 env vars). Created website docs stub at `Website/modulo-website/src/docs/deployment.md`.
- 2026-07-06: Cross-cutting QA (improve-architecture, index 227): Fixed CRITICAL — `$(DATABASE_PASSWORD)`/`$(REDIS_PASSWORD)` literal text in secrets.yaml URL construction. Now uses K8s env var expansion (`$(VAR)`) with `secretKeyRef` to Bitnami-generated secrets in deployment-backend.yaml. Removed auto-construction from secrets template (sensitive data no longer generated with placeholders). Fixed MAJOR — expanded configmap from 4 to 22 env vars (added MODULO_DB, MODULO_DEBUG, MODULO_TELEMETRY_ENABLED, MODULO_OTEL_SERVICE_NAME, MODULO_AUTH_RATE_LIMIT_ENABLED, MODULO_AUTH_MAX_ATTEMPTS, MODULO_AUTH_WINDOW_SECONDS, INACTIVITY_TIMEOUT_MINUTES, MODULO_SSE_MAX_CONNECTIONS_PER_ORG, MODULO_SSE_MAX_CONNECTIONS_PER_USER, MODULO_CSRF_ENABLED, MODULO_CSRF_EXEMPT_PATHS, MODULO_PLUGIN_DISCOVERY, MODULO_MAX_LOCAL_CONCURRENCY, MODULO_SECRETS_BACKEND). Fixed MAJOR — expanded secrets from 4 to 10 (added MODULO_ADMIN_PASSWORD, MODULO_SCIM_TOKEN, MODULO_RATELIMIT_BYPASS_TOKEN, MODULO_USERS, MODULO_LICENSE_KEY). Fixed MAJOR — `startupProbe.enabled` now defaults to `true` with periodSeconds=10. Removed 3 resolved Known Gaps. Status: partial.

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
- **URL auto-construction with Bitnami sub-charts now requires explicit password**: When postgresql/redis sub-charts are enabled without providing `secrets.DATABASE_URL`, the chart expects `.Values.postgresql.auth.password` / `.Values.redis.auth.password` to be set. The K8s env var expansion references the Bitnami-generated Secrets (`{{ .Release.Name }}-postgresql`, `{{ .Release.Name }}-redis`). If the user sets `nameOverride`/`fullnameOverride` on the sub-charts, the secret names may differ — the user must then provide explicit `secrets.DATABASE_URL` and `secrets.REDIS_URL` instead.
- **~50 advanced env vars still unexposed** via Helm configmap: OIDC/SAML vars (modulo_oidc_providers, modulo_saml_*), Vault credentials (vault_*), AWS credentials (aws_*), CORS max age, CSP monitor domains, SSE zombie timeout, FERNET_KEY_OLD. These are used only in edge-case configurations and can be added via `extraEnv` / `extraEnvFrom`.
- **Vault/AWS secrets-backend credentials not in helm secrets**: MODULO_SECRETS_BACKEND=vault or =aws requires vault_*/aws_* env vars that are not in the chart's secrets template. Users must provide these via `extraEnvFrom` referencing their own Secrets.
