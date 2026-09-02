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

Modulo is a self-hosted agent governance platform for building governed,
repeatable AI-assisted software delivery pipelines. It provides a composable
pipeline of atomic AI agents that automate work between existing tools like
GitHub, GitLab, and Slack.

We're glad you're here. Be respectful, constructive, and assume good faith.

By contributing, you agree that your contributions are licensed under the
project's [Business Source License 1.1](LICENSE).

Use the [bug report](.github/ISSUE_TEMPLATE/bug_report.yml) and [feature
request](.github/ISSUE_TEMPLATE/feature_request.yml) templates when opening
issues.

We do not yet have a formal code of conduct; be respectful, constructive, and
assume good faith.

---

## Development Setup

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | >= 3.12 | Managed via `uv` |
| Node.js | >= 20 | Frontend tooling |
| Docker | Latest | Postgres, Redis, MariaDB |
| uv | Latest | [Install uv](https://docs.astral.sh/uv/getting-started/installation/) |

### Required services

The project needs a running PostgreSQL 16 and Redis 7 instance. Use Docker
Compose to start them:

```powershell
docker compose -f docker-compose.local.yml up -d db-local redis-local
```

This starts PostgreSQL on `localhost:5434` and Redis on `localhost:6380`
with the following defaults:

| Variable | Default |
|---|---|
| `POSTGRES_USER` | `modulo` |
| `POSTGRES_PASSWORD` | `modulo` |
| `POSTGRES_DB` | `modulo` |
| `DATABASE_URL` | `postgresql+asyncpg://modulo:modulo@localhost:5434/modulo` |
| `REDIS_URL` | `redis://localhost:6380/0` |

To use MariaDB instead of PostgreSQL, apply the override:

```powershell
docker compose -f docker-compose.yml -f deploy/compose/docker-compose.mariadb.yml up -d
```

The backend auto-detects MariaDB and configures the connection string
(`mysql+aiomysql://modulo:modulo@localhost:5435/modulo`). Note that MariaDB is
deprecated (2026-07-11); PostgreSQL is the supported primary database.

---

## Quick Start

```powershell
# 1. Clone the repository
git clone https://github.com/farnalabs/modulo.git modulo
cd modulo

# 2. Start infrastructure
docker compose -f docker-compose.local.yml up -d db-local redis-local

# 3. Install backend dependencies
cd backend
uv sync --frozen
cd ..

# 4. Run database migrations
cd backend
uv run alembic upgrade heads
cd ..

# 5. Install frontend dependencies
cd frontend
pnpm install --frozen-lockfile
cd ..

# 6. Configure environment
$env:SECRET_KEY = "dev-secret-key-32-bytes-at-least-here!"
# Generate your own key, e.g.: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:FERNET_KEY = "<generate-your-own-fernet-key>"
$env:DATABASE_URL = "postgresql+asyncpg://modulo:modulo@localhost:5434/modulo"
$env:REDIS_URL = "redis://localhost:6380/0"
$env:MODULO_USERS = "admin:admin"
```

**Terminal 1 — backend**

```powershell
cd backend
uv run uvicorn modulo.api.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — frontend**

```powershell
cd frontend
pnpm run dev
```

The backend is available at `http://localhost:8000` and the frontend at
`http://localhost:5173`.

**Background workers (required for pipeline execution and cron)**

Pipeline runs and scheduled triggers execute through background SAQ workers.
When running the API outside containers you must start them yourself:

```powershell
# Runs worker — executes run jobs
uv run python -m saq modulo.core.saq_worker.runs_settings

# System worker — scheduler + reconcile + crons (requires SAQ_AUTH_USERNAME/SAQ_AUTH_PASSWORD)
$env:SAQ_AUTH_USERNAME = "admin"; $env:SAQ_AUTH_PASSWORD = "admin"
uv run python -m modulo.core.saq_worker
```

The `python -m saq` argument is the settings module (no `worker` subcommand in
SAQ 0.26.4). With only Postgres + Redis + uvicorn running, no pipeline run
executes and no trigger fires — the workers are mandatory. If you used
`docker compose -f docker-compose.local.yml up -d` with the full local stack,
`saq-runner` and `saq-system` services launch both for you.

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
| `FERNET_KEY` | Yes | — | Fernet encryption key for credentials (valid urlsafe-base64 key, min 32 bytes) |
| `DATABASE_URL` | Yes | — | Database connection string (required, no static default); the SQLite URL applies only when `MODULO_DB=sqlite` |
| `MODULO_DB` | No | `postgres` | Database dialect (`postgres`, `sqlite`, `mariadb`) |
| `REDIS_URL` | No | `redis://localhost:6379/0` | Redis connection for the SAQ broker (required in production) |
| `MODULO_PUBLIC_URL` | No | `http://localhost:8000` | Public-facing URL for OIDC/SAML callbacks |
| `MODULO_USERS` | No | — | Seed admin users (`username:password`) |

---

## Project Structure

```
modulo/
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
│   │       │   └── migrations/    #   Alembic migration scripts
│   │       ├── model_backends/   # LLM provider wrappers
│   │       └── otel_bridge/      # OpenTelemetry ↔ LangGraph bridge
│   ├── tests/
│   │   ├── unit/                 # Unit tests (fast, no DB)
│   │   ├── integration/          # Integration tests (real Postgres)
│   │   └── bdd/                  # BDD tests (pytest-bdd + Playwright)
│   ├── pyproject.toml            # Python deps, tool configs
│   ├── alembic.ini
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── stores/               # Pinia stores
│   │   ├── composables/          # Vue composables
│   │   ├── views/                # Route-level pages
│   │   ├── components/           # Reusable components (shadcn-vue)
│   │   └── __tests__/            # Vitest unit tests
│   ├── tests/                    # Playwright E2E tests
│   ├── package.json
│   └── Dockerfile
├── .github/
│   └── workflows/                # CI/CD pipeline definitions
├── docs/
│   ├── product-map/              # Feature graph entries (see product-map/README.md)
│   ├── security/                 # Security documentation
│   └── deployment/               # Deployment guides
      (Architecture decision records moved to the private farnalabs/devtools repo,
       Repos/devtools/adr/, 2026-09-02 — FAR-434)
      (product map: frontend/src/manifest.yaml — feature registry + per-route refs, ADR 008)
├── deploy/
│   ├── compose/                  # Non-default compose files: docker-compose.{prod,test,mariadb}.yml
│   ├── docker/                   # Dockerfile.{all-in-one,backend,frontend}
│   └── fly/                      # fly.staging.toml + Fly helper scripts
├── docker-compose.yml            # Full stack: Postgres + Redis + backend + workers + frontend
├── docker-compose.local.yml      # Local dev infra: Postgres + Redis + observability
├── fly.toml                      # Fly config for app.modulo.run
└── ...                           # dotfile config (.gitignore, ruff.toml, .pre-commit-config.yaml, ...)
```

CLI tools are registered as console scripts in `pyproject.toml`:

| Command | Entry point | Purpose |
|---|---|---|
| `modulo` | `modulo.cli.backup:cli` | Backup and restore database |
| `modulo-migrate` | `modulo.cli.migrate:cli` | Export/import/verify org data |
| `modulo-break-glass` | `modulo.cli.break_glass:cli` | Emergency break-glass operations |

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
pnpm run lint                 # eslint src/**/*.{vue,ts,js}

# Type check
pnpm run type-check           # vue-tsc --noEmit

# Format check
pnpm run lint:fix             # auto-fix lint issues
```

### Pre-commit hooks

Install pre-commit hooks to automatically check staged changes. Run from the
repository root (`.pre-commit-config.yaml` lives there; `pre-commit` is
installed as a global uv tool):

```powershell
pre-commit install
```

### Commit guidelines

- Use present-tense, imperative-style commit messages
- Prefix with a conventional-commit scope, e.g. `feat(auth): add OIDC refresh token support`
- PR titles that resolve a Linear ticket carry the ticket identifier, e.g. `feat(FAR-123): ...`
- Keep commits focused on a single concern
- Reference issues and PRs where applicable

---

## Testing

We maintain three test layers with increasing fidelity:

### Unit tests

Fast, no database required. Run from `backend/`:

```powershell
cd backend
uv run pytest tests/unit tests/architecture --tb=short -q --timeout=120
```

Tests marked `awaiting-implementation` are excluded by default. Run them
explicitly with:

```powershell
uv run pytest tests/unit/ -m awaiting-implementation
```

### Architecture tests

Dependency/layer-contract enforcement via import-linter. Run from `backend/`:

```powershell
uv run pytest tests/architecture -q
```

### Integration tests

Require a running PostgreSQL instance. Run from `backend/`:

```powershell
docker compose -f docker-compose.local.yml up -d db-local
cd backend
uv run pytest tests/integration/ -m integration -n 2
```

Uses `testcontainers` for isolated database sessions. Concurrent execution
is supported via `pytest-xdist` (`-n` flag).

### BDD / E2E tests

Require PostgreSQL, Redis, a running backend, and a built frontend. Run from
`backend/`:

```powershell
docker compose -f docker-compose.local.yml up -d db-local redis-local
cd backend
uv run alembic upgrade heads
# Terminal 1 — backend:
uv run uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — frontend (build + preview):
cd ../frontend
pnpm run build
pnpm run preview -- --port 4173
cd backend
uv run pytest tests/bdd/ -m e2e --base-url http://localhost:4173 -q
```

Conventions: Playwright tests run against `?theme=agent`; every interactive
element needs a `data-testid`; use `waitForSelector('[data-loading="false"]')`
— never `waitForTimeout()`.

### Frontend unit tests

```powershell
cd frontend
pnpm run test:unit            # vitest run src
```

### Frontend E2E tests

```powershell
cd frontend
pnpm run test:e2e             # playwright test
```

### Coverage thresholds

| Target | Threshold | Measured by |
|---|---|---|
| Overall backend | 80% | pytest-cov |
| `modulo.auth` | 90% | pytest-cov |
| `pipeline_engine` | 85% | pytest-cov |
| `db.rls` | 95% | pytest-cov |

Coverage is enforced in CI via the coverage-thresholds script
(`.github/scripts/coverage-thresholds.sh` on Linux runners).

### Running tests on multiple databases

The backend nominally supports PostgreSQL, SQLite, and MariaDB/MySQL (see
`docs/architecture.md`). Use the `MODULO_DB` environment variable to switch
locally:

```powershell
$env:DATABASE_URL = "sqlite+aiosqlite:///./test.db"
$env:MODULO_DB = "sqlite"
uv run pytest tests/unit/ -m 'not integration' -q
```

---

## CI/CD

All CI runs on hosted Ubicloud runners (ubicloud-standard-2). Workflows are defined
in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | Push to main, every PR, manual | Backend lint (ruff, ruff-format, mypy, bandit, vulture, semgrep, pip-audit, import-linter); backend unit tests with coverage + architecture tests + changed integration tests; frontend (lint, type-check, vitest, build, pnpm audit, WCAG contrast); schema freshness; product-map validation; manifest validation; gitleaks secret scan |
| `bdd.yml` | Push to main, every PR, manual | Full BDD/E2E suite: Postgres + Redis, Alembic migrations, frontend build, backend + preview, pytest-bdd Playwright suite |
| `deploy.yml` | Push to main, manual | Deploy pipeline: throttle check → pre-deploy full CI → staging deploy + staging E2E → production deploy + prod smoke tests |
| `merge-queue.yml` | Cron every 15 min, manual | Merge queue: squash-merges approved PRs to main after CI + approval re-verification; closes Linear tickets; dispatches CI/deploy on main |
| `pr-review.yml` | Manual dispatch | Automated PR review |
| `branch-fixer.yml` | Manual/webhook dispatch | Fixes failing or review-blocked PRs in sandboxes |
| `alpha-handover-report.yml` | Manual dispatch | Verifies alpha handover criteria with evidence |
| `schemathesis-nightly.yml` | Cron nightly, manual | Nightly API contract fuzzing against main |

Jobs run in parallel where possible. Stale jobs are cancelled via
concurrency groups keyed on `${{ github.ref }}`.

### PR-based delivery

Modulo uses PR-based delivery. Push your branch, open a pull request, and CI
(`ci.yml`) validates it automatically; merging is handled by the
`merge-queue.yml` workflow once checks pass and review approves. For
farnalabs-internal delivery the `<push-and-create-PR>` / `<poll-PR-until-merged>` helpers
exist in the devtools tooling repo.

---

## Pull Request Process

### Before submitting

1. Ensure your branch is up to date with `main`
2. Run the test suites and lint checks relevant to your change (see
   [Testing](#testing) and [Coding Standards](#coding-standards))
3. Verify coverage thresholds are met
4. Update the product map entry for any feature changes (see the product map — the
   feature graph lives in `docs/product-map/`, keyed by the `features:` registry and
   route `product_map` refs in `frontend/src/manifest.yaml`; see
   `docs/product-map/README.md`)
5. Update the product map (frontend/src/manifest.yaml) and relevant docs if your change introduces new behaviour

### Review requirements

- PRs are reviewed by the automated PR Reviewer (`pr-review.yml`) and, where
  needed, maintainers. The reviewer checks:
  - Correctness: tests pass, coverage met
  - Architecture: follows ADRs and import contracts
  - Security: no leaked secrets, input validation, RLS enforcement
  - Documentation: product map (frontend/src/manifest.yaml) and relevant docs updated if behaviour changed

### Merge policy

**Squash merge** is used for feature branches. Merging is performed by the
`merge-queue.yml` workflow, which squash-merges approved PRs after CI passes
and re-verifies review approval — no direct commits to `main`. All CI checks
must pass before merge.

### Branch naming

Use branch names that reflect the work:

- `task-far-<n>-<short-description>` for Linear-ticket work
- `dist/<short-description>` for distribution batches
- `fix/deploy-<sha>` for CI/deploy hotfixes
- `docs/<short-description>` for documentation

---

## Release Process

### Versioning

Modulo follows [Semantic Versioning 2.0.0](https://semver.org/). During the
alpha phase (`0.x`), breaking changes may occur in minor releases.

### How releases work

Modulo does not run a manual release workflow. Version bumps happen at publish
time (via `publish.ps1` verify step / CI) — never per merge — and a changelog
is maintained in the product roadmap. Before a release tag is created, maintainers
update both application version files together and set the `LICENSE` Change
Date to three years after the release date.

### Changelog

A changelog is maintained in the product roadmap — each release adds an entry under
the version heading with notable additions, changes, and fixes.

---

## Security

Security is a top priority. Please report vulnerabilities responsibly.

For details on supported versions, disclosure timelines, and our coordinated
disclosure process, see [`SECURITY.md`](SECURITY.md).

### Security contacts

Prefer GitHub's **Report a vulnerability** (private security advisory) on the
repository page, or email `security@modulo.run`.

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
