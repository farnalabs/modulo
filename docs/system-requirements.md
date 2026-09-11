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

## Native Single-Install Bundle (Linux)

The native installer (`scripts/install.sh`) distributes a self-contained
bundle: the backend, a bundled CPython runtime, PostgreSQL 16, Redis 8, and
the built SPA, laid out under `~/.local/opt/modulo/`. The table below is
the honest v1 envelope (ADR 031; recentre on what `bundle-v*` artifacts
actually ship - do not widen these rows until the packaging tickets land):

| OS | glibc floor | Status |
|----|-------------|--------|
| Debian 13+ | 2.41 | Supported |
| Ubuntu 24.04+ | 2.39+ | Supported |
| Ubuntu 20.04-22.04 | 2.31-2.35 | Not supported (glibc below the bundled floor) |
| macOS | - | Planned (P2) |
| Windows | - | Planned (P3) |

### Supported architectures

| Architecture | Status |
|--------------|--------|
| linux-amd64 (x86_64) | Supported |
| linux-arm64 | Planned (P2) |
| macOS / Windows | Planned (P2/P3) |

### Disk and RAM envelope

The native bundle is roughly the same footprint as Compose, minus Docker
itself: ~2 GB for the install roots, and the bundled Postgres data dir
grows with run history.

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| RAM | 4 GB | 8 GB |
| Disk (install + data dir) | 10 GB | 20 GB |
| Swap | Required (Postgres needs commit headroom) | - |

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
| **MySQL** | 8+ | Supported | Conformance | Via `MODULO_DB=mysql` (`aiomysql` driver) |
| **MariaDB** | 11+ | Supported | Conformance | Via `MODULO_DB=mariadb` |
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
| Redis | 8+ | **Yes** (production) | SAQ task queue, rate limiting, event broker |
| Python | 3.12+ | Yes | Application runtime |
| `uv` | 0.11.x | Yes | Python package manager (pinned in Docker images) |
| Node.js | 20+ | For frontend dev | Frontend build toolchain |
| Docker | 24+ | For Docker Compose | Container runtime |

### Redis Requirement Table

| Deployment Type | Redis Required? | Reason |
|----------------|-----------------|--------|
| Single replica, single process | **Yes** | The dispatcher enqueues every run to SAQ's Redis queue (`core/dispatch.py`) and cron/polling triggers fire via Redis-backed SAQ system crons (`fire_due_triggers`, `fire_cron_trigger`, `fire_polling_trigger` in `core/saq_worker.py`); `api/main.py` refuses to boot without `REDIS_URL`. This reconciles with the "Redis \| Yes (production)" prerequisite row above and [`docs/quickstart.md`](./quickstart.md) §3b. |
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
| Configuration precedence (native installs) | [`docs/configuration-precedence.md`](./configuration-precedence.md) |
| Public launch checklist | [`docs/public-launch-checklist.md`](./public-launch-checklist.md) |
| Upgrade process | [`docs/upgrade-process.md`](./upgrade-process.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) |
| Network egress | [`docs/operations/network-egress.md`](./operations/network-egress.md) |
