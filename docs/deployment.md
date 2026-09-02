# Deployment Guide

## Prerequisites

- Python 3.12+
- PostgreSQL 16+
- Redis 7+ (for SAQ task queue, multi-replica coordination)

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

## Configuration

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
| `MODULO_USERS` | Alpha | – | Comma-separated `user:pass` pairs for initial user seed |
| `MODULO_DB` | No | `postgres` | Database backend (`postgres`, `sqlite`, `mariadb`, or `mysql`) |
| `REDIS_URL` | No | `redis://localhost:6379/0` | Redis URL for SAQ broker, event coordination, rate limiting |
| `MODULO_PUBLIC_URL` | For SSO | `http://localhost:8000` | Public-facing URL for OAuth redirects |
| `CORS_ORIGINS` | No | `http://localhost:5173` | Comma-separated allowed CORS origins |
| `CORS_MAX_AGE` | No | `600` | Preflight cache max-age in seconds |
| `MODULO_SECRETS_BACKEND` | No | `fernet` | Secrets backend: `fernet`, `vault`, or `aws` |
| `MODULO_OIDC_PROVIDERS` | For SSO | – | JSON array of OIDC provider configs |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | – | OTel gRPC/HTTP exporter endpoint |
| `MODULO_E2B_API_KEY` | For E2B | – | E2B sandbox API key for runtime provider |
| `MODULO_ADMIN_SECRET` | No | – | Shared secret for `modulo-migrate` CLI auth |
| `OLLAMA_BASE_URL` | For Ollama | `http://localhost:11434` | Ollama server URL |

---

## Deployment Modes

Modulo supports three deployment modes depending on your needs.

### Standalone (single-user, local)

Three environment variables are **required** and have no default — the app
refuses to start without them:

| Variable | Purpose | How to generate |
|----------|---------|-----------------|
| `DATABASE_URL` | SQLAlchemy async DB URL for the application database | `sqlite+aiosqlite:///./modulo.db` for the local SQLite file below |
| `SECRET_KEY` | 32+ byte random string used to sign JWTs | `$(openssl rand -base64 48)` |
| `FERNET_KEY` | 44-char base64 Fernet key used to encrypt stored connector credentials | `$(openssl rand -base64 32)` (base64-encoded 32-byte key) |

The command below sets all three inline, so it is runnable as written. `MODULO_ADMIN_PASSWORD`
seeds the initial admin user (optional but recommended for first login); `MODULO_DB=sqlite`
selects the SQLite backend so no separate database server is needed.

```bash
git clone https://github.com/farnalabs/modulo   # or install the farnalabs-modulo package
cd backend
uv sync
DATABASE_URL=sqlite+aiosqlite:///./modulo.db \
  SECRET_KEY=$(openssl rand -base64 48) \
  FERNET_KEY=$(openssl rand -base64 32) \
  MODULO_ADMIN_PASSWORD=changeme \
  MODULO_DB=sqlite \
  uv run uvicorn modulo.api.main:app --port 8000
```

| Component | How it runs |
|---|---|
| Database | SQLite file (`./modulo.db`), no server process needed |
| Task scheduling | In-process asyncio loops – cron and polling triggers work |
| Task queue | In-process, no durability across crashes |
| Rate limiting | No-op, disabled when Redis is unavailable (all requests allowed) |
| Concurrency | Single process, single worker |

**What you lose vs. full deployment:**
- **No horizontal scaling** – one process, one user at a time
- **No task durability** – if the process crashes mid-run, the run is lost (re-run manually)
- **No distributed rate limiting** – without Redis the limiter is a per-process no-op, so limits don't coordinate across processes

**What you keep:**
- Cron-triggered pipelines ✓
- Polling triggers ✓
- All pipeline features, evals, HITL, connectors ✓
- The SQLite DB file is portable – copy it to another machine and restart `uvicorn` from the new location to pick it up

### Docker Compose (single-server, multi-user)

```
curl https://modulo.run/install.sh | bash
# or: docker compose -f deploy/compose/docker-compose.prod.yml up
```

| Component | How it runs |
|---|---|
| Database | PostgreSQL 16 (separate container) |
| Task scheduling | In-process asyncio loops (default) or SAQ system worker cron (with Redis) |
| Task queue | In-process (default) or SAQ workers (with Redis) |
| Rate limiting | In-memory no-op (default) or Redis sliding window (with Redis) |
| Concurrency | Single backend replica, multiple simultaneous requests |

If Redis is configured (`REDIS_URL` set), the app automatically upgrades scheduling, queuing, and rate limiting to use SAQ + Redis. `REDIS_URL` defaults to `redis://localhost:6379/0`; if it is explicitly set to an empty value, startup aborts with a `RuntimeError` (see `api/main.py`) instead of a silent fallback.

### Kubernetes (production, multi-replica)

The Kubernetes/Helm example deployment configs were removed – they were never
exercised by CI or used in production. Modulo's only managed deployment path is
Fly.io. For self-hosting, use the Docker Compose configuration
(`deploy/compose/docker-compose.prod.yml`). Kubernetes/Helm support can be re-added later as a
properly maintained example config.

---

## Scaling

### Horizontal scaling (multiple backend replicas)

For more than one backend replica, **Redis is mandatory.** Here's why:

| Feature | Without Redis | With Redis | What goes wrong at 2+ replicas |
|---|---|---|---|
| Cron triggers | In-process asyncio loop | SAQ system worker cron | Both replicas fire every trigger. Runs execute twice. |
| Polling triggers | In-process asyncio loop | SAQ system worker cron | Same – duplicate execution. |
| Task queue | In-process | SAQ broker (Redis) | Jobs are scheduled in the replica that received the request. If that replica crashes or is scaled down, the job disappears. |
| Rate limiting | In-memory no-op | Redis sliding window | Without Redis the limiter is disabled (no-op); with Redis, all replicas share one sliding-window counter in Redis |
| Lock coordination | PG advisory locks | PG advisory locks | These work across replicas via PostgreSQL – no Redis needed for locks. |

**The pattern:** without Redis, each replica independently runs its own scheduler and rate limiter. They don't coordinate. This is fine for a single replica. For two or more, the system behaves incorrectly.

**The one exception** is PG advisory locks – they coordinate across any number of replicas via PostgreSQL itself, so locking patterns work without Redis regardless of replica count.

### Vertical scaling (bigger machine)

Adding CPU/RAM to a single replica works without Redis. The asyncio event loop handles many concurrent requests within one process. Uvicorn worker processes (configurable via `uvicorn --workers`) use multiple CPU cores on a single machine.

### Configuration

```env
# Single replica – no dedicated multi-replica coordination needed.
# REDIS_URL defaults to redis://localhost:6379/0; do not set it empty (startup aborts).
REDIS_URL=redis://localhost:6379/0

# Multiple replicas – Redis required for coordination
REDIS_URL=redis://redis:6379/0
```
