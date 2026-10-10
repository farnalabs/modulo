# Deployment Guide

## Which deployment to use

| Requirement | Use |
|-------------|-----|
| Server or production deployment, multiple operators, replicas, reverse proxy | **Docker Compose** (this guide's main path) |
| Workstation or single-machine evaluation, no Docker installed | **Native install** (`scripts/install.sh`) |

The support split is deliberate: Compose is the server/production path
(vertical scaling, reverse proxy, observability stack), while the native
bundle is the workstation/evaluation path (self-contained, no container
runtime, per-user install root). The native path's requirements and
limitations are in [system-requirements.md](./system-requirements.md) and
the launcher command surface starts at `modulo start`.

---

## Prerequisites

- Python 3.12+
- PostgreSQL 16+
- Redis 8+ (required for the SAQ task queue, scheduling, and coordination)

## Installation

```bash
cd backend
uv sync
```

## Configuration

Set environment variables in `.env`:

```env
DATABASE_URL=postgresql+asyncpg://modulo:modulo@localhost:5434/modulo
SECRET_KEY=<random-64-char-string>
FERNET_KEY=<random-44-char-base64>
```

## Running

```bash
# Apply database migrations first
uv run alembic upgrade heads

# Start the API server
uv run uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000
```

## Observability Stack

The local Docker Compose file includes an optional observability stack behind the `--profile observability` flag:

| Service | Image | Port | Purpose |
|---|---|---|---|
| `otel-collector` | `otel/opentelemetry-collector-contrib` | 4317 (gRPC) | Receives OTLP metrics, exports to Prometheus + file + console |
| `prometheus` | `prom/prometheus` | 9090 | Metrics store with 7-day retention |
| `grafana` | `grafana/grafana` | 3000 | Pre-provisioned dashboards + Prometheus datasource |

### Start

```bash
docker compose -f docker-compose.local.yml --profile observability up -d
```

### URLs

| Service | URL | Credentials |
|---|---|---|
| Grafana | http://localhost:3000 | `admin` / `admin` |
| Prometheus | http://localhost:9090 | – |

### Configuration

Files are in `configs/`:

| File | Purpose |
|---|---|
| `configs/otel-collector.yml` | OTel Collector pipeline: OTLP receiver → batch → Prometheus + debug + file exporters |
| `configs/grafana/datasources/prometheus.yml` | Pre-provisioned Prometheus datasource pointing at `http://prometheus:9090` |
| `configs/grafana/dashboards/dashboard.yml` | Dashboard provider that loads JSON models from `configs/grafana/dashboards/` |

Pre-built Grafana dashboards are loaded automatically from `configs/grafana/dashboards/`:
- `pipeline-performance.json` – run durations, volumes, error rates
- `hitl-review.json` – HITL gate activity, review speed, approval rates
- `cost-tracking.json` – LLM spend by org/model/pipeline

### OTel Integration

To send metrics from your application, configure its OTLP exporter to point at:

```
http://localhost:4317
```

Set the `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable:

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

---

## Health watchdog (Docker Compose)

The root `docker-compose.yml` ships a **`watchdog` service** - a [Gatus](https://gatus.io)
instance that runs **by default**, so `docker compose up -d` starts it along with
the rest of the stack. It exists for the one failure Modulo cannot report about
itself: Modulo being down.

| | |
|---|---|
| **What it monitors** | `GET http://backend:8000/healthz/ready` over the compose network, asserting **both** the HTTP status (`200`) **and** the body's top-level `status` (`ok`) |
| **Why both** | `/healthz/ready` answers `200` with `"status": "degraded"` when a non-gating sub-check is degraded, and only answers `503` for `"unavailable"` - a status-code-only check would pass exactly the degradation it is there to catch |
| **Cadence** | every 60s; alerts after **3 consecutive failures** (~3 minutes, so one blip does not page you), clears after **2 consecutive successes**, and sends a *resolved* message so an outage has a visible end |
| **Dashboard** | <http://127.0.0.1:8082> (loopback-only, like every other published port in this file) |
| **Health** | the service has its own Docker healthcheck, so a wedged watchdog shows as `unhealthy` in `docker ps` |
| **Config** | [`deploy/watchdog/`](../deploy/watchdog/) - config-as-code, mounted read-only into the container |

### Enabling email alerting

Monitoring is on from the first `up`. **Email alerting is off until you set the
SMTP variables** - the same ones the app already uses for HITL email alerts, so
one SMTP setup serves both:

| Variable | Required for watchdog alerts | Purpose |
|---|---|---|
| `SMTP_HOST` | **Yes** | SMTP server hostname |
| `SMTP_PORT` | **Yes** | SMTP server port (e.g. `587`) |
| `EMAIL_FROM` | **Yes** | From-address |
| `ALERT_EMAIL_TO` | **Yes** | Comma-separated recipients |
| `SMTP_USERNAME` | Only if your server requires auth | SMTP username |
| `SMTP_PASSWORD` | Only if your server requires auth | SMTP password |

Leaving all of them unset is a **supported state, not a misconfiguration**: the
watchdog still starts, still probes, still updates the dashboard - it just never
sends mail. It says so once at startup, rather than failing silently:

```text
watchdog: email alerting DISABLED (not set: SMTP_HOST SMTP_PORT EMAIL_FROM ALERT_EMAIL_TO). Monitoring is ON - ... set those variables to enable email alerts (see docs/deployment.md).
```

Gatus adds its own confirmation on the next line
(`Ignoring provider=email due to error=from and to fields are required` when off,
`configuredProviders=[email]` when on), so the state is visible in
`docker compose logs watchdog` either way. Nothing about the missing credentials
can stop the container: with them absent the service starts, runs, and reports
health normally.

Two caveats on the values themselves (both are Gatus behaviour, verified against
the pinned image):

- A literal `$` in a value must be written `$$` - Gatus expands `${VAR}` in the
  config before parsing it, and treats a lone `$` as a variable reference.
- Because expansion happens *before* YAML parsing, a `"` or `\` inside
  `SMTP_PASSWORD` would break the parse. Use a password without those characters
  (or escape them for YAML).

### Notes

- The watchdog does **not** `depends_on` the backend - it has to start when the
  backend does not, because reporting that is its job.
- The config lives in [`deploy/watchdog/config.yaml`](../deploy/watchdog/config.yaml);
  edits apply on the next `docker compose up -d watchdog` (the file is mounted,
  so no rebuild is needed).
- Scope: the root `docker-compose.yml` and the production compose
  (`deploy/compose/docker-compose.prod.yml`). The two probe different targets:
  the production compose runs the all-in-one image, so it probes
  `GET http://modulo:80/healthz/ready` (service `modulo` behind nginx on port
  80 - uvicorn's own `127.0.0.1:8000` is loopback-bound inside the container),
  and its dashboard is loopback-bound at `127.0.0.1:8083`. The Helm chart does
  not have it.

---

## TLS / HTTPS

For production, terminate TLS at a reverse proxy:

**nginx:**
```nginx
server {
    listen 443 ssl;
    server_name modulo.example.com;

    ssl_certificate     /etc/ssl/certs/modulo.crt;
    ssl_certificate_key /etc/ssl/private/modulo.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

**Caddy** (automatic TLS):
```caddyfile
modulo.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Set `MODULO_PUBLIC_URL=https://modulo.example.com` so OAuth redirect URIs and
WebSocket connections use the correct origin.

---

## Migration

The `modulo-migrate` CLI tool exports, imports, and verifies organisation data
between Modulo instances. It is installed as a console script entry point.

### Authentication

Authentication is required for all commands. Provide an admin-level JWT:

```bash
modulo-migrate --token <admin-jwt> export-org <org-id>
```

Or set the `MODULO_ADMIN_SECRET` environment variable (bypasses JWT validation):

```bash
export MODULO_ADMIN_SECRET=<shared-secret>
modulo-migrate export-org <org-id>
```

The token can also be passed via `MODULO_ADMIN_TOKEN` environment variable.

### Commands

#### export-org

Export all org-scoped data (users, pipelines, runs, audit events, library
primitives, connector instances, model backends) as a JSONL bundle with
per-record SHA-256 hashes.

```bash
modulo-migrate export-org <org-id> --output ./backup.jsonl
```

Partial export:

```bash
modulo-migrate export-org <org-id> --output ./pipelines.jsonl --pipelines-only
modulo-migrate export-org <org-id> --output ./users.jsonl --users-only
```

#### import-org

Import from a previously exported JSONL bundle. Conflict resolution strategies:

| Strategy     | Behaviour |
|--------------|-----------|
| `skip`       | Leave existing records untouched (default) |
| `overwrite`  | Replace existing records with imported values |
| `merge`      | Only fill null/empty fields on existing records |

```bash
modulo-migrate import-org <org-id> --input ./backup.jsonl --on-conflict merge
```

Partial import:

```bash
modulo-migrate import-org <org-id> --input ./pipelines.jsonl --pipelines-only
```

#### verify-export

Re-compute hashes on an export file and compare against the stored export hash.

```bash
modulo-migrate verify-export <org-id> --input ./backup.jsonl
```

### Output Format

The export is a JSONL file where:

- **Line 1**: Metadata header with version, export timestamp, and aggregate
  SHA-256 hash of the entire bundle.
- **Subsequent lines**: One JSON object per record, with keys:
  - `__table__` – table name (e.g. `"users"`, `"pipelines"`)
  - `id` – record primary key (string-formatted UUID)
  - `data` – full column data for the record
  - `__hash__` – SHA-256 of the sorted serialised `data`

### Progress Bars

All long-running operations (export, import) display progress bars via `tqdm`,
showing per-table and per-row progress.

### Error Handling

- Import errors are counted per-table (reported as `errors` in the summary).
- Verification exits with code 1 on hash mismatch.
- Admin auth failures exit with a descriptive message.

---

## Break-Glass Admin Recovery Deploy Gate

The break-glass enforcement ships in two deliverables – **(A)** last-admin
prevention + operator role + migration, **(B)** CLI + login-hook consumption +
SQL-predicate deny. The (B) deploy carries a one-time precondition:

1. **Zero live break-glass rows.** Run `modulo-break-glass status --all`
   before deploying (B). A non-zero exit (`5`) means a live row exists –
   resolve it (`deactivate` / `deactivate --force`) before deploying. See
   `docs/operations/break-glass-admin-recovery-runbook.md`.
2. **Expired rows must NOT block deploys.** A row past `break_glass_expires_at`
   is deny-covered by the enforcement code itself; it is a hygiene item for
   the daily sweep, not a deploy blocker.

From (B) onward the daily `status --all` sweep (§8 of the runbook) is the
ongoing monitoring surface.

---

## CORS Configuration

Cross-Origin Resource Sharing (CORS) is configured via environment variables.

**`CORS_ORIGINS`** – Comma-separated list of allowed origins:

```env
CORS_ORIGINS=http://localhost:5173,https://modulo.example.com
```

Each origin must be a full origin including scheme and host, without a trailing slash:
- ✅ `https://modulo.example.com`
- ❌ `https://modulo.example.com/`
- ❌ `*`

**`CORS_MAX_AGE`** – Preflight cache duration in seconds (default: `600` / 10 minutes):

```env
CORS_MAX_AGE=3600
```

### Security recommendations

1. **Never use `*` (wildcard) in production.** If `debug=False` and `CORS_ORIGINS` contains `*`, startup will reject the configuration. Wildcard origins prevent browsers from sending credentials and bypass the security model entirely.
2. **Always explicitly list your frontend origins.** Include both the exact development URL (`http://localhost:5173`) and the production URL (`https://app.modulo.example.com`).
3. **No trailing slashes.** Origins with trailing slashes are rejected at startup.
4. **Per-origin method restrictions** are not supported by the underlying Starlette CORSMiddleware. The allowed methods (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`) apply uniformly to all origins. If per-origin method control is required, configure it at the reverse proxy layer.
5. **CORS is enforced by the browser, not the server.** It does not protect against direct API calls from server-side or non-browser clients. Use authentication and rate limiting for API security.

---

## Related Documentation

For the full environment variable reference, see [`docs/configuration-reference.md`](./configuration-reference.md).

For system requirements (minimum resources, supported databases), see [`docs/system-requirements.md`](./system-requirements.md).

For upgrade procedures on existing deployments, see [`docs/upgrade-process.md`](./upgrade-process.md).

For the production launch checklist, see [`docs/public-launch-checklist.md`](./public-launch-checklist.md).

---

## Environment variable reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | **Yes** | – | `postgresql+asyncpg://user:pass@host:port/db` |
| `SECRET_KEY` | **Yes** | – | 32+ byte random string for JWT signing |
| `FERNET_KEY` | **Yes** | – | 44-char base64 Fernet key for credential encryption |
| `MODULO_USERS` | No | – | Comma-separated `email:password` pairs for initial user seed (plaintext is bcrypt-hashed at seed time; `admin` gets the admin role) |
| `MODULO_DB` | No | `postgres` | Database backend (`postgres`, `sqlite`, `mariadb`, or `mysql`) |
| `REDIS_URL` | **Yes** | `redis://localhost:6379/0` | Redis URL for the SAQ broker, scheduling, event coordination, and rate limiting. The API refuses to boot when this is explicitly empty. |
| `MODULO_PUBLIC_URL` | For SSO | `http://localhost:8000` | Public-facing URL for OAuth redirects |
| `CORS_ORIGINS` | No | `http://localhost:5173` | Comma-separated allowed CORS origins |
| `CORS_MAX_AGE` | No | `600` | Preflight cache max-age in seconds |
| `MODULO_SECRETS_BACKEND` | No | `fernet` | Secrets backend: `fernet`, `vault`, or `aws` |
| `MODULO_OIDC_PROVIDERS` | No | `[]` | JSON array of OIDC provider configs. **Deprecated**; use the admin SSO providers UI. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | – | OTel gRPC/HTTP exporter endpoint |
| `MODULO_E2B_API_KEY` | For E2B | – | E2B sandbox API key for runtime provider |
| `MODULO_ADMIN_SECRET` | No | – | Shared secret for `modulo-migrate` CLI auth |


---

## Deployment Modes

Modulo supports three deployment modes depending on your needs.

### Standalone (single-user, local)

Three environment variables are **required** and have no default: the app
refuses to start without them:

| Variable | Purpose | How to generate |
|----------|---------|-----------------|
| `DATABASE_URL` | SQLAlchemy async DB URL for the application database | `sqlite+aiosqlite:///./modulo.db` for the local SQLite file below |
| `SECRET_KEY` | 32+ byte random string used to sign JWTs | `$(openssl rand -base64 48)` |
| `FERNET_KEY` | 44-char base64 Fernet key used to encrypt stored connector credentials | `$(openssl rand -base64 32)` (base64-encoded 32-byte key) |

The command below sets all three inline, so it is runnable as written. `MODULO_USERS`
seeds the initial admin user (optional but recommended for first login); `MODULO_DB=sqlite`
selects the SQLite backend so no separate database server is needed.

```bash
git clone https://github.com/farnalabs/modulo   # or install the farnalabs-modulo package
cd backend
uv sync
DATABASE_URL=sqlite+aiosqlite:///./modulo.db \
  SECRET_KEY=$(openssl rand -base64 48) \
  FERNET_KEY=$(openssl rand -base64 32) \
  MODULO_USERS=admin:changeme \
  MODULO_DB=sqlite \
  uv run uvicorn modulo.api.main:app --port 8000
```

| Component | How it runs |
|---|---|
| Database | SQLite file (`./modulo.db`), no server process needed |
| Task scheduling | SAQ system-worker cron (`fire_due_triggers`); cron & polling triggers fire via the Redis-backed SAQ workers |
| Task queue | SAQ Redis queue (runs worker); Redis is required |
| Rate limiting | Redis sliding window when Redis is reachable; per-process no-op otherwise |
| Concurrency | Single process, single worker |

Runs and triggers require Redis plus the SAQ workers – see [`docs/quickstart.md`](./quickstart.md) §3b. `REDIS_URL` defaults to `redis://localhost:6379/0`, so start a local Redis and the two SAQ workers; otherwise pipeline runs and cron/polling triggers never execute (and `api/main.py` refuses to boot if `REDIS_URL` is empty).

**What you lose vs. full deployment:**
- **No horizontal scaling** – one process, one user at a time
- **No task durability** – if the SAQ worker crashes mid-run, the run is lost (re-run manually)
- **No distributed rate limiting** – without Redis the limiter is a per-process no-op, so limits don't coordinate across processes

**What you keep:**
- Cron-triggered pipelines ✓ (with Redis + SAQ workers running)
- Polling triggers ✓ (with Redis + SAQ workers running)
- All pipeline features, evals, HITL, connectors ✓
- The SQLite DB file is portable – copy it to another machine and restart `uvicorn` from the new location to pick it up

### Docker Compose (single-server, multi-user)

**1. Create the environment file.** The stack will not start without it: compose
refuses to run (naming the variable) until `MODULO_DB_PASSWORD`, `SECRET_KEY`,
`FERNET_KEY`, `SAQ_AUTH_USERNAME` and `SAQ_AUTH_PASSWORD` are set.

```bash
cp .env.prod.example deploy/compose/.env
# then edit deploy/compose/.env and generate the REQUIRED values:
#   MODULO_DB_PASSWORD=$(openssl rand -hex 24)
#   SECRET_KEY=$(openssl rand -base64 48)
#   FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
#   SAQ_AUTH_PASSWORD=$(openssl rand -hex 24)
```

**2. Start the stack.**

```bash
docker compose -f deploy/compose/docker-compose.prod.yml up -d
```

**3. Verify it can actually execute work.** The readiness probe reports one
check per queue; both must be `ok`:

```bash
curl -s http://localhost/healthz/ready | python -m json.tool
# look for "saq_workers": { "status": "ok", ... }
```

If `saq_workers` is not `ok`, no pipeline run, cron trigger or polling trigger
will execute - see *What the single container runs* below.

**What runs.** The `modulo` service runs the **all-in-one image**: one
container whose `supervisord` starts the API (`uvicorn`), `nginx`, and **both
SAQ queue workers** - `saq-runs` executes pipeline runs and `saq-system` owns
the `system` queue (`fire_due_triggers`, i.e. every cron and polling trigger,
plus the system crons). The workers run *inside* the application container
deliberately: that is what guarantees they receive exactly the same environment
the API receives (compose `environment:` + the project `.env`), with no second
copy of `DATABASE_URL` / `SECRET_KEY` / `FERNET_KEY` to fall out of sync. It is
also why the image is "all-in-one" rather than an API-only artifact.

Worker output lands beside the API and nginx logs, under
`/var/log/supervisor/` inside the container:

```bash
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo \
  tail -f /var/log/supervisor/saq-runs.out.log     # also: saq-system.*, backend.*, nginx.*
```

The native single-install script is a *different* path - it installs a
self-contained bundle (own Python runtime, PostgreSQL, Redis) for a workstation
or single machine without Docker; it does not bootstrap this Compose stack.
Download it to a file and run it rather than streaming it into a shell:
`install.sh` verifies every download's sha256 before executing anything, and a
`curl ... | bash` pipe skips that verification entirely.

```bash
curl -fsSL https://raw.githubusercontent.com/farnalabs/modulo/main/scripts/install.sh -o modulo-install.sh
bash modulo-install.sh
```

| Component | How it runs |
|---|---|
| Database | PostgreSQL 16 (separate container) |
| Application + web tier | uvicorn + nginx inside the single `modulo` container, under supervisord |
| Task scheduling | SAQ system-worker cron (`fire_due_triggers`) - the `saq-system` program in the **same** container's supervisord |
| Task queue | SAQ Redis queue, consumed by the `saq-runs` program in the **same** container's supervisord |
| Rate limiting | Redis sliding window |
| Concurrency | Single backend replica, multiple simultaneous requests |

Redis is required: the dispatcher enqueues every run to SAQ's Redis queue and cron/polling triggers run as Redis-backed SAQ system crons, and `api/main.py` refuses to boot if `REDIS_URL` is empty. `REDIS_URL` defaults to `redis://localhost:6379/0`; if it is explicitly set to an empty value, startup aborts with a `RuntimeError` (see `api/main.py`) instead of a silent fallback.

The two queue workers are **not** separate compose services here - they are
supervisord programs inside the `modulo` container
(`deploy/supervisor/supervisord.conf`), which is what keeps the deployment
"all-in-one": one artifact, one environment, no duplicated credentials block.
They run the same commands the root `docker-compose.yml` uses for its
separate `saq-runner` / `saq-system` services. On an existing deployment
whose `.env` predates this, add the now-required variables before upgrading
(`MODULO_DB_PASSWORD`, `SAQ_AUTH_USERNAME`, `SAQ_AUTH_PASSWORD`) - a database
already initialised on the old `changeme` default keeps that value until you
rotate it.

The compose also constructs one URL the system worker needs:
`MODULO_SYSTEM_DATABASE_URL` (`modulo_system` + `MODULO_DB_PASSWORD` @ the
bundled Postgres). `dispatcher_reconcile` and the other cross-org system
crons fail closed without it, and `dispatcher_reconcile` gates
`/healthz/ready` - so leaving it out gives an API that answers while
readiness never becomes ready.

**Upgrading a database that already ran:** the `modulo_system` role is
created on the *first* boot, when the URL above did not exist yet, so it
carries a random password - and a warm boot (migrations already at head)
does not re-run the role bootstrap to correct it. The symptom is
`password authentication failed for user "modulo_system"` in the
`saq-system` worker log and `dispatcher_reconcile` reporting `failed` in
readiness. Reconcile the role once after upgrading:

```bash
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo \
  python -m modulo.db.bootstrap_role
```

It is idempotent (it re-applies the roles/grants from the URLs in the
container's environment), after which the system crons pass and readiness
reports `dispatcher_reconcile: ok`.

### Kubernetes (production, multi-replica)

A maintained, vendor-neutral Helm chart ships in this repository at
`deploy/helm/modulo/`. It deploys the **Modulo stack** - backend API, SAQ
runner and system workers, frontend, and an optional in-cluster Redis, against
an external PostgreSQL - and includes ResourceQuota, LimitRange, optional HPA,
ingress and namespace templates. `deploy/helm/modulo/values.eks.example.yaml`
is a validated EKS example (RDS, ElastiCache, ALB), and the chart was deployed
end-to-end on a real EKS cluster on 2026-09-23 (FAR-1052); see
`deploy/helm/modulo/README.md` §Validation Status for exactly what was proven
and which follow-ups remain open.

**Stack versus runtime provider.** The chart installs the stack, not the agent
runtime. The Kubernetes *runtime provider*, which runs agent workspaces as
long-lived pods, ships on main (FAR-1051): an admin enables it with
`MODULO_KUBERNETES_ENABLED` and selects it per dispatch with an Environment
Profile whose `provider_type` is `kubernetes`, and agents then run as
long-lived pods in the customer's cluster under that cluster's ServiceAccount
(`MODULO_KUBERNETES_SERVICE_ACCOUNT`). The kind conformance gate is green
(FAR-1053); the scheduled managed-cluster leg has not run yet - its kubeconfig
secrets are not provisioned - so no managed cluster is claimed
(`docs/deployment/k8s-conformance-parity.md` records exactly what each leg
proves). Chart setup and validation status: `deploy/helm/modulo/README.md`.
The reader-facing guide covering both halves - installing the stack and
running agent workspaces as pods - is published at
https://modulo.run/docs/kubernetes.

**Supported paths remain unchanged:** Docker Compose
(`deploy/compose/docker-compose.prod.yml`) is the default server/production
install, and the native install (`scripts/install.sh`) is the workstation
path. Modulo's own managed deployment runs on Fly.io.

---

## Scaling

### Horizontal scaling (multiple backend replicas)

**Redis is mandatory, and since `REDIS_URL` must always be set (the API refuses to boot without it), a shared Redis is required even for a single replica.** As replicas grow, that shared Redis is what coordinates them. Here's why:

| Feature | Single replica (Redis required) | 2+ replicas (shared Redis) | What the shared Redis solves at 2+ replicas |
|---|---|---|---|
| Cron triggers | SAQ system worker cron | SAQ system worker cron | A single shared Redis queue ensures each trigger fires exactly once; without coordination, every replica would fire every trigger and runs would execute twice. |
| Polling triggers | SAQ system worker cron | SAQ system worker cron | Same – duplicate execution is avoided by the shared queue. |
| Task queue | SAQ broker (Redis) | SAQ broker (Redis) | Jobs live in Redis, so a crash or scale-down of a replica never loses the job. |
| Rate limiting | Redis sliding window | Redis sliding window | All replicas share one sliding-window counter in Redis instead of each counting independently. |
| Lock coordination | PG advisory locks | PG advisory locks | These work across replicas via PostgreSQL – no Redis needed for locks. |

**The pattern:** every replica connects to the same Redis. SAQ runs one system worker that owns the triggers and queue, so each run and trigger executes exactly once no matter how many replicas are running.

**The one exception** is PG advisory locks – they coordinate across any number of replicas via PostgreSQL itself, so locking patterns work regardless of replica count.

### Vertical scaling (bigger machine)

Adding CPU/RAM to a single replica still requires Redis (`REDIS_URL` must always be set). The asyncio event loop handles many concurrent requests within one process. Uvicorn worker processes (configurable via `uvicorn --workers`) use multiple CPU cores on a single machine.

### Configuration

```env
# Single replica – no dedicated multi-replica coordination needed.
# REDIS_URL defaults to redis://localhost:6379/0; do not set it empty (startup aborts).
REDIS_URL=redis://localhost:6379/0

# Multiple replicas – Redis required for coordination
REDIS_URL=redis://redis:6379/0
```
