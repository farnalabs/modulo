# Contributing to Modulo

- [Welcome](#welcome)
- [Development Setup](#development-setup)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [CI/CD](#cicd)
- [Pull Request Process](#pull-request-process)
- [Release Process](#release-process)
- [Security](#security)

---

## Welcome

Modulo is a governed orchestration layer for agentic SDLC pipelines. It
provides a composable pipeline of atomic AI agents that automate work between
existing tools like GitHub, Linear, and Notion.

We're glad you're here. By participating in this project, you agree to abide
by our [Code of Conduct](CODE_OF_CONDUCT.md) — be respectful, constructive,
and assume good faith.

---

## Development Setup

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | >= 3.12 | Managed via `uv` |
| Node.js | >= 20 | Frontend tooling |
| Docker | Latest | Postgres, Redis, MariaDB |
| uv | Latest | `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` |

### Required services

The project needs a running PostgreSQL 16 and Redis 7 instance. Use Docker
Comose to start them:

```powershell
docker compose up -d db redis
```

This starts PostgreSQL on `localhost:5432` and Redis on `localhost:6379`
with the following defaults:

| Variable | Default |
|---|---|
| `POSTGRES_USER` | `modulo` |
| `POSTGRES_PASSWORD` | `modulo` |
| `POSTGRES_DB` | `modulo` |
| `DATABASE_URL` | `postgresql+asyncpg://modulo:modulo@localhost:5432/modulo` |
| `REDIS_URL` | `redis://localhost:6379/0` |

To use MariaDB instead of PostgreSQL, apply the override:

```powershell
docker compose -f docker-compose.yml -f docker-compose.mariadb.yml up -d
```

The backend auto-detects MariaDB and configures the connection string
(`mysql+asyncmy://modulo:modulo@localhost:3306/modulo`).

---

## Quick Start

```powershell
# 1. Clone the repository
git clone https://github.com/farnalabs/modulo.git Modulo\Development\Product
cd Modulo\Development\Product

# 2. Start infrastructure
docker compose up -d db redis

# 3. Install backend dependencies
cd backend
uv sync
cd ..

# 4. Run database migrations
cd backend
uv run alembic upgrade heads
cd ..

# 5. Install frontend dependencies
cd frontend
npm install
cd ..

# 6. Start the backend (uvicorn with hot-reload)
cd backend
$env:SECRET_KEY = "dev-secret-key-32-bytes-at-least-here!"
$env:FERNET_KEY = "dev-fernet-key-32-bytes-at-least-here!"
$env:DATABASE_URL = "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo"
$env:MODULO_USERS = "admin:admin"
uv run uvicorn modulo.api.main:app --reload --host 0.0.0.0 --port 8000

# 7. In a separate terminal, start the frontend
cd frontend
npm run dev
```

The backend is available at `http://localhost:8000` and the frontend at `http://localhost:5173`.

### Quick setup with Docker Compose (all services)

```powershell
docker compose up -d
```

This starts the database, Redis, backend, and frontend together. The backend
auto-seeds an admin user based on the `MODULO_USERS` environment variable.

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Yes | — | JWT signing key (min 32 bytes) |
| `FERNET_KEY` | Yes | — | Fernet encryption key for credentials |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./modulo.db` | Database connection string |
| `MODULO_DB` | No | `postgres` | Database dialect (`postgres`, `sqlite`, `mariadb`) |
| `REDIS_URL` | No | `redis://localhost:6379/0` | Redis connection for Celery task queue |
| `MODULO_PUBLIC_URL` | No | `http://localhost:8000` | Public-facing URL for OIDC/SAML callbacks |
| `MODULO_USERS` | No | — | Seed admin users (`username:password`) |

---

## Project Structure

```
Modulo\Development\Product\
├── backend/
│   ├── src/
│   │   └── modulo/
│   │       ├── api/              # FastAPI routes, middleware, DI
│   │       ├── auth/             # JWT, OIDC, SAML, API keys
│   │       ├── cli/              # Click-based CLI tools
│   │       │   ├── backup.py     #   modulo backup / restore
│   │       │   └── migrate.py    #   modulo-migrate export / import / verify
│   │       ├── connectors/       # External tool integrations
│   │       ├── core/             # Pipeline engine, eval, HITL, triggers
│   │       ├── db/               # SQLAlchemy models, CRUD, migrations, RLS
│   │       ├── model_backends/   # LLM provider wrappers
│   │       └── otel_bridge/      # OpenTelemetry ↔ LangGraph bridge
│   ├── tests/
│   │   ├── unit/                 # Unit tests (fast, no DB)
│   │   ├── integration/          # Integration tests (real Postgres)
│   │   └── bdd/                  # BDD tests (pytest-bdd + Playwright)
│   ├── migrations/               # Alembic migration scripts
│   ├── pyproject.toml            # Python deps, tool configs
│   ├── alembic.ini
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── stores/               # Pinia stores
│   │   ├── composables/          # Vue composables
│   │   ├── views/                # Route-level pages
│   │   └── components/           # Reusable components (shadcn-vue)
│   ├── src/__tests__/            # Vitest unit tests
│   ├── tests/                    # Playwright E2E tests
│   ├── package.json
│   └── Dockerfile
├── .github/
│   └── workflows/                # CI/CD pipeline definitions
├── docs/
│   ├── prd.md                    # Product requirements document
│   ├── adr/                      # Architecture decision records
│   ├── product-map/              # Feature graph entries
│   ├── security/                 # Security documentation
│   └── deployment/               # Deployment guides
├── docker-compose.yml            # Local dev: Postgres + Redis
└── docker-compose.mariadb.yml    # MariaDB override
```

CLI tools are registered as console scripts in `pyproject.toml`:

| Command | Entry point | Purpose |
|---|---|---|
| `modulo` | `modulo.cli.backup:cli` | Backup and restore database |
| `modulo-migrate` | `modulo.cli.migrate:cli` | Export/import/verify org data |

---

## Coding Standards

### Python (backend)

All Python code is checked with the following tools. Run them before pushing:

```powershell
cd backend

# Lint and format
uv run ruff check .
uv run ruff format --check .

# Type checking (strict mode)
uv run mypy src/modulo/

# Security scanning
uv run bandit -r src/modulo/ -ll
uv run semgrep --config=../.semgrep/ .

# Dependency audit
uv run pip-audit

# Import architecture enforcement
uv run lint-imports
```

**ruff configuration** (from `pyproject.toml`):
- Line length: 120
- Target: Python 3.12
- Enabled rule sets: pycodestyle (E, W), pyflakes (F), isort (I), pep8-naming (N),
  pyupgrade (UP), flake8-bugbear (B), flake8-bandit (S), flake8-async (ASYNC),
  ruff-specific (RUF)
- Per-file ignores: test files relax security and bugbear rules; specific files
  exempt naming conventions for SCIM and SAML integrations

**mypy configuration**: strict mode with `pydantic.mypy` plugin. LangGraph,
LangChain, testcontainers, and factory-boy imports are allowed untyped. BDD
step modules have relaxed rules.

### Import architecture (enforced by import-linter)

- `modulo.api` must not import `langgraph` directly
- `modulo.connectors` must not import `modulo.api` or `modulo.auth`
- `modulo.core`, `.api`, `.connectors` must not import `modulo_cloud`
- `modulo.otel_bridge` must not import `core.pipeline_engine`, `hitl_manager`, `eval_engine`

### TypeScript / Vue (frontend)

```powershell
cd frontend

# Lint
npm run lint                 # eslint src/**/*.{vue,ts,js}

# Type check
npm run type-check           # vue-tsc --noEmit

# Format check
npm run lint:fix             # auto-fix lint issues
```

### Pre-commit hooks

Install pre-commit hooks to automatically check staged changes:

```powershell
cd backend
uv run pre-commit install
```

### Commit guidelines

- Use present-tense, imperative-style commit messages
- Prefix with the area changed, e.g. `backend/auth: add OIDC refresh token support`
- Keep commits focused on a single concern
- Reference issues and PRs where applicable

---

## Testing

We maintain three test layers with increasing fidelity:

### Unit tests

Fast, no database required. Run from `backend/`:

```powershell
cd backend
uv run pytest tests/unit/ -q
```

Tests marked `awaiting-implementation` are excluded by default. Run them
explicitly with:

```powershell
uv run pytest tests/unit/ -m awaiting-implementation
```

### Integration tests

Require a running PostgreSQL instance. Run from `backend/`:

```powershell
docker compose up -d db
cd backend
uv run pytest tests/integration/ -m integration -n 2
```

Uses `testcontainers` for isolated database sessions. Concurrent execution
is supported via `pytest-xdist` (`-n` flag).

### BDD / E2E tests

Require PostgreSQL, Redis, a running backend, and a built frontend. Run from
`backend/`:

```powershell
docker compose up -d db redis
cd backend
uv run alembic upgrade heads
# In separate terminals:
#   backend:  uv run uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000
#   frontend: cd ../frontend && npm run build && npm run preview -- --port 4173
cd backend
uv run pytest tests/bdd/ -m e2e --base-url http://localhost:4173 -q
```

### Frontend unit tests

```powershell
cd frontend
npm run test:unit            # vitest run src
```

### Frontend E2E tests

```powershell
cd frontend
npm run test:e2e             # playwright test
```

### Coverage thresholds

| Target | Threshold | Measured by |
|---|---|---|
| Overall backend | 80% | pytest-cov |
| `modulo.auth` | 90% | pytest-cov |
| `pipeline_engine` | 85% | pytest-cov |
| `db.rls` | 95% | pytest-cov |

Coverage is enforced in CI via `coverage-thresholds.ps1`.

### Running tests on multiple databases

The CI runs unit tests against SQLite, PostgreSQL, and MariaDB. Use the
`MODULO_DB` environment variable to switch locally:

```powershell
$env:DATABASE_URL = "sqlite+aiosqlite:///./test.db"
$env:MODULO_DB = "sqlite"
uv run pytest tests/unit/ -m 'not integration' -q
```

---

## CI/CD

All CI runs on a self-hosted Windows runner with Docker. Workflows are defined
in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | Every push & PR | Backend lint (ruff, mypy, bandit, semgrep, pip-audit), frontend (lint, type-check, unit test, build, npm audit), product map validation, gitleaks secret scan |
| `unit.yml` | Every push & PR | Backend unit tests with coverage threshold enforcement |
| `integration.yml` | Push to main | Backend integration tests against real PostgreSQL |
| `bdd.yml` | Push to main | Full BDD/E2E suite: spin up Postgres + Redis, run migrations, build frontend, start backend, run Playwright tests |
| `multi-backend.yml` | Every push & PR | Unit tests against SQLite, PostgreSQL, and MariaDB |
| `container-scan.yml` | Every push | Build Docker images, scan with Trivy (CRITICAL severity → exit 1) |
| `security-audit.yml` | Weekly + dep PRs | pip-audit, npm audit, gitleaks, semgrep, Trivy container scan |
| `release.yml` | Manual dispatch + weekly | Bump version, update LICENSE, tag, push |

Jobs run in parallel where possible. Stale jobs are cancelled via
concurrency groups keyed on `${{ github.ref }}`.

### Local CI simulation

Use the `gate.ps1` script from the tooling repo to run the full CI suite on a
worktree branch before merging to main:

```powershell
..\..\..\..\devtools\harness\tools\gate.ps1 -Branch <branch-name>
```

---

## Pull Request Process

### Before submitting

1. Ensure your branch is up to date with `main`
2. Run the full test suite and lint checks (see [Testing](#testing) and [Coding Standards](#coding-standards))
3. Verify coverage thresholds are met
4. Update the product map entry for any feature changes (see `docs/product-map/`)
5. Update the PRD if your change introduces new behaviour

### Review requirements

- **Every PR requires at least one review** from a maintainer before merging
- The reviewer checks:
  - Correctness: tests pass, coverage met
  - Architecture: follows ADRs and import contracts
  - Security: no leaked secrets, input validation, RLS enforcement
  - Documentation: PRD and product map updated if behaviour changed

### Merge policy

- **Squash merge** is preferred for feature branches
- **Linear history** is required — no merge commits on `main`
- All CI checks must pass (lint, tests, coverage, security scans, container scan)
- Changes are committed directly to `main` after gate approval

### Branch naming

```
<type>/<short-description>
```

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `security`.

---

## Release Process

### Versioning

Modulo follows [Semantic Versioning 2.0.0](https://semver.org/). During the
alpha phase (`0.x`), breaking changes may occur in minor releases.

### How releases work

Releases are triggered via the `Release` GitHub Actions workflow. It runs
manually (`workflow_dispatch`) or on a weekly schedule. The process:

1. **Check for changes** — compares the latest `v*` tag against `HEAD`
2. **Determine version** — accepts `patch`, `minor`, or `major` bump (manual)
   or an explicit tag like `v1.2.3`
3. **Update files** — increments version in:
   - `backend/pyproject.toml`
   - `frontend/package.json`
   - `docs/prd.md`
   - `LICENSE` Change Date (3 years from release)
4. **Generate SBOM** — runs `backend/scripts/generate-sbom.py`
5. **Commit and tag** — `git tag -a v<version>`
6. **Push** — tags and commits are pushed to origin

### Changelog

A changelog is maintained in `docs/prd.md` — each release adds an entry under
the version heading with notable additions, changes, and fixes.

---

## Security

Security is a top priority. Please report vulnerabilities responsibly.

For details on supported versions, disclosure timelines, and our coordinated
disclosure process, see [`SECURITY.md`](SECURITY.md).

### Security contacts

- **Email**: `security@modulo.run`
- **Do not** open public GitHub issues for security vulnerabilities

### Security documentation

| Document | Location |
|---|---|
| Secret management | `docs/security/secret-management.md` |
| Input validation guide | `docs/security/input-validation-guide.md` |
| Dependency update policy | `docs/security/dependency-policy.md` |
| Penetration test plan | `docs/security/penetration-test-plan.md` |
| Incident response playbook | `docs/security/incident-response-playbook.md` |

### Security best practices for contributors

- Never commit secrets, API keys, or credentials
- Always use Pydantic validation for API inputs — never accept raw request bodies
- Ensure every new API route and MCP tool handler enforces RLS
- Use advisory locks (`modulo.db.repositories.locks`) for concurrency-sensitive
  database operations
- Decrypted credentials must never enter LangGraph state, checkpoint blobs,
  OTel spans, or logs

---

*Thank you for contributing to Modulo.*
