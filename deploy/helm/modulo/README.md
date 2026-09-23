# Modulo Helm Chart

Vendor-neutral Helm chart for the Modulo stack. Deploys backend API, SAQ workers, frontend SPA, and Redis. Postgres is external/optional.

## Prerequisites

- Kubernetes 1.25+
- Helm 3.10+
- A Postgres 16 database (external or embedded)
- Redis 7+ (external or embedded)

## Quick Start

```bash
# Add the chart (requires two Postgres roles: admin + app)
helm install modulo ./deploy/helm/modulo \
  --set postgres.host=your-rds-host \
  --set postgres.password=your-password \
  --set postgres.username=modulo_app \
  --set postgres.adminUsername=modulo \
  --set backend.env.SECRET_KEY=$(openssl rand -hex 32) \
  --set backend.env.FERNET_KEY=$(openssl rand -hex 32) \
  --set backend.env.MODULO_USERS="admin:$(openssl passwd -6 your-admin-password)" \
  --set backend.env.SAQ_AUTH_USERNAME=admin \
  --set backend.env.SAQ_AUTH_PASSWORD=$(openssl rand -hex 16)
```

## Configuration

### External Postgres (Default)

The chart does NOT deploy Postgres by default. Modulo requires **two distinct Postgres roles**:

| Role | Purpose | Used by |
|------|---------|---------|
| `modulo_app` | Restricted runtime role — DML only, no superuser, no BYPASSRLS | `DATABASE_URL` (backend, SAQ workers) |
| `modulo` | Admin/superuser role — migrations, role bootstrap, DDL | `DATABASE_ADMIN_URL` (entrypoint only) |

The `modulo_app` role is **created automatically** by `bootstrap_role` on startup — the operator only needs to provision the admin role with sufficient privileges (superuser or CREATEROLE + CREATEDB). A third role, `modulo_system` (LOGIN, BYPASSRLS — see `postgres.systemUsername`), is created by the same bootstrap with the shared password; the chart wires it into `MODULO_SYSTEM_DATABASE_URL`, without which the `dispatcher_reconcile` system cron refuses to run and backend readiness stays 503 forever.

Set these values:

```yaml
postgres:
  host: your-rds-host.amazonaws.com
  port: 5432
  database: modulo
  username: modulo_app       # Restricted runtime role (DATABASE_URL)
  adminUsername: modulo      # Admin/superuser role (DATABASE_ADMIN_URL)
  existingSecret: modulo-secrets  # Secret with "password" key
```

Or set `postgres.password` directly (rendered into a Secret). The password is shared between both roles unless your setup requires separate credentials.

### External Redis (Default)

```yaml
redis:
  embedded: false  # Use external Redis
  host: your-redis-host.cache.amazonaws.com
  port: 6379
  db: 0
  existingSecret: modulo-secrets  # Secret with "password" key
```

### Embedded Redis (Development)

```yaml
redis:
  embedded: true      # Deploy Redis in-cluster
  # password: secret  # Optional. Sets --requirepass on the container and embeds
                     # the credential in REDIS_URL; the probes authenticate too.
```

`redis.embedded` is the only switch that controls the embedded Redis Deployment.
(`postgres.enabled`, `redis.enabled` and the former `redisChart.enabled` were
dead config and have been removed. Postgres is always external.)

<a id="required-values"></a>

### Required Values

This section is the canonical reference for the chart's mandatory values; other
files point here instead of restating the failure modes.

The chart **fails at `helm install`** if these values are not supplied (Helm's `required` function enforces this):

| Value | Purpose | Format |
|---|---|---|
| `backend.env.SECRET_KEY` | JWT signing key — all API tokens are signed with this | Any string >= 32 bytes. Generate with: `openssl rand -base64 32` |
| `backend.env.FERNET_KEY` | Connector credential encryption — stored secrets are encrypted at rest with this | URL-safe base64 Fernet key, >= 32 bytes. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `backend.env.SAQ_AUTH_USERNAME` / `SAQ_AUTH_PASSWORD` | SAQ system worker web-UI auth. **The saq-system worker fail-closes without them** — it crash-loops, `dispatcher_reconcile` never runs, and backend `/healthz/ready` stays 503 forever (the backend pod never becomes Ready). | **Non-empty strings.** Generate with: `openssl rand -hex 16`. The fail-closed check (`saq_worker._assert_system_auth_configured`) is truthiness-only, so an **empty string is rejected too** and crash-loops saq-system at boot. |

The two keys in the first two rows are validated at runtime by Pydantic (`_MIN_KEY_LEN = 32` in `settings.py`); the SAQ auth pair is enforced by the worker's fail-closed boot check, not Pydantic. The chart renders all of them into the Kubernetes Secret and all workloads (`backend`, `saq-runner`, `saq-system`) consume them from there.

**Optional but recommended:**

| Value | Purpose |
|---|---|
| `backend.env.MODULO_USERS` | Admin user credentials (`user:password` format). Without this, no login is possible — the app warns at boot but does not crash. |

### Secrets Management

No literal secrets live in `values.yaml`. Use one of:

1. **Inline values** (recommended) — set `postgres.password`, `backend.env.SECRET_KEY`, `backend.env.FERNET_KEY` directly (rendered into a Secret)
2. **existingSecret** — reference a pre-created Kubernetes Secret for Postgres/Redis credentials

### Image Digest Pinning

The chart supports digest-pinned images for reproducibility:

```yaml
backend:
  image:
    repository: ghcr.io/farnalabs/modulo/backend
    digest: sha256:abc123...  # Preferred: immutable reference
    tag: "latest"             # Fallback: mutable tag
```

**Trade-off:** Digest pinning ensures exact image reproducibility but requires manual updates when new images are published. Tag-based references are simpler but may pull different images over time.

When both are set the digest wins, and the chart renders a single image
reference (`repository@digest`) — a tag is only used when no digest is set.

### Connection URLs and Secret keys

`DATABASE_URL`, `DATABASE_ADMIN_URL`, and `REDIS_URL` are always present in the
chart Secret, even when empty, because the workloads consume them via
non-optional `secretKeyRef` entries. A missing key would leave pods in
`CreateContainerConfigError`; an empty value surfaces as a normal application
startup error instead. `REDIS_PASSWORD` is rendered only when
`redis.password` (or `redis.existingSecret`) is set.

### Image Pull Secrets (Private GHCR Images)

The GHCR images (`ghcr.io/farnalabs/modulo/backend`, `.../frontend`) are private.
Set `imagePullSecrets` to pull them:

```yaml
# 1. Create the secret
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=<github-username> \
  --docker-password=<github-pat>

# 2. Reference it in values
imagePullSecrets:
  - name: ghcr-secret
```

The secret is rendered into both the ServiceAccount and every pod spec.
Public images would remove the need for this.

## EKS Deployment

See `values.eks.example.yaml` for a validated EKS configuration using AWS managed services (RDS, ElastiCache, ALB).

```bash
# Deploy (SECRET_KEY, FERNET_KEY and SAQ auth are mandatory — see Required Values)
helm install modulo ./deploy/helm/modulo \
  -f deploy/helm/modulo/values.eks.example.yaml \
  --set postgres.password=<rds-password> \
  --set backend.env.SECRET_KEY=$(openssl rand -base64 32) \
  --set backend.env.FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  --set backend.env.SAQ_AUTH_USERNAME=admin \
  --set backend.env.SAQ_AUTH_PASSWORD=<saq-auth-password>
```

## Local Development (kind)

```bash
# Create kind cluster
kind create cluster --name modulo-dev

# Deploy with embedded Redis (requires two Postgres roles: admin + app)
helm install modulo ./deploy/helm/modulo \
  --set redis.embedded=true \
  --set postgres.host=host.docker.internal \
  --set postgres.password=changeme \
  --set postgres.username=modulo_app \
  --set postgres.adminUsername=modulo \
  --set backend.env.SECRET_KEY=dev-secret-key-not-for-production-32b! \
  --set backend.env.FERNET_KEY=dev-fernet-key-not-for-production-32b!! \
  --set backend.env.MODULO_USERS="admin:admin" \
  --set backend.env.SAQ_AUTH_USERNAME=admin \
  --set backend.env.SAQ_AUTH_PASSWORD=admin
```

## Upgrade

```bash
helm upgrade modulo ./deploy/helm/modulo \
  -f deploy/helm/modulo/values.yaml
```

## Uninstall

```bash
helm uninstall modulo
kubectl delete namespace modulo  # If namespace.create=true
```

## Architecture

### Services

| Service | Description | Port |
|---------|-------------|------|
| `backend` | FastAPI API server | 8000 |
| `saq-runner` | Pipeline job executor | N/A |
| `saq-system` | Scheduler + system crons | 8081 |
| `frontend` | Vue SPA (nginx) | 80 |
| `redis` | Queue backend (embedded only) | 6379 |

### Security

- **Pod Security Admission:** `restricted` enforcement at namespace level
- **Security Context:** `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: ALL`
- **seccompProfile:** `RuntimeDefault` on all pods
- **readOnlyRootFilesystem:** Enabled on all containers. Frontend nginx uses `command: ["nginx", "-g", "daemon off;"]` to skip the docker-entrypoint (which writes into `/etc/nginx/conf.d/`); the nginx config is provided via a ConfigMap mount. Backend/SAQ runners use writable `/tmp` emptyDir mounts.

### Resource Management

- **ResourceQuota:** Namespace-level limits for CPU, memory, and pod count
- **LimitRange:** Default container resource requests and limits
- **HPA:** Optional autoscaling for backend and frontend

## Validation Status

- [x] `helm lint` passes with default values
- [x] `helm lint` passes with EKS example values
- [x] `helm template` renders without errors
- [x] **Live EKS validation (FAR-1052, 2026-09-23):** deployed end-to-end on a
  real EKS cluster (`modulo-validation`, EKS 1.34, 2× `m7i-flex.large` managed
  nodes, in-cluster embedded Redis, Postgres external in a sibling namespace,
  images `365370368472.dkr.ecr.us-east-1.amazonaws.com/modulo/{backend,frontend}:dev-20260923`).
  All workloads became Ready with backend/worker pods spread across **both**
  nodes (no co-location); backend `/healthz/ready` returned 200 with
  `dispatcher_reconcile` and the full gate list green — proving the FAR-1158
  readiness split (liveness `/healthz`, readiness `/healthz/ready`) holds on
  multi-node Kubernetes. Two gaps found and fixed during this run: SAQ system
  auth was documented as optional (it is fail-closed required — see Required
  Values), and `MODULO_SYSTEM_DATABASE_URL` was not wired by the chart (without
  it `dispatcher_reconcile` never runs and readiness never passes).
- [ ] Live kind validation (FAR-1053)

## Follow-ups

- **FAR-1053:** Live kind validation (EKS done — see Validation Status)
- CI `helm lint` job (requires `workflow`-scoped token)
- Redis persistence (PVC) for embedded mode
- NetworkPolicy templates
