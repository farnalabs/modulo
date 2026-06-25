# Quickstart

Get Modulo up and running with a demo pipeline in under 10 minutes. No external API keys required.

## Prerequisites

- Docker Desktop (PostgreSQL 16 + Redis 7)
- Python 3.12+
- `uv` package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/))

## 1. Start infrastructure

```powershell
# From Development/Product/
docker compose -f docker-compose.local.yml up -d
```

This starts:
- **PostgreSQL 16** on port `5434`
- **Redis 7** on port `6380`

## 2. Set up the backend

```powershell
cd Development/Product/backend
uv sync

# Create .env (these values work with the local Docker containers)
@"
DATABASE_URL=postgresql+asyncpg://modulo:modulo@localhost:5434/modulo
MODULO_DB=postgres
SECRET_KEY=local-dev-secret-key-not-for-production
FERNET_KEY=vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI=
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

## 4. Start the frontend (optional)

```powershell
cd Development/Product/frontend
npm install
npm run dev
```

The UI is now live at `http://localhost:5173`. Log in with `admin:admin`.

## 5. Run the demo pipeline

With `MODULO_DEMO_MODE=true` (set in your `.env`), a pre-built `prd-to-requirements` pipeline is available:

1. Open the dashboard at `http://localhost:5173`
2. Click the **Demo pipeline** card
3. Click **Run** — the pipeline reads a sample PRD and extracts structured requirements
4. View the output in the run inspection panel

No external API keys are needed — the demo uses `StubModelBackend`.

## Next steps

- Read the [Architecture Guide](./architecture.md) to understand the system design
- Check the [Deployment Guide](./deployment.md) for production setup
- Browse the [PRD](./prd.md) for full feature specifications
