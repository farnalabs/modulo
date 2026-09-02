# Deployment Journeys – Three Paths to Running Modulo

Modulo supports three distinct deployment scenarios. Every user starts on one
path and can graduate to the next as their needs grow – all three use the same
codebase, same Docker images, same RuntimeProvider ABC.

---

## 1. Entirely Local – Dev & Solo Evaluation

**Who it's for:** solo developers, evaluation, local testing before staging.

**Goal:** minimal friction, no cloud services, works offline.

### Options

| Approach | What you need | How to run | RuntimeProvider |
|---|---|---|---|
| Docker Compose (recommended) | Docker Desktop | `docker compose -f docker-compose.local.yml up -d` then `uv run uvicorn ...` | `LocalRuntimeProvider` (default, max 2 concurrent agents) |
| SQLite mode | Python 3.12, `uv` | Set `MODULO_DB=sqlite`, run `uv run uvicorn modulo.api.main:app` | `LocalRuntimeProvider` |
| Full stack with Docker | Docker Compose | `docker compose -f docker-compose.local.yml --profile observability up -d` | `LocalRuntimeProvider` |

For a full walkthrough, see `docs/quickstart.md` and `AGENTS.md` §Local Development Setup.

### Limits
- No sandbox isolation (agents run as subprocesses on your machine)
- Concurrency capped at `MODULO_MAX_LOCAL_CONCURRENCY` (default 2)
- No horizontal scaling (single process)
- SQLite mode: no RLS, no advisory locks, no flood protection

### When to outgrow this
You want to share the instance with a team, deploy it somewhere always-on,
or run more than 2 agents in parallel.

---

## 2. New Cloud Deployment – No Existing Infrastructure

**Who it's for:** solo devs or small teams who want a production-like instance
without managing servers or databases.

**Goal:** one service, minimal setup, gets you live in minutes.

### The pattern

```
┌─────────────────────────────────────────────┐
│  Fly.io / Railway                            │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐ │
│  │ Postgres  │  │  Redis   │  │ Modulo App │ │
│  │ (managed) │  │(managed) │  │(Docker)    │ │
│  └──────────┘  └──────────┘  └─────┬──────┘ │
│                                     │        │
│                LocalRuntimeProvider │(in-proc)│
│                max_concurrency = 2  │        │
└─────────────────────────────────────┴────────┘
```

### Option A: Fly.io (recommended)

```bash
# One-time
fly launch --image ghcr.io/farnalabs/modulo
fly postgres create --name modulo-db
fly redis create --name modulo-redis
fly secrets set \
  SECRET_KEY="$(openssl rand -base64 32)" \
  FERNET_KEY="$(openssl rand -base64 32)" \
  DATABASE_URL="postgresql+asyncpg://..." \
  REDIS_URL="redis://..." \
  MODULO_PUBLIC_URL="https://app.modulo.run" \
  MODULO_USERS="admin:your-password"
fly deploy
```

The production `fly.toml` is at the repository root; `deploy/fly/` holds `fly.staging.toml` plus the bootstrap and entrypoint scripts.

### Option B: Railway

Same pattern – Docker image + managed Postgres + Redis via Railway's dashboard.
Set the same env vars in the Railway dashboard.

### How agents run

By default, agents run **in-process** via `LocalRuntimeProvider`. The app
container runs them as subprocesses, capped at 2 concurrent. No sandbox,
no isolation, no scaling – but it's free and works immediately.

**To add sandboxed agents:** set `MODULO_E2B_API_KEY` as an env var and restart.
The `RuntimeProviderHub` auto-detects the key and makes E2B available for
profiles that request it. Existing pipelines continue to work unchanged –
the ABC hides the backend.

### Limits

| Resource | Free tier | Paid tier |
|---|---|---|
| Postgres | 1 GB (Fly downsize) | Scale as needed |
| Redis | 30 MB (Upstash free) | Scale as needed |
| Concurrency | 2 in-process agents | 2 in-process + N E2B sandboxes |
| Uptime | Always-on | Always-on |

### When to outgrow this
You need more than 2 concurrent agents, sandbox isolation for security,
or want to deploy on your own infrastructure (Docker Compose / VM).

---

## 3. Existing Infrastructure – Integrate With What You Have

**Who it's for:** teams that run their own infrastructure (Docker Compose,
Podman, VMs), have a VPC, use AWS/GCP, have compliance requirements (SOC 2,
data residency, air-gapped).

**Goal:** deploy Modulo into existing infra with maximum control.

### The pattern

```
┌──────────┐  ┌──────────┐  ┌──────────────────┐
│Postgres  │  │  Redis   │  │  Modulo Backend  │
│(existing)│  │(existing)│  │  (Docker/Podman) │
└──────────┘  └──────────┘  └────────┬─────────┘
                                     │
                                     ▼
                        ┌──────────────────────┐
                        │    E2B Runtime       │
                        │    (sandboxed)       │
                        └──────────────────────┘
```

### Deployment options

| Approach | Docs | RuntimeProvider |
|---|---|---|
| Docker Compose (prod) | `docs/deployment.md` | Local or E2B |
| Raw Docker | `docker compose up` with prod config | Local or E2B |

### RuntimeProvider options for this tier

| Provider | When to use | Concurrency | Isolation |
|---|---|---|---|
| `LocalRuntimeProvider` | Default; single-host, no sandbox | Capped (default 2) | None |
| `E2BRuntimeProvider` | Add sandboxed execution with E2B | Unlimited (E2B's capacity) | Full sandbox |
| `DockerRuntimeProvider` | Run each agent in a separate container | Host capacity | Container-level |

All providers share the same `RuntimeProvider` ABC. Switching between them
is a config change, not a code change.

### Team features already available

These are not future plans – they are built and tested today:

- **SSO**: OIDC (Google, GitHub, Okta, any IdP) + SAML 2.0
- **Team RBAC**: team-scoped resources, operator/runner/viewer roles,
  privilege cap at org role
- **Audit**: append-only audit log with cryptographic chaining,
  auditor viewer UI, JSONL/CSV export
- **Spend limits**: per-org and per-team run and token budgets
- **License enforcement**: Ed25519-signed license keys with feature gates
- **Checkpoint encryption**: Fernet-encrypted agent state at rest
- **Tenant isolation**: Row-Level Security on Postgres, organisation_id
  on all tables including LangGraph checkpoints
- **Observability**: OpenTelemetry-native, pre-built Grafana dashboards
- **Backup/DR**: `modulo backup` / `modulo restore` CLI, full DR procedure
- **Secrets backends**: Fernet (default), Vault, AWS Secrets Manager

### When you need modulo-cloud (V3 – not yet built)

A hosted SaaS wrapping Modulo core, adding multi-org billing, subdomain
routing, and a public community library registry. Only needed if/when
you want to offer Modulo as a service to external teams.

---

## Journey Map

```
                        ┌──────────────────────┐
                        │  Local Development    │
                        │  (Docker Compose /    │
                        │   SQLite)             │
                        │  ~2 concurrent agents │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  Cloud – New Infra    │
                        │  (Fly.io / Railway)   │
                        │  ~2 in-process agents │
                        │  + opt-in E2B for     │
                        │  sandboxed scaling    │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  Existing Infra       │
                        │  (Docker Compose/VPC) │
                        │  Any RuntimeProvider  │
                        │  Full team           │
                        └──────────────────────┘
```

Every user starts at the top. Most will be well served by the middle tier
(Fly.io + optional E2B). Only regulated enterprises with existing infra
need the bottom tier. The code and config are identical across all three.

### Supporting resources

- [System Requirements](./system-requirements.md) – minimum resources, supported databases
- [Configuration Reference](./configuration-reference.md) – all environment variables
- [Upgrade Process](./upgrade-process.md) – upgrading existing deployments
- [Public Launch Checklist](./public-launch-checklist.md) – production readiness verification
