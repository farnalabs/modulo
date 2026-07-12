# Modulo

Modulo is a self-hosted orchestration layer for building governed, repeatable
AI-assisted software delivery pipelines. It connects atomic agents to tools
such as GitHub, Linear, and Notion while keeping execution, approvals, audit
data, and credentials under the operator's control.

> **Alpha software:** Modulo is under active development. Interfaces, database
> migrations, configuration, and deployment procedures may change. Evaluate it
> before using it with production data and keep current backups.

## What it includes

- A visual pipeline editor and reusable pipeline templates
- Agent, manual, conditional, parallel, and approval stages
- Run history, evaluation, cost controls, and observability integrations
- Role-based access controls, audit trails, SSO, and feature licensing
- Extensible model backends, connectors, MCP tools, and runtime providers
- PostgreSQL as the primary database, with additional database conformance work

The detailed product intent and delivery status live in [the PRD](docs/prd.md).
Items described there may be planned or partially delivered; check the relevant
product-map entry and tests before relying on a capability.

## Architecture

| Area | Technology |
|---|---|
| API and workers | Python 3.12, FastAPI, SQLAlchemy, Alembic, Celery |
| Agent orchestration | LangGraph and provider-specific LangChain packages |
| Web application | Vue 3, TypeScript, Pinia, Vite |
| Data services | PostgreSQL 16 and Redis 7 |
| Local deployment | Docker Compose |
| Quality tooling | pytest, Vitest, Ruff, mypy, ESLint, Semgrep, Bandit |

## Quick start

The shortest local evaluation uses Docker Desktop and Docker Compose:

```powershell
git clone https://github.com/farnalabs/modulo.git
Set-Location modulo
docker compose up -d
```

After the services become healthy, open <http://localhost:5173> and sign in
with the local demo credentials `admin` / `admin`. These credentials and the
Compose secrets are for local evaluation only.

For a development setup with the API and frontend running outside containers,
follow the [quick-start guide](docs/quickstart.md). The full setup requires
Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20+, and Docker Desktop.

## Documentation

- [Quick start](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Configuration reference](docs/configuration-reference.md)
- [Deployment guide](docs/deployment.md)
- [System requirements](docs/system-requirements.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Upgrade process](docs/upgrade-process.md)

## Development and testing

Install and check each application from its own directory:

```powershell
Set-Location backend
uv sync --frozen
uv run pytest tests/unit tests/architecture
uv run ruff check .

Set-Location ../frontend
npm ci
npm run lint
npm run type-check
npm run test:unit
```

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
