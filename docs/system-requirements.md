# System Requirements

Supported platforms, minimum resources, and database backends for running Modulo in production.

---

## Supported Platforms

| Platform | Status | Documentation |
|----------|--------|---------------|
| **Docker Compose** | Production-ready | [`docs/deployment.md`](./deployment.md) |
| **Self-hosted (bare metal / VM)** | Production-ready | [`docs/operations/self-hosted-admin.md`](./operations/self-hosted-admin.md) |
| **Fly.io** | Production-ready | [`docs/deployment-journey.md`](./deployment-journey.md) |
| **Railway** | Production-ready | [`docs/deployment-journey.md`](./deployment-journey.md) |
| **SQLite (standalone)** | Development only | [`docs/quickstart.md`](./quickstart.md) |

---

## Minimum Resources

### Development / Evaluation

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Disk | 10 GB SSD | 20 GB SSD |
| Docker | Docker Desktop 24+ | Docker Desktop 24+ |
| Python | 3.12+ | 3.12+ |

### Single-Server Production (Docker Compose)

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| CPU | 4 vCPU | 8 vCPU | LLM calls are I/O-bound, not CPU-bound |
| RAM | 8 GB | 16 GB | More RAM for larger pipeline states |
| Disk | 20 GB SSD | 50 GB SSD | Database grows with run history |
| Network | 100 Mbps | 1 Gbps | Webhook delivery, connector calls |

---

## Supported Databases

| Database | Version | Status | Production Ready | Notes |
|----------|---------|--------|-----------------|-------|
| **PostgreSQL** | 16+ | **Supported** | **Yes** | Primary production database |
| **SQLite** | 3.x | Compatible | **No** | Dev-only: no RLS, no advisory locks |

### PostgreSQL Requirements

- **Version**: 16 or later
- **Extensions**: none required; `gen_random_uuid()` is built into PostgreSQL 16+ (core since PG 13)
- **Connection**: Async via `asyncpg` driver
- **TLS**: `sslmode=require` recommended for production
- **Schema**: Alembic-managed migrations run on startup

### SQLite Limitations (Dev Only)

SQLite mode skips these PostgreSQL-specific features:

- Row-Level Security (RLS)
- Advisory locks (`pg_try_advisory_lock`)
- `SELECT FOR UPDATE SKIP LOCKED` (flood protection)
- Distributed rate limiting

A startup warning (structured log key `startup.sqlite_mode`) is logged when running in SQLite mode.

See [`docs/troubleshooting.md`](./troubleshooting.md) §8 for known limitations.

---

## Required Services

| Service | Version | Required | Purpose |
|---------|---------|----------|---------|
| PostgreSQL | 16+ | **Yes** (production) | Primary data store |
| Redis | 7+ | **Yes** (production) | SAQ task queue, rate limiting, event broker |
| Python | 3.12+ | Yes | Application runtime |
| `uv` | Latest | Yes | Python package manager |
| Node.js | 20+ | For frontend dev | Frontend build toolchain |
| Docker | 24+ | For Docker Compose | Container runtime |

### Redis Requirement Table

| Deployment Type | Redis Required? | Reason |
|----------------|-----------------|--------|
| Single replica, single process | **Yes** | Required for SAQ execution, event coordination, rate limiting, caching, and session state (defaults to `redis://localhost:6379/0`) |
| Multiple replicas | **Yes** | SAQ worker coordination, distributed rate limiting |
| Horizontal scaling | **Yes** | Cross-replica event broker, cron triggers |
| Production with 2+ backend pods | **Yes** | See [`docs/deployment.md`](./deployment.md) §Scaling |

---

## Network Requirements

### Outbound (optional, per configuration)

| Destination | Port | Protocol | Purpose |
|-------------|------|----------|---------|
| LLM API endpoints | 443 | HTTPS | Model backend calls (Anthropic, OpenAI, etc.) |
| Connector API endpoints | 443 | HTTPS | GitHub, GitLab, Linear, etc. |
| OIDC/SAML provider | 443 | HTTPS | SSO authentication |
| OTel collector | 4317 | gRPC | Telemetry export (when enabled) |
| E2B API | 443 | HTTPS | Sandboxed agent runtime |

### Inbound

| Port | Protocol | Purpose |
|------|----------|---------|
| 443 | HTTPS | API + Web UI (via reverse proxy) |
| 80 | HTTP | Redirect to HTTPS |

With default settings and no connectors configured, Modulo makes **zero external network calls**. See [`docs/operations/network-egress.md`](./operations/network-egress.md) for the full egress audit.

---

## Browser Support

| Browser | Supported | Notes |
|---------|-----------|-------|
| Chrome 120+ | Yes | Primary development target |
| Firefox 120+ | Yes | Tested |
| Safari 17+ | Yes | Tested |
| Edge 120+ | Yes | Chromium-based |

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| Quickstart | [`docs/quickstart.md`](./quickstart.md) |
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment journeys | [`docs/deployment-journey.md`](./deployment-journey.md) |
| Configuration reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Public launch checklist | [`docs/public-launch-checklist.md`](./public-launch-checklist.md) |
| Upgrade process | [`docs/upgrade-process.md`](./upgrade-process.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) |
| Network egress | [`docs/operations/network-egress.md`](./operations/network-egress.md) |
