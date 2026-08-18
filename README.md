# Modulo

<!-- HERO: logo + tagline + badge row -->
<p align="center">
  <img src="docs/assets/modulo-logo.svg" width="200" alt="Modulo logo"/>
</p>

<p align="center">
  <em>Modulo is a self-hosted agent governance platform for building governed,
  repeatable AI-assisted software delivery pipelines.</em>
</p>

<!-- BADGE ROW — active badges (work on private repo) -->
<p align="center">
  <a href="https://github.com/farnalabs/modulo/actions"><img src="https://img.shields.io/github/actions/workflow/status/farnalabs/modulo/ci.yml?branch=main&label=CI&logo=github"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-BSL%201.1-blue"/></a>
  <a href="https://docs.modulo.run"><img src="https://img.shields.io/badge/docs-docs.modulo.run-blue"/></a>
  <a href="https://app.modulo.run"><img src="https://img.shields.io/badge/app-app.modulo.run-blue"/></a>
</p>

<!-- PUBLIC LAUNCH ONLY — uncomment when repo goes public:
<p align="center">
  <img src="https://img.shields.io/github/stars/farnalabs/modulo"/>
  <img src="https://img.shields.io/github/forks/farnalabs/modulo"/>
  <img src="https://img.shields.io/github/v/release/farnalabs/modulo"/>
</p>
-->

<!-- SONARQUBE/SONARCLOUD — uncomment when FAR-319 lands:
<p align="center">
  <a href="https://sonarcloud.io/dashboard?id=modulo"><img src="https://sonarcloud.io/api/project_badges/quality_gate?project=modulo"/></a>
  <a href="https://sonarcloud.io/dashboard?id=modulo"><img src="https://sonarcloud.io/api/project_badges/measure?project=modulo&metric=coverage"/></a>
  <a href="https://sonarcloud.io/dashboard?id=modulo"><img src="https://sonarcloud.io/api/project_badges/measure?project=modulo&metric=bugs"/></a>
  <a href="https://sonarcloud.io/dashboard?id=modulo"><img src="https://sonarcloud.io/api/project_badges/measure?project=modulo&metric=security_rating"/></a>
</p>
-->

> [!WARNING]
> **Alpha software.** Modulo is under active development. Interfaces, database
> migrations, configuration, and deployment procedures may change. Evaluate it
> before using it with production data and keep current backups.

## Table of Contents
- [What it is](#what-it-is)
- [Key features](#key-features)
- [Quick start](#quick-start)
- [Documentation](#documentation)
- [Architecture](#architecture)
- [Development and testing](#development-and-testing)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## What it is

Modulo is a self-hosted agent governance platform for building governed, repeatable
AI-assisted software delivery pipelines. It connects atomic agents to external tools like GitHub, GitLab, and Slack
while keeping execution, approvals, audit data, and credentials under the
operator's control.

Unlike running agents ad hoc, Modulo gives you a visual, composable pipeline of
atomic AI agents with built-in governance: role-based access, audit trails,
human-in-the-loop approvals, cost controls, and evaluation gates — so AI-assisted
delivery is repeatable and auditable, not a one-off experiment.

The detailed product intent and delivery status live in [the PRD](docs/prd.md).
Items described there may be planned or partially delivered; check the relevant
product-map entry and tests before relying on a capability.

## Key features

- A visual pipeline editor and reusable pipeline templates
- Agent, manual, conditional, parallel, and approval stages
- Run history, evaluation, cost controls, and observability integrations
- Role-based access controls, audit trails, SSO, and feature licensing
- Extensible model backends, connectors, MCP tools, and runtime providers
- PostgreSQL as the primary database, with additional database conformance work

## Quick start

The shortest local evaluation uses Docker Desktop and Docker Compose:

```bash
git clone https://github.com/farnalabs/modulo.git
cd modulo
docker compose up -d
```

On Windows, use PowerShell with `Set-Location modulo` instead of `cd modulo`.

After the services become healthy, open <http://localhost:5173> and sign in
with the local demo credentials `admin` / `admin`. These credentials and the
Compose secrets are for local evaluation only.

For a development setup with the API and frontend running outside containers,
follow the [quick-start guide](docs/quickstart.md). The full setup requires
Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20+, and Docker Desktop.

### Local SAQ workers (required for pipeline execution and cron)

Modulo executes pipeline runs through **SAQ** workers.
After starting Postgres + Redis (e.g. `docker compose up -d` or
`docker compose -f docker-compose.local.yml up -d`), launch the workers:

```bash
# Runs worker — executes run jobs (queue: runs)
uv run python -m saq modulo.core.saq_worker.runs_settings

# System worker — scheduler (fire_due_triggers) + reconcile + system crons.
# The web UI binds 127.0.0.1:8081; requires SAQ_AUTH_USERNAME/SAQ_AUTH_PASSWORD.
SAQ_AUTH_USERNAME=admin SAQ_AUTH_PASSWORD=admin \
  uv run python -m modulo.core.saq_worker
```

Note: `python -m saq` takes the **settings module** as its only positional
argument — there is no `worker` subcommand in SAQ 0.26.4. Running a local
Redis is required (see `REDIS_URL`). The compose stack (`docker-compose.yml`
and `docker-compose.local.yml`) includes `saq-runner` and `saq-system`
services that launch both workers for you. The `saq-system` service is
**required** for local cron/triggers to fire — a dev running only
Postgres + Redis + uvicorn gets zero trigger firing.

## Documentation

- [Quick start](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Configuration reference](docs/configuration-reference.md)
- [Deployment guide](docs/deployment.md)
- [System requirements](docs/system-requirements.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Upgrade process](docs/upgrade-process.md)

## Architecture

| Area | Technology |
|---|---|
| API and workers | Python 3.12, FastAPI, SQLAlchemy, Alembic, SAQ |
| Agent orchestration | LangGraph and provider-specific LangChain packages |
| Web application | Vue 3, TypeScript, Pinia, Vite |
| Data services | PostgreSQL 16 and Redis 7 |
| Local deployment | Docker Compose |
| Quality tooling | pytest, Vitest, Ruff, mypy, ESLint, Semgrep, Bandit |

## Development and testing

Install and check each application from its own directory:

```bash
cd backend
uv sync --frozen
uv run pytest tests/unit tests/architecture
uv run ruff check .

cd ../frontend
pnpm install --frozen-lockfile
pnpm run lint
pnpm run type-check
pnpm run test:unit
```

On Windows, use PowerShell with `Set-Location backend` and
`Set-Location ../frontend` instead of `cd`.

The repository also contains integration, multi-database, browser, security,
and container suites. Some require local services or deployment credentials
and therefore do not run on every pull request. See [TESTING.md](TESTING.md)
for the current test matrix and prerequisites.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Modulo is
alpha software, so focused changes with tests and documentation are easiest to
review. Use GitHub issues for reproducible bugs and scoped feature proposals.

## Security

Do not report vulnerabilities in a public issue. Follow [SECURITY.md](SECURITY.md)
and email `security@modulo.run` with reproduction details.

## License

Modulo is licensed under the [Business Source License 1.1](LICENSE). Production
and commercial use is permitted, except offering Modulo as a paid hosted or
managed service to third parties. Each release converts to the Apache License
2.0 on its stated Change Date, three years after release.
