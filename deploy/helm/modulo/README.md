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
  --set backend.env.MODULO_USERS="admin:$(openssl passwd -6 your-admin-password)"
```

## Configuration

### External Postgres (Default)

The chart does NOT deploy Postgres by default. Modulo requires **two distinct Postgres roles**:

| Role | Purpose | Used by |
|------|---------|---------|
| `modulo_app` | Restricted runtime role — DML only, no superuser, no BYPASSRLS | `DATABASE_URL` (backend, SAQ workers) |
| `modulo` | Admin/superuser role — migrations, role bootstrap, DDL | `DATABASE_ADMIN_URL` (entrypoint only) |

The `modulo_app` role is **created automatically** by `bootstrap_role` on startup — the operator only needs to provision the admin role with sufficient privileges (superuser or CREATEROLE + CREATEDB).

Set these values:

```yaml
postgres:
  enabled: false  # Use external Postgres
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
  enabled: true
  embedded: false  # Use external Redis
  host: your-redis-host.cache.amazonaws.com
  port: 6379
  db: 0
  existingSecret: modulo-secrets  # Secret with "password" key
```

### Embedded Redis (Development)

```yaml
redis:
  enabled: true
  embedded: true  # Deploy Redis in-cluster
```

### Required Values

The chart **fails at `helm install`** if these values are not supplied (Helm's `required` function enforces this):

| Value | Purpose | Format |
|---|---|---|
| `backend.env.SECRET_KEY` | JWT signing key — all API tokens are signed with this | Any string >= 32 bytes. Generate with: `openssl rand -base64 32` |
| `backend.env.FERNET_KEY` | Connector credential encryption — stored secrets are encrypted at rest with this | URL-safe base64 Fernet key, >= 32 bytes. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

Both are validated at runtime by Pydantic (`_MIN_KEY_LEN = 32` in `settings.py`). The chart renders them into the Kubernetes Secret and all workloads (`backend`, `saq-runner`, `saq-system`) consume them from there.

**Optional but recommended:**

| Value | Purpose |
|---|---|
| `backend.env.MODULO_USERS` | Admin user credentials (`user:password` format). Without this, no login is possible — the app warns at boot but does not crash. |
| `backend.env.SAQ_AUTH_USERNAME` / `SAQ_AUTH_PASSWORD` | SAQ system worker auth. Only needed if `saq-system` is exposed externally. |

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
# Deploy (SECRET_KEY and FERNET_KEY are mandatory — helm install fails without them)
helm install modulo ./deploy/helm/modulo \
  -f deploy/helm/modulo/values.eks.example.yaml \
  --set postgres.password=<rds-password> \
  --set backend.env.SECRET_KEY=$(openssl rand -base64 32) \
  --set backend.env.FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
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
  --set backend.env.MODULO_USERS="admin:admin"
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
- [ ] Live cluster validation (FAR-1053)

## Follow-ups

- **FAR-1053:** Live cluster validation (kind + EKS)
- CI `helm lint` job (requires `workflow`-scoped token)
- Redis persistence (PVC) for embedded mode
- NetworkPolicy templates
