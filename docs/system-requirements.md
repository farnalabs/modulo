# System Requirements

Supported platforms, minimum resources, and database backends for running Modulo in production. Docker Compose on Linux is the production server path. Native installers for Linux, macOS and Windows are all in the Alpha release scope.

---

## Supported Platforms

Linux is the production server host. Native macOS and Windows installers are
also in the Alpha scope, not deferred beyond the release: macOS is FAR-678 and
Windows is FAR-679, with native validation tracked in FAR-2196 (macOS) and
FAR-2197 (Windows). Per-platform delivery status changes faster than this page,
so ADR 031 defers it to those tickets rather than restating it here.

### Platform support matrix

This matrix is about the **server host** for a production or evaluation
deployment, not about developer workstations (see Development / Evaluation
below).

| Server host platform | Status |
|----------------------|--------|
| **Linux** | Supported (Docker Compose, and the Linux-first native single-install) |
| **macOS** | In Alpha scope for a native install (FAR-678, validation FAR-2196). No macOS artifact is published yet and the installer workflow is fail-closed, so it is not a usable server host today. |
| **Windows** | In Alpha scope for a native install (FAR-679, validation FAR-2197). No Windows installer is published yet and the installer workflow is fail-closed, so it is not a usable server host today. |

Docker Desktop (see Development / Evaluation below) is a developer
convenience on macOS and Windows: it runs the Linux containers inside a Linux
VM, so the deployed stack is still Linux. It is not a supported production
host.

**Native installer status today:**

- `scripts/install.sh` is the Linux installer and fails closed everywhere else.
  On Darwin it aborts with "macOS native artifact is not yet published"; on any
  other host it reports that it supports Linux only.
- The release pipeline builds one native bundle, `linux-amd64`
  (`bundle-release.yml`). No macOS or Windows bundle is published yet; FAR-678
  and FAR-679 carry those.
- No `bundle-v*` release is published, so the native install path fails closed
  on every platform for now. ADR 031 Decision 12 records that state and the
  tickets that change it.
- The macOS and Windows installer workflows exist and are wired into
  `release-unified.yml`, but both are fail-closed: they validate the packaging
  contract and refuse to publish a non-dry-run installer until a real native
  runtime exists.

A Windows-hosted production deployment therefore means Windows Server running
the Linux stack in a VM or container host, not native Windows binaries. Docker
Compose remains the production server path on every platform.

### Install paths

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

| OS | glibc | Status |
|----|-------|--------|
| Debian 13+ | 2.41 | Supported |
| Ubuntu 26.04+ | 2.41+ | Supported |
| Ubuntu 24.04 and earlier | 2.39 and below | Not supported (below the bundled service-binary floor) |
| macOS | - | No published bundle yet (Alpha scope: FAR-678, validation FAR-2196) |
| Windows | - | No published bundle yet (Alpha scope: FAR-679, validation FAR-2197) |

The hard requirement is **glibc 2.41 or newer**. The bundled PostgreSQL 16 and
Redis 8 binaries are extracted from Debian 13 (trixie) packages, and the venv's
own 2.39 runner floor is lower, so the service binaries set the effective floor
(`scripts/bundle-versions.env`: "the effective bundle floor is glibc 2.41
(Debian 13+, Ubuntu 26.04+)"; `bundle-release.yml` states the same envelope).
An Ubuntu 24.04 host (glibc 2.39) is therefore below the floor and is not a
supported native-install target.

### Supported architectures

| Architecture | Status |
|--------------|--------|
| linux-amd64 (x86_64) | Supported target; the only architecture `bundle-release.yml` builds (no `bundle-v*` release published yet) |
| linux-arm64 | Planned (pins exist; no artifact published yet) |
| macOS arm64 / x64 | Alpha scope, no published artifact (FAR-678, validation FAR-2196) |
| Windows amd64 | Alpha scope, no published artifact (FAR-679, validation FAR-2197) |

`linux-arm64` is not installable yet: the bundle build matrix carries only
`amd64`, so no `linux-arm64` tarball is published even though the arm64 pins
already exist in `scripts/bundle-versions.env` and the matrix is structured to
accept them. `scripts/install.sh` preflight accepts `aarch64`/`arm64` for the
architecture case, but there is no `linux-arm64` bundle to download.

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
| **SQLite** | 3.x | Compatible | **No** | Dev-only: no RLS, no advisory locks |
| **MySQL** | 8+ | Partial | **No** | `MODULO_DB=mysql` is accepted (`aiomysql` driver) but the per-node output store is postgres/sqlite-only - see below |
| **MariaDB** | 11+ | Deprecated | **No** | `MODULO_DB=mariadb` accepted; deprecated since 2026-07-11, and blocked on the same write path as MySQL |

MySQL and MariaDB are configured through `MODULO_DB` (`settings.py` accepts
`postgres`, `sqlite`, `mariadb`, `mysql`) and `aiomysql` is a declared
dependency, but they are **not** production-conformant:

* `db/crud/run_node_outputs.py::dialect_insert` raises
  `NotImplementedError` for any dialect other than `postgres` / `sqlite`. That
  module is the chokepoint for every `run_node_outputs` read and write (FAR-583),
  so a run cannot store its node outputs, telemetry, markers, or artifacts on
  either backend.
* The repository hub dispatches only `postgres` to `PostgresRepository`; any
  other backend falls back to `GenericRepository` with a warning, so tenant
  isolation relies on injected ORM filters instead of database RLS.
* MariaDB is additionally called out as deprecated and untested in the code
  itself, and has no `ON CONFLICT` support.

Treat the two MySQL-family rows as configuration surface only.

### PostgreSQL Requirements

- **Version**: 16 or later
- **Extensions**: none required; `gen_random_uuid()` is built into PostgreSQL 16+ (core since PG 13)
- **Connection**: Async via `asyncpg` driver
- **TLS**: `sslmode=require` (or stronger) recommended for production; the
  `sslmode` in the database URL is honoured. Accepted values: `disable`,
  `require`, `verify-ca`, `verify-full`; `prefer`/`allow` are rejected at
  startup (they silently downgrade to plaintext). No `sslmode` = plaintext.
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
| `uv` | 0.11.13 | Yes | Python package manager (exact pin in `backend/Dockerfile` and `deploy/docker/Dockerfile.all-in-one`) |
| Node.js | 20+ | For frontend dev | Frontend build toolchain; CI pins Node 22. Use `pnpm` (the repo pins `pnpm@11.28.8` via `packageManager`), not npm |
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
| Chromium 120+ | Yes | The only browser the E2E suite installs (`playwright install chromium` in CI and in the deploy workflow) |

Firefox, Safari, and Edge are **not** covered by the automated E2E matrix; the
Playwright config (`frontend/playwright.config.ts`) declares no `projects` block,
so a single default Chromium browser runs. Treat the other three as untested
here.

---

## Modulo Website Version Display (Scope-Out Decision)

**Decision recorded per FAR-1872 (d):** A grep of `modulo-website/src/**` found ZERO version strings and ZERO ghcr tags.

**Explicit scope-out:** The marketing website (modulo.run, served from `modulo-website` repo) does not display version badges, changelog mirrors, or a downloads page in this release. This is a deliberate deferral: the website surfaces product capabilities and documentation; version-specific download/install links live in the product docs (`docs/deployment.md`, `docs/upgrade-process.md`, `docs/release-process.md`) and the GHCR package page.

If a version badge, changelog mirror, or downloads page is desired for the website, it should be tracked as a separate feature ticket in the `modulo-website` repo.

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
