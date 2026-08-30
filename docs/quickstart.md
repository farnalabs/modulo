# Quickstart

Welcome to Modulo. Get a local Modulo stack running in under 10 minutes, then run your first pipeline.

Modulo is a self-hosted agent governance platform for building governed, repeatable AI-assisted software delivery pipelines. You run it on your own infrastructure; there is no hosted SaaS version yet. See [`docs/system-requirements.md`](./system-requirements.md) for supported platforms and minimum resource requirements.

## Prerequisites

| Dependency | Version | Required For |
|---|---|---|
| **Docker Desktop** | 24+ | PostgreSQL 16 + Redis 7 (local dev) |
| **Python** | 3.12+ | Backend runtime |
| **`uv`** | Latest | Python package manager ([install](https://docs.astral.sh/uv/getting-started/installation/)) |
| **Node.js** | 20+ | Frontend development (optional) |

## 1. Start infrastructure

```powershell
# From the repository root
docker compose -f docker-compose.local.yml up -d
```

This starts:
- **PostgreSQL 16** on port `5434`
- **Redis 7** on port `6380`

## 2. Set up the backend

```powershell
cd backend
uv sync

# Create .env (these values work with the local Docker containers)
@"
DATABASE_URL=postgresql+asyncpg://modulo:modulo@localhost:5434/modulo
MODULO_DB=postgres
SECRET_KEY=local-dev-secret-key-not-for-production
FERNET_KEY=<generate-your-own-fernet-key>
# Generate your own key, e.g.: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
REDIS_URL=redis://localhost:6380/0
MODULO_PUBLIC_URL=http://localhost:8000
MODULO_USERS=admin:admin
CORS_ORIGINS=http://localhost:5173
"@ | Out-File -Encoding utf8 .env

# Fix alembic_version table width for branch migration IDs
docker compose -f ../docker-compose.local.yml exec db-local psql -U modulo -c "CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY);"

# Run migrations
uv run alembic upgrade heads
```

## 3. Start the backend

```powershell
uv run uvicorn modulo.api.main:app --reload --port 8000
```

The API is now live at `http://localhost:8000`. OpenAPI docs at `http://localhost:8000/docs`.

## 3b. Start the SAQ workers (required for pipeline execution + cron)

Modulo executes pipeline runs through SAQ workers. Start them in separate
terminals:

```powershell
# Runs worker: executes run jobs (queue: runs)
uv run python -m saq modulo.core.saq_worker.runs_settings

# System worker: scheduler (fire_due_triggers) + reconcile + system crons
$env:SAQ_AUTH_USERNAME = "admin"
$env:SAQ_AUTH_PASSWORD = "admin"
uv run python -m modulo.core.saq_worker
```

Notes:
- `python -m saq` takes the **settings module** as its only positional arg; there is no `worker` subcommand in SAQ 0.26.4.
- A local Redis is required (`REDIS_URL`, e.g. `redis://localhost:6380/0` from the compose `redis-local` service).
- The compose stack (`docker-compose.local.yml`) ships `saq-runner` + `saq-system` services that launch both workers; `saq-system` is **required** for local cron/triggers to fire.

## 4. Start the frontend (optional)

```powershell
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

No API key handy? The codebase ships a deterministic test double, `StubModelBackend` (`backend/src/modulo/model_backends/stub/backend.py`), that returns responses keyed by its input. It is not exposed in the UI's provider dropdown; it is reachable only through the API as the `custom` provider, with a `fixture_map` in `default_params`:

```powershell
curl -X POST http://localhost:8000/api/v1/model-backends `
  -u admin:admin `
  -H "Content-Type: application/json" `
  -d '{"name":"stub","display_name":"Stub","provider":"custom","model_id":"stub","default_params":{"fixture_map":{"hello":"hello from the stub"}}}'
```

(`-u admin:admin` are the credentials configured via `MODULO_USERS` in your `.env`.)

**2. Define schemas**

1. Go to **Schemas** (`/schemas`). The page has three tabs: **Browse**, **Editor**, and **Infer**.
2. Open the **Editor** (`/schemas/editor`) and create an input schema and an output schema for your pipeline stage: give each a name and version, add fields, and click **Save**. Saving validates the JSON Schema and publishes a versioned definition.
3. Already connected a data source? Use the **Infer** tab (`/schemas/infer`) to derive a draft schema from a connected connector: pick the connector and resource type, click **Infer Schema**, then review and save the draft. (API: `POST /api/v1/schemas/infer`.)

**3. Build a pipeline**

1. Go to **Pipelines** (`/pipelines`) and click **New Pipeline**. This takes you to the **Library** (`/library`). Pick a template and click **Create Pipeline**; the wizard creates the pipeline in your workspace.
2. Open it in the editor (`/pipelines/:id/editor`). Click **Add Node** to drop a node onto the canvas, then select it to open the **Node Properties** panel. A node runs as an **Agent** bound to a model backend with input/output schemas; bind it to an agent via the agent picker (agents and connectors are defined through the API: `POST /api/v1/agents`, `POST /api/v1/connectors`).
3. Add a second node the same way, then connect the two by dragging between the nodes' ports. To add a human-in-the-loop gate, select the edge between them and enable the **HITL Gate** section with a label (e.g. "Review before deploy").
4. Click **Save** to persist the graph (`PATCH /api/v1/pipelines/{pipeline_id}/graph`).

**4. Trigger a run**

With the pipeline open in the editor, click **Run Pipeline** (a dialog opens; enter a prompt or leave it blank) and confirm. You can also trigger from the pipelines list via the **Run** button on a pipeline card. Both call `POST /api/v1/runs` with a `pipeline_id` and `input_payload`:

```powershell
curl -X POST http://localhost:8000/api/v1/runs `
  -u admin:admin `
  -H "Content-Type: application/json" `
  -d '{"pipeline_id":"<pipeline-id>","input_payload":{"prompt":"hello"}}'
```

The request returns immediately; the run executes in the background via the SAQ workers, and its status is polled from `GET /api/v1/runs/{run_id}`.

**5. Watch the run**

1. Go to **Runs** (`/runs`) to see run history: filter by status or trigger type, or use the **Stop** button to cancel an in-flight run.
2. Open the run (`/runs/:id`): you'll see the live status, per-node progress chips, and the **Execution Trace** table with per-node input/output (expand the **IO** cell), token counts, cost, logs, and prompt.
3. If your edge has a HITL gate, the run pauses at `awaiting_human` and the page shows a **HITL Gate** section: **Claim Gate** to take it, then **Approve** or **Reject**.
4. Every lifecycle event is written to the immutable audit log. Open **Audit Log** (`/admin/audit`) and filter by the `run.started`, `run.completed`, and `run.failed` event types.

## Next steps

- Read the [Architecture Guide](./architecture.md) to understand the system design
- Check the [Deployment Guide](./deployment.md) for production setup
- Review the [Configuration Reference](./configuration-reference.md) for all available environment variables
- See [System Requirements](./system-requirements.md) for production hardware and platform requirements
- Plan your public launch with the [Launch Checklist](./public-launch-checklist.md)
- Learn the [Upgrade Process](./upgrade-process.md) for existing deployments
- Read [Core Principles](./core-principles.md) for the product's design pillars
