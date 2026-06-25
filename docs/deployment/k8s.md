# Kubernetes Deployment Guide

This guide walks through deploying Modulo on Kubernetes using kustomize-based
manifests at `deploy/k8s/`.

---

## 1. Prerequisites

| Component | Requirement |
|---|---|
| **Kubernetes cluster** | Minimum 2 vCPU, 4 GB RAM (dev); 8 vCPU, 16 GB RAM (prod) |
| **kubectl** | v1.28+ ([install](https://kubernetes.io/docs/tasks/tools/)) |
| **kustomize** | Built into kubectl v1.14+ (`kubectl kustomize --help`) |
| **helm** (optional) | For cert-manager / external-secrets / sealed-secrets ([install](https://helm.sh/docs/intro/install/)) |
| **Ingress controller** | nginx-ingress, Traefik, or cloud LB. Guide assumes `ingress-nginx` ([install](https://kubernetes.github.io/ingress-nginx/deploy/)) |
| **cert-manager** | v1.14+ for automated TLS ([install](https://cert-manager.io/docs/installation/)) |
| **Metrics Server** | Required for HPA (`kubectl top pods`) ([install](https://github.com/kubernetes-sigs/metrics-server)) |

---

## 2. Quick Start (5 min)

```bash
# From the codebase root

# 1. Create namespace
kubectl create namespace modulo-dev

# 2. Deploy everything (backend, frontend, Postgres, Redis, ingress)
kubectl apply -k deploy/k8s/overlays/dev -n modulo-dev

# 3. Wait for all deployments to become available
kubectl wait --for=condition=available deployment --all -n modulo-dev --timeout=120s

# 4. Verify
kubectl get pods -n modulo-dev
kubectl get ingress -n modulo-dev
```

Your dev instance is now accessible at the host specified in
`deploy/k8s/overlays/dev/ingress-patch.yaml` (default: `dev.example.com`).

---

## 3. Secret Management

Modulo requires five secrets. Choose one option below.

### Required Secrets

| Key | Description | Example |
|---|---|---|
| `SECRET_KEY` | JWT signing key, 32+ bytes | `openssl rand -base64 32` |
| `FERNET_KEY` | Fernet encryption key, 44-char base64 | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DATABASE_URL` | Async Postgres DSN | `postgresql+asyncpg://modulo:pass@modulo-postgres:5432/modulo` |
| `REDIS_URL` | Redis connection string | `redis://modulo-redis:6379/0` |
| `POSTGRES_PASSWORD` | Raw Postgres password | `openssl rand -base64 16` |

### Option A: Sealed Secrets (recommended for GitOps)

```bash
# 1. Install sealed-secrets controller
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm install sealed-secrets sealed-secrets/sealed-secrets \
  --namespace sealed-secrets --create-namespace

# 2. Fetch the public cert
kubeseal --fetch-cert > pub-cert.pem
kubeseal --fetch-cert --controller-namespace sealed-secrets > pub-cert.pem

# 3. Create a plain secret file (DO NOT COMMIT THIS)
cat > secret.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: modulo-secrets
  namespace: modulo-prod
type: Opaque
stringData:
  SECRET_KEY: "$(openssl rand -base64 32)"
  FERNET_KEY: "$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
  DATABASE_URL: "postgresql+asyncpg://modulo:your-password@modulo-postgres:5432/modulo"
  REDIS_URL: "redis://modulo-redis:6379/0"
  POSTGRES_PASSWORD: "your-password"
EOF

# 4. Seal it (output is safe to commit)
kubeseal --format yaml --cert pub-cert.pem < secret.yaml > deploy/k8s/base/sealed-secret.yaml

# 5. Apply (or let ArgoCD sync it)
kubectl apply -f deploy/k8s/base/sealed-secret.yaml -n modulo-prod

# 6. Clean up plaintext
rm secret.yaml
```

The controller decrypts the SealedSecret into a regular `Secret` named
`modulo-secrets` (matching `spec.template.metadata.name` in
`deploy/k8s/base/secrets.yaml`).

### Option B: External Secrets Operator

```bash
# Install
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace
```

Create a `SecretStore` pointing at your backing store, then an `ExternalSecret`:

```yaml
# Example: AWS Secrets Manager
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secrets-store
  namespace: modulo-prod
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth:
        jwt:
          serviceAccountRef:
            name: modulo-sa
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: modulo-secrets
  namespace: modulo-prod
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-store
    kind: SecretStore
  target:
    name: modulo-secrets
  data:
    - secretKey: SECRET_KEY
      remoteRef:
        key: modulo/prod/secrets
        property: SECRET_KEY
    - secretKey: FERNET_KEY
      remoteRef:
        key: modulo/prod/secrets
        property: FERNET_KEY
    - secretKey: DATABASE_URL
      remoteRef:
        key: modulo/prod/database
        property: url
    - secretKey: REDIS_URL
      remoteRef:
        key: modulo/prod/redis
        property: url
    - secretKey: POSTGRES_PASSWORD
      remoteRef:
        key: modulo/prod/postgres
        property: password
```

Supported providers: AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault,
Azure Key Vault, and [others](https://external-secrets.io/latest/provider/).

### Option C: Plain Secrets (dev / local only — never GitOps)

```bash
kubectl create secret generic modulo-secrets -n modulo-dev \
  --from-literal=SECRET_KEY="$(openssl rand -base64 32)" \
  --from-literal=FERNET_KEY="$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")" \
  --from-literal=DATABASE_URL="postgresql+asyncpg://modulo:dev-pass@modulo-postgres:5432/modulo" \
  --from-literal=REDIS_URL="redis://modulo-redis:6379/0" \
  --from-literal=POSTGRES_PASSWORD="dev-pass"
```

> **Warning:** Plain secrets are visible in the cluster and to anyone with
> `get secret` access. Do not use in production or commit to Git.

---

## 4. Configuration

### ConfigMap Reference

All non-sensitive environment variables are set via the `config` ConfigMap
(`deploy/k8s/base/configmap.yaml`). Merge overrides per overlay.

| Variable | Default | Description |
|---|---|---|
| `MODULO_PUBLIC_URL` | `https://modulo.example.com` | Public-facing URL for links, callbacks, CORS |
| `CORS_ORIGINS` | `https://modulo.example.com` | Comma-separated allowed CORS origins |
| `MODULO_DB` | `postgres` | Database backend (only `postgres` supported in k8s) |
| `MODULO_LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MODULO_SECRETS_BACKEND` | `fernet` | Secrets backend type |
| `MODULO_PLUGIN_DISCOVERY` | `true` | Enable automatic plugin discovery |
| `MODULO_OTEL_SERVICE_NAME` | `modulo` | OpenTelemetry service name for traces |
| `MODULO_TELEMETRY_ENABLED` | `false` | Enable OTel telemetry (off by default for data residency) |
| `MODULO_DEMO_MODE` | `false` | Enable demo mode with sample pipelines |
| `MODULO_USERS` | (empty) | Inline user seeding (`user:pass,user2:pass2`) |

### Secret Reference

| Variable | Source | Required | Description |
|---|---|---|---|
| `DATABASE_URL` | Secret | Yes | `postgresql+asyncpg://user:pass@host:5432/db` |
| `SECRET_KEY` | Secret | Yes | JWT signing key, 32+ bytes |
| `FERNET_KEY` | Secret | Yes | Fernet encryption key, 44-char base64 |
| `REDIS_URL` | Secret | Yes | `redis://host:6379/db` |
| `POSTGRES_PASSWORD` | Secret | Yes | Raw Postgres password |

### Setting CORS_ORIGINS

For production, set `CORS_ORIGINS` to your actual domain in the overlay:

```yaml
# deploy/k8s/overlays/prod/kustomization.yaml
configMapGenerator:
  - name: config
    behavior: merge
    literals:
      - CORS_ORIGINS=https://modulo.example.com,https://admin.modulo.example.com
```

For development with a local frontend, include the local dev server:

```yaml
# deploy/k8s/overlays/dev/kustomization.yaml
configMapGenerator:
  - name: config
    behavior: merge
    literals:
      - CORS_ORIGINS=https://dev.example.com,http://localhost:5173
```

### Setting MODULO_PUBLIC_URL

Modulo uses `MODULO_PUBLIC_URL` to generate absolute URLs for webhook callbacks,
email links, and OAuth redirects. It must match your Ingress host.

```yaml
# dev
- MODULO_PUBLIC_URL=https://dev.example.com

# prod
- MODULO_PUBLIC_URL=https://modulo.example.com
```

---

## 5. Storage

### Postgres StatefulSet

Modulo ships with a Postgres 16 StatefulSet (`deploy/k8s/base/postgres-statefulset.yaml`):

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 1
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 10Gi
```

### PVC Sizing

| Environment | Starting Size | Monitor Threshold |
|---|---|---|
| Dev | 10 Gi | — |
| Prod | 10 Gi | Resize when > 60% used |

To resize:

```bash
# Edit the PVC directly (requires CSI expander support)
kubectl edit pvc data-modulo-postgres-0 -n modulo-prod
# Change spec.resources.requests.storage, save and exit
```

To migrate to a larger volume class:

```bash
# 1. Take a backup (see backup docs)
# 2. Create a new StatefulSet with larger PVC template under a different name
# 3. Import the backup
# 4. Update kustomization and re-apply
```

### Backup & Restore

See [Backup & Restore](../operations/backup.md) for the full guide, including:

- Encrypted daily backups via `scripts/backup.py`
- AES-256-CBC encryption with PBKDF2
- Retention pruning (7 daily, 4 weekly, 12 monthly)
- Full restoration walkthrough

Quick reference:

```bash
# Backup
cd /opt/modulo/codebase
uv run scripts/backup.py --output /backups/daily/modulo-backup-$(date +%Y%m%d).tar.gz.enc

# Restore (dry-run first)
uv run scripts/restore.py --input /backups/daily/backup-20260624.tar.gz.enc --dry-run
uv run scripts/restore.py --input /backups/daily/backup-20260624.tar.gz.enc --full
```

---

## 6. Networking

### Ingress TLS with cert-manager

The base Ingress (`deploy/k8s/base/ingress.yaml`) includes a
`cert-manager.io/cluster-issuer` annotation. Before deploying to prod:

1. Ensure cert-manager is installed:
   ```bash
   kubectl get pods -n cert-manager
   ```

2. Create a `ClusterIssuer` for Let's Encrypt production:
   ```yaml
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata:
     name: letsencrypt-prod
   spec:
     acme:
       server: https://acme-v02.api.letsencrypt.org/directory
       email: ops@yourdomain.com
       privateKeySecretRef:
         name: letsencrypt-prod-account-key
       solvers:
         - http01:
             ingress:
               class: nginx
   ```
   ```bash
   kubectl apply -f cluster-issuer.yaml
   ```

3. cert-manager reads the Ingress annotation and automatically provisions a
   `Certificate` resource and the TLS secret.

For dev, use the staging issuer to avoid rate limits:

```yaml
# deploy/k8s/overlays/dev/ingress-patch.yaml
annotations:
  cert-manager.io/cluster-issuer: letsencrypt-staging
```

### WebSocket Support

Modulo uses WebSockets for:
- **HITL** (human-in-the-loop) — real-time approval/rejection streaming
- **Run inspection** — live node output streaming
- **Event broker** — fan-out of `astream_events()` to multiple subscribers

The Ingress already forwards `/ws` to the backend with WebSocket support:

```yaml
# From base/ingress.yaml
- path: /ws
  pathType: Prefix
  backend:
    service:
      name: backend
      port:
        number: 8000
```

The nginx ConfigMap handles the WebSocket upgrade:

```nginx
location /ws {
    proxy_pass http://modulo-backend:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

If using a non-nginx Ingress controller (Traefik, AWS ALB, GKE), ensure it
supports HTTP/1.1 upgrade passthrough. AWS ALB requires
`alb.ingress.kubernetes.io/conditions.ws` and target group stickiness.

### MCP SSE Endpoint Proxy

The MCP (Model Context Protocol) server uses SSE over HTTP. The `/mcp` path
requires the same WebSocket-compatible proxy config as `/ws`:

```nginx
location /mcp {
    proxy_pass http://modulo-backend:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_buffering off;  # required for SSE streaming
}
```

The Ingress and nginx ConfigMap already include `/mcp`. No additional config
is needed unless you're using a non-nginx Ingress.

### Ingress Timeouts

Modulo pipelines can run for minutes. Adjust Ingress timeouts accordingly:

```yaml
annotations:
  nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
  nginx.ingress.kubernetes.io/proxy-send-timeout: "600"
  nginx.ingress.kubernetes.io/proxy-body-size: "50m"
```

---

## 7. Scaling

### HorizontalPodAutoscaler (HPA)

The prod overlay includes an HPA for both backend and frontend
(`deploy/k8s/overlays/prod/hpa.yaml`):

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: backend
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: backend
  minReplicas: 3
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: frontend
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
```

### When to Scale

| Signal | Action |
|---|---|
| CPU consistently > 70% | Increase minReplicas or adjust HPA target |
| Memory consistently > 80% | Increase memory limits or add replicas |
| Pipeline queue backlog growing | Add backend replicas |
| WebSocket connections dropping | Check HPA status, ensure metrics-server is running |

### Max Replicas Guidance

| Component | Recommended Max | Rationale |
|---|---|---|
| Backend | 10 | Stateful WebSocket connections; too many replicas fragments HITL sessions |
| Frontend | 6 | Stateless static assets; easy to scale horizontally |
| Postgres | 1 (read/write) | Use read replicas for analytics queries only |

### Verify HPA is Working

```bash
# Check current metrics
kubectl get hpa -n modulo-prod

# Check cluster resource usage
kubectl top pods -n modulo-prod
kubectl top nodes
```

If `kubectl top pods` returns `error: metrics not available yet`, install
[Metrics Server](https://github.com/kubernetes-sigs/metrics-server).

---

## 8. Monitoring

### Health Checks

All components have liveness + readiness probes defined in the base manifests:

| Component | Liveness | Readiness | Startup |
|---|---|---|---|
| Backend | `GET /healthz` (10s delay, 15s period) | `GET /healthz` (5s delay, 10s period) | — |
| Frontend | `GET /` (10s delay, 15s period) | `GET /` (5s delay, 10s period) | — |
| Postgres | `pg_isready -U modulo` (30s delay, 10s period) | `pg_isready -U modulo` (5s delay, 5s period) | — |
| Redis | TCP socket check :6379 (10s delay, 15s period) | TCP socket check :6379 (5s delay, 10s period) | — |

### Prometheus + Grafana

Modulo exposes metrics at `GET /metrics` on the backend.

1. Deploy the kube-prometheus-stack:
   ```bash
   helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
   helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
     --namespace monitoring --create-namespace
   ```

2. Ensure the backend has a `PodMonitor` or `ServiceMonitor`:
   ```yaml
   apiVersion: monitoring.coreos.com/v1
   kind: ServiceMonitor
   metadata:
     name: modulo-backend
     namespace: modulo-prod
   spec:
     selector:
       matchLabels:
         app.kubernetes.io/component: backend
     endpoints:
       - port: http
         path: /metrics
   ```

3. Import Grafana dashboards:
   - **Kubernetes / Views (Global)**: `https://grafana.com/grafana/dashboards/15757`
   - **Modulo-specific**: `docs/grafana/modulo-dashboard.json`

### Key Metrics to Watch

| Metric | What It Tells You |
|---|---|
| `modulo_pipeline_runs_total` | Pipeline throughput |
| `modulo_pipeline_run_duration_seconds` | Pipeline latency |
| `modulo_hitl_pending_claims` | HITL queue depth |
| `modulo_websocket_connections` | Active WebSocket sessions |
| `container_cpu_usage_seconds_total` | Pod CPU usage (for HPA tuning) |
| `container_memory_working_set_bytes` | Pod memory usage |

---

## 9. Upgrades

### Rolling Update Strategy

Modulo uses the default Kubernetes rolling update (`RollingUpdate` with
`maxSurge: 25%`, `maxUnavailable: 25%`).

```bash
# 1. Update the image tag in your overlay or kustomization.yaml
#    deploy/k8s/overlays/prod/backend-patch.yaml:
#      spec:
#        template:
#          spec:
#            containers:
#              - name: backend
#                image: registry.example.com/modulo-backend:v2.0.0

# 2. Re-apply
kubectl apply -k deploy/k8s/overlays/prod

# 3. Monitor rollout
kubectl rollout status deployment/modulo-backend -n modulo-prod

# 4. Watch pods transition
kubectl get pods -n modulo-prod -w
```

For zero-downtime upgrades, ensure:
- `minReadySeconds: 30` is set on the deployment
- Readiness probe responds correctly before traffic is routed
- Multiple replicas are running (≥ 2 for backend, ≥ 2 for frontend)

### Database Migration

Alembic migrations run automatically on backend startup via the
`run_migrations` startup hook in `modulo.api.main`. This means:

1. A new backend pod starts
2. It acquires a Postgres advisory lock (`pg_advisory_xact_lock`)
3. It runs `alembic upgrade head`
4. It proceeds to serve traffic

Migration failure causes the backend to fail its startup sequence and
eventually restart.

If you need to run migrations manually:

```bash
# Run as a one-off job
kubectl run modulo-migrate --image=registry.example.com/modulo-backend:v2.0.0 \
  -n modulo-prod --rm -it --restart=Never -- \
  alembic upgrade head

# Or create a Job from the backend's command
kubectl create job --from=cronjob/modulo-migrate modulo-migrate-manual -n modulo-prod
```

### Rollback

```bash
# Rollback backend to previous revision
kubectl rollout undo deployment/modulo-backend -n modulo-prod

# Rollback to a specific revision
kubectl rollout undo deployment/modulo-backend -n modulo-prod --to-revision=3

# View history
kubectl rollout history deployment/modulo-backend -n modulo-prod

# Rollback frontend
kubectl rollout undo deployment/modulo-frontend -n modulo-prod
```

**Important:** Rollback does NOT revert database migrations. If you rolled
back to a version that expects an older schema, the backend will fail its
startup migration check. In that case:

```bash
# Check current Alembic version
kubectl exec deploy/modulo-backend -n modulo-prod -- alembic current

# Downgrade to the previous migration (use with extreme caution)
kubectl exec deploy/modulo-backend -n modulo-prod -- alembic downgrade -1
```

---

## 10. Troubleshooting

| Symptom | Likely Cause | Resolution |
|---|---|---|
| **Pod crash loop** | Missing secrets or bad DB connection | `kubectl logs -l app.kubernetes.io/component=backend -n modulo-prod --tail=50`. Verify secrets exist: `kubectl get secret modulo-secrets -n modulo-prod` |
| **Migration failure** | Alembic version conflict or truncated `version_num` column | `kubectl logs deploy/modulo-backend -n modulo-prod --tail=20 \| grep alembic`. If `version_num` is too short (default VARCHAR(32)), manually alter: `ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255);` |
| **Postgres won't start** | PVC not bound or wrong password | `kubectl describe pvc data-modulo-postgres-0 -n modulo-prod`. Check `POSTGRES_PASSWORD` matches the secret. |
| **Ingress / TLS not working** | cert-manager not installed or ClusterIssuer missing | `kubectl describe certificate -n modulo-prod`. Check cert-manager pod logs: `kubectl logs -n cert-manager -l app.kubernetes.io/name=cert-manager` |
| **WebSocket not connecting** | Ingress controller doesn't support WebSocket upgrade | Verify `proxy_set_header Upgrade` and `proxy_set_header Connection "upgrade"` are in your nginx ConfigMap. For AWS ALB, configure target group stickiness and `conditions.ws`. |
| **502 Bad Gateway errors** | Backend readiness probe failing or backend not ready | `kubectl describe pod -l app.kubernetes.io/component=backend -n modulo-prod \| grep -A10 Readiness`. Check if `/healthz` endpoint is responding inside the pod. |
| **HPA not scaling** | Metrics Server not installed or no metrics | `kubectl top pods -n modulo-prod`. If error, install Metrics Server: `kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml` |
| **Frontend shows blank page** | nginx proxy misconfigured or wrong backend service | `kubectl exec deploy/modulo-frontend -n modulo-prod -- cat /etc/nginx/conf.d/default.conf`. Verify `proxy_pass http://modulo-backend:8000` is correct. |
| **Pods stuck in Pending** | Insufficient cluster resources | `kubectl describe pod <name> -n modulo-prod`. Check resource requests vs node allocatable resources. |
| **Database connection refused** | Postgres not ready | `kubectl logs -l app.kubernetes.io/component=postgres -n modulo-prod --tail=20`. Verify `pg_isready` returns `accepting connections`. |

---

## Architecture Overview

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Ingress    │────▶│  Frontend    │     │  Backend    │
│  (TLS)      │     │  (nginx,     │     │  (FastAPI,  │
│             │     │   port 80)   │     │   port 8000)│
│             │     │              │     │             │
│             │     └──────────────┘     └──────┬──────┘
│             │                                 │
│             │     ┌──────────────┐     ┌──────┴──────┐
│             │     │  Postgres    │     │   Redis     │
│             │     │  (Stateful-  │     │  (Deploy-   │
│             │     │   Set, 10Gi) │     │   ment)     │
│             │     └──────────────┘     └─────────────┘
└─────────────┘
```

---

## Directory Structure

```
deploy/k8s/
├── base/
│   ├── kustomization.yaml          # Resource list, common labels, name prefix
│   ├── configmap.yaml              # Shared env vars (non-secret)
│   ├── secrets.yaml                # SealedSecret placeholder
│   ├── backend-deployment.yaml     # FastAPI deployment + service
│   ├── frontend-deployment.yaml    # nginx deployment + service
│   ├── postgres-statefulset.yaml   # StatefulSet + PVC + service
│   ├── redis-deployment.yaml       # Redis deployment + service
│   ├── ingress.yaml                # Ingress with cert-manager TLS
│   └── nginx-config.yaml           # nginx ConfigMap (API proxy, SPA fallback)
├── overlays/
│   ├── dev/
│   │   ├── kustomization.yaml      # 1 replica, min resources
│   │   ├── backend-patch.yaml
│   │   ├── frontend-patch.yaml
│   │   └── ingress-patch.yaml      # dev.example.com
│   └── prod/
│       ├── kustomization.yaml      # 3+ replicas, HPA
│       ├── backend-patch.yaml
│       ├── frontend-patch.yaml
│       ├── ingress-patch.yaml      # modulo.example.com
│       └── hpa.yaml                # HorizontalPodAutoscaler
```
