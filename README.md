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
  <a href="https://github.com/farnalabs/modulo/tree/main/docs"><img src="https://img.shields.io/badge/docs-github-blue"/></a>
  <a href="https://app.modulo.run"><img src="https://img.shields.io/badge/app-app.modulo.run-blue"/></a>
</p>

> [!WARNING]
> **Alpha software.** Modulo is under active development. Interfaces, database
> migrations, configuration, and deployment procedures may change. Evaluate it
> before using it with production data and keep current backups.

## Table of Contents
- [What it is](#what-it-is)
- [Core concepts](#core-concepts)
- [Key features](#key-features)
- [Quick start](#quick-start)
- [Documentation](#documentation)
- [Architecture](#architecture)
- [Development and testing](#development-and-testing)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## What it is

Modulo turns AI agent runs into governed, repeatable delivery pipelines. It
connects atomic agents to external tools like GitHub, GitLab, and Slack while
keeping execution, approvals, audit data, and credentials under the operator's
control.

Agent governance, in practice, means you wire AI agents into your tools inside
a visual pipeline where every run is approved, audited, and budgeted — so
AI-assisted delivery is repeatable and auditable, not a one-off experiment.

The detailed product intent and delivery status live in [the PRD](docs/prd.md).
Items described there may be planned or partially delivered; check the relevant
documentation and tests before relying on a capability.

## Core concepts

**Pipelines.** Modulo is a visual, composable pipeline of atomic AI agents —
agent, manual, conditional, parallel, and approval nodes — that automates work
between your existing tools. Runs are executed, evaluated, and audited.

**Bring your own agent runtime.** Agents run in an agentic sandbox platform of
your choice (for example E2B or local Docker). Modulo dispatches work to the
sandbox, collects the structured output, and owns the governance around it:
auth, audit, cost tracking, evaluation gates, and human-in-the-loop approvals.

**Schemas everywhere.** Every node's input and output is a typed JSON Schema.
Schemas define the contracts between stages, can be inferred from your
connected tools, are validated at run time, and migrate between versions.

**Everything is versioned.** Pipelines are snapshotted at run time, schemas and
agent prompts are versioned, and every action is written to an audit trail. You
can always see exactly what ran, with what inputs, and why.

## Key features

- A visual pipeline editor and reusable pipeline templates
- Agent, manual, conditional, parallel, and approval nodes
- Run history, evaluation, cost controls, and observability integrations
- Role-based access controls, audit trails, single sign-on (SSO), and feature licensing
- Extensible model backends, connectors, Model Context Protocol (MCP) tools, and runtime providers
- PostgreSQL 16 as the primary database, with additional database conformance work

## Quick start

The shortest local evaluation uses Docker Desktop and Docker Compose:

```bash
git clone https://github.com/farnalabs/modulo.git
cd modulo
docker compose up -d
```

The first run builds the backend and frontend images and may take a few minutes.

The Compose stack includes PostgreSQL and Redis. Redis is required for pipeline
execution and scheduled triggers — it is started automatically by
`docker compose up -d`.

After the services become healthy, open <http://localhost:5173> and sign in
with the local demo credentials `admin` / `admin`. These credentials and the
Compose secrets are for local evaluation only.

For a development setup with the API and frontend running outside containers,
follow the [quick-start guide](docs/quickstart.md). The full setup requires
Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20+, and Docker Desktop.

**Next steps.** To run a first pipeline, connect a model backend, bring an
agentic sandbox platform (for example E2B) or use a local runtime, define the
schemas for your stages, and trigger a run. The [quick-start
guide](docs/quickstart.md) walks through the how-to, and the [configuration
reference](docs/configuration-reference.md) documents every environment
variable.

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
| API and workers | Python 3.12, FastAPI, SQLAlchemy, Alembic |
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

The repository also contains integration, multi-database, browser, security,
and container suites. Some require local services or deployment credentials
and therefore do not run on every pull request. See
[docs/definition-of-done.md](docs/definition-of-done.md) §1 for the current
test suite inventory and prerequisites.

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
