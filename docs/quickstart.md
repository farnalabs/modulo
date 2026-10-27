# Quickstart

Welcome to Modulo. Get a local Modulo stack running in under 10 minutes, then run your first pipeline.

Modulo is a self-hosted Agentic Delivery Governance platform for building governed, repeatable AI-assisted software delivery pipelines. You run it on your own infrastructure; there is no hosted SaaS version yet. See [`docs/system-requirements.md`](./system-requirements.md) for supported platforms and minimum resource requirements.

## Prerequisites

| Dependency | Version | Required For |
|---|---|---|
| **Docker Desktop** | 24+ | PostgreSQL 16 + Redis 8 (local dev) |
| **Python** | 3.12+ | Backend runtime |
| **`uv`** | 0.11.13 | Python package manager ([install](https://docs.astral.sh/uv/getting-started/installation/)); this is the exact version the Docker images pin |
| **Node.js** | 20+ | Frontend development (optional). CI pins Node 22; install `pnpm` (the repo pins `pnpm@11.28.8`) rather than npm |

## 1. Start infrastructure

```bash
# From the repository root.

# Generate the Fernet key ONCE. Every process that reads or writes a stored
# credential must hold the SAME key, so the value you export here is reused in
# backend/.env in step 2. A Fernet key is url-safe base64 of exactly 32 bytes —
# a fixed placeholder cannot work, so the compose file refuses to start without
# one rather than half-starting a stack that cannot store credentials.
# (Windows PowerShell: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export FERNET_KEY=$(openssl rand -base64 32)

# The backend .env below logs in with password `modulo`, so start Postgres with
# the same password (the compose default is `changeme`).
POSTGRES_PASSWORD=modulo docker compose -f docker-compose.local.yml up -d
```

This starts:
- **PostgreSQL 16** on port `5434`
- **Redis 8** on port `6380`
- **`saq-runner` + `saq-system`**, the two SAQ workers (they need the schema
  step 2 creates; until then they retry and log connection errors)

If `FERNET_KEY` is unset, `docker compose up` fails immediately with
`required variable FERNET_KEY is missing a value` and the generation command —
that is the intended behaviour, not a bug.

## 2. Set up the backend

```bash
cd backend
uv sync

# Create .env (these values work with the local Docker containers).
# FERNET_KEY is the value exported in step 1 — a different key here cannot
# decrypt the credentials the workers stored.
cat > .env <<EOF
DATABASE_URL=postgresql+asyncpg://modulo:modulo@localhost:5434/modulo
MODULO_DB=postgres
SECRET_KEY=local-dev-secret-key-not-for-production
FERNET_KEY=$FERNET_KEY
REDIS_URL=redis://localhost:6380/0
MODULO_PUBLIC_URL=http://localhost:8000
MODULO_USERS=admin:admin
CORS_ORIGINS=http://localhost:5173
EOF

# Fix alembic_version table width for branch migration IDs
docker compose -f ../docker-compose.local.yml exec db-local psql -U modulo -c "CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY);"

# Run migrations
uv run alembic upgrade heads
```

## 3. Start the backend

```bash
uv run uvicorn modulo.api.main:app --reload --port 8000
```

The API is now live at `http://localhost:8000`. OpenAPI docs at `http://localhost:8000/docs`.

## 3b. Start the SAQ workers (required for pipeline execution + cron)

Modulo executes pipeline runs through SAQ workers. Start them in separate
terminals:

```bash
# Runs worker: executes run jobs (queue: runs)
uv run python -m saq modulo.core.saq_worker.runs_settings

# System worker: scheduler (fire_due_triggers) + reconcile + system crons
SAQ_AUTH_USERNAME=admin SAQ_AUTH_PASSWORD=admin uv run python -m modulo.core.saq_worker
```

Notes:
- `python -m saq` takes the **settings module** as its only positional arg; there is no `worker` subcommand in SAQ 0.26.4.
- A local Redis is required (`REDIS_URL`, e.g. `redis://localhost:6380/0` from the compose `redis-local` service).
- The compose stack (`docker-compose.local.yml`) ships `saq-runner` + `saq-system` services that launch both workers; `saq-system` is **required** for local cron/triggers to fire.

## 3c. Smoke-check the stack

A Modulo stack can answer `GET /healthz` and accept a login while still being
unable to do the two things that matter: storing a credential and checkpointing
a run. Both failures come from a `FERNET_KEY` that is not a real Fernet key, and
neither is visible in `docker compose ps`. Run the smoke check once — it creates
and deletes a throwaway model backend (a real encryption round-trip) and asserts
the checkpointer tables exist:

```bash
# From the repository root (not backend/)
./scripts/smoke-compose.sh --compose-file docker-compose.local.yml --db-service db-local
```

Expected output:

```text
Modulo compose smoke check -> http://localhost:8000
  PASS login as admin (HTTP 200)
  PASS readiness checkpointer ok
  PASS checkpoint tables present (checkpoints, checkpoint_migrations)
  PASS credential stored (model backend created, HTTP 201)
  PASS smoke-check model backend removed
Smoke check passed: login, checkpointer, checkpoint tables, credential round-trip.
```

A failure names its own fix. The two signatures it catches:

| Failed step | What it means |
|---|---|
| `readiness checkpointer 'degraded'` | `startup.checkpointer_init_failed` — the checkpointer could not initialise, so `checkpoints` / `checkpoint_migrations` were never created |
| `creating a model backend returned HTTP 500` | `ValueError: FernetSecretsBackend: invalid fernet_key` — the key only passed the `Settings` length check, not Fernet's shape check |

Both mean no credential has ever been stored successfully, so the fix is to
generate a key for real — `openssl rand -base64 32` — write it to `.env` at the
repo root (and to the same `FERNET_KEY` in the backend's own `.env`, created in
step 2), restart the backend, and recreate the compose services so they pick it
up. On a stack that *has* stored credentials under some other key, reuse that
key instead: a new key cannot decrypt them.

## 4. Start the frontend (optional)

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm run dev
```

The UI is now live at `http://localhost:5173`. Log in with `admin:admin`.

## Run your first pipeline

Everything above gets you a running stack. Now you'll build and run your first real pipeline. Pipeline runs execute through the SAQ workers, so keep the terminals from step 3b running while you work through this section.

> **Heads up on the model backend:** every agent node calls a model backend, and there is no "works with no API keys" option. You need either a real provider API key, or the built-in stub backend (details in step 1).

**1. Create a model backend**

1. Go to **Model Backends** (route `/admin/model-backends`).
2. Click **Add Model Backend** and fill in the form: a name, a display name, a **Provider** (e.g. `openai` or `anthropic`), a **Model ID**, and the provider's **API key**.
3. Click **Create**. The new backend runs a best-effort health check on save and shows a **Configured** badge once its credentials are stored.

No API key handy? The codebase ships a deterministic test double, `StubModelBackend` (`backend/src/modulo/model_backends/stub/backend.py`), that returns responses keyed by its input. It is not exposed in the UI's provider dropdown; it is reachable only through the API as the `custom` provider, with a `fixture_map` in `default_params`. The API authenticates with Bearer tokens, so log in first (the seeded `admin` account from `MODULO_USERS` in your `.env`):

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin","password":"admin"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -X POST http://localhost:8000/api/v1/model-backends \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"stub","display_name":"Stub","provider":"custom","model_id":"stub","api_key":"not-needed-for-stub","default_params":{"fixture_map":{"hello":"hello from the stub"}}}'
```

(`api_key` is a required field on the endpoint; the stub ignores credentials, so any placeholder works.)

**2. Define schemas**

1. Go to **Schemas** (`/schemas`). The page has three tabs: **Browse**, **Editor**, and **Infer**.
2. Open the **Editor** (`/schemas/editor`) and create an input schema and an output schema for your pipeline stage: give each a name and version, add fields, and click **Save**. Saving validates the JSON Schema and publishes a versioned definition.
3. Already connected a data source? Use the **Infer** tab (`/schemas/infer`) to derive a draft schema from a connected connector: pick the connector and resource type, click **Infer Schema**, then review and save the draft. (API: `POST /api/v1/schemas/infer`.)

**3. Build a pipeline**

1. Go to **Pipelines** (`/pipelines`) and click **New Pipeline**. This takes you to the **Library** (`/library`). Pick a template and click **Create Pipeline**; the wizard creates the pipeline in your workspace.
2. Open it in the editor (`/pipelines/:id/editor`). Click **Add Node** to drop a node onto the canvas, then select it to open the **Node Properties** panel. A node runs as an **Agent** bound to a model backend with input/output schemas; bind it to an agent via the agent picker (agents and connectors are defined through the API: `POST /api/v1/agents`, `POST /api/v1/connectors`).
3. Add a second node the same way, then connect the two by dragging between the nodes' ports. To add a human-in-the-loop gate, select the edge between them and enable the **HITL Gate** section with a label (e.g. "Review before deploy").
4. Click **Save** to persist the graph (`PUT /api/v1/pipelines/{pipeline_id}/graph`).

**4. Trigger a run**

With the pipeline open in the editor, click **Run Pipeline** (a dialog opens; enter a prompt or leave it blank) and confirm. You can also trigger from the pipelines list via the **Run** button on a pipeline card. Both call `POST /api/v1/runs` with a `pipeline_id` and `input_payload` (same login as step 1, then Bearer auth):

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin","password":"admin"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -X POST http://localhost:8000/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pipeline_id":"<pipeline-id>","input_payload":{"prompt":"hello"}}'
```

The request returns immediately; the run executes in the background via the SAQ workers, and its status is polled from `GET /api/v1/runs/{run_id}`.

**5. Watch the run**

1. Go to **Runs** (`/runs`) to see run history: filter by status or trigger type, or use the **Stop** button to cancel an in-flight run.
2. Open the run (`/runs/:id`): you'll see the live status, per-node progress chips, and the **Execution Trace** table with per-node input/output (expand the **IO** cell), token counts, cost, logs, and prompt.
3. If your edge has a HITL gate, the run pauses at `awaiting_human` and the page shows a **HITL Gate** section: **Claim Gate** to take it, then **Approve** or **Reject**.
4. Every lifecycle event is written to the immutable audit log. Open **Audit Log** (`/admin/audit`) and filter by the `run.started`, `run.completed`, and `run.failed` event types. The events are recorded on every tier; opening the Audit Log itself needs the Team-tier `audit_viewer` feature plus the `audit.manage` permission.

## Next steps

- Read the [Architecture Guide](./architecture.md) to understand the system design
- Check the [Deployment Guide](./deployment.md) for production setup
- Review the [Configuration Reference](./configuration-reference.md) for all available environment variables
- See [System Requirements](./system-requirements.md) for production hardware and platform requirements
- Plan your public launch with the [Launch Checklist](./public-launch-checklist.md)
- Learn the [Upgrade Process](./upgrade-process.md) for existing deployments
- Read [Core Principles](./core-principles.md) for the product's design pillars
