# ADR 002 — Multi-Backend Database Abstraction Strategy

**Date**: 2026-06-26
**Status**: Draft — implementation in progress

> **Update 2026-07-11: MariaDB deprecated.** MariaDB support was added as premature generality (see architecture critique 2026-07-09). Production and demo run on Postgres (Supabase). MariaDB references are preserved for backward compatibility but the backend is not actively tested or maintained.

## Context

Modulo ships as a self-hosted product. Currently it requires PostgreSQL for all deployments, including local development and CI. This creates friction for:

- **Quick-start evaluations**: users who want a single `docker compose up` with SQLite or MariaDB for a 5-minute trial
- **Resource-constrained environments**: users who can't run Postgres (low-memory VPS, no Postgres native on their platform)
- **Demo / tightly-bundled deployment**: an all-in-one container with SQLite would eliminate a separate Postgres dependency

Additionally, the demo app at `demo.modulo.run` uses **Supabase Postgres** — which is still Postgres and requires no abstraction. The multi-backend work is orthogonal to the demo.

Externalising database choice makes Modulo more accessible without changing the product's capabilities for Postgres users.

## The ORM Question

SQLAlchemy 2.0 already supports PostgreSQL, MySQL/MariaDB, SQLite, Oracle, and MSSQL through swappable async dialects — one connection string change is all it takes, provided dialect-specific features are avoided.

| Feature | SQLAlchemy 2.0 | Needed for multi-backend |
|---|---|---|
| Async | Native (`create_async_engine`) | — |
| Postgres | asyncpg | — |
| MySQL/MariaDB | aiomysql (untested against MariaDB) | Need testing |
| SQLite | aiosqlite | Already works (dev-only today) |
| UUID PKs | Built-in `Uuid` (2.0.23+) | Replace `postgresql.UUID` |
| JSON columns | Generic `JSON` type | Already generic |
| RLS / tenancy | PG-only feature | Need app-level abstraction |
| Migrations | Alembic (37 revisions) | Need conditional DDL |

**The gap isn't the ORM — it's our code.** We use Postgres-specific features (`postgresql.UUID`, `SET LOCAL` for RLS, advisory locks, PG JSON casts) that lock us to Postgres. SQLAlchemy itself handles cross-backend UUIDs, JSON, and relationships just fine.

## Decision

Implement a three-layer abstraction to decouple the application from PostgreSQL without losing Postgres-native capabilities when available:

### Layer 1: Model types (trivial — done in worktree)

Replace `from sqlalchemy.dialects.postgresql import UUID` with SA's generic `Uuid`:

```python
# Before
from sqlalchemy.dialects.postgresql import UUID
id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

# After
from sqlalchemy import Uuid
id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
```

**Storage by backend:** PG → native `UUID` type, MySQL/MariaDB → `CHAR(32)`, SQLite → `CHAR(32)`. The ORM handles the mapping transparently.

### Layer 2: Repository ABC (moderate — implemented in worktree)

Follow the existing Hub pattern (ConnectorHub, ModelBackendHub, SecretsBackend, RuntimeProvider):

```
backend/src/modulo/db/
  repositories/
    __init__.py           # Hub: resolves repo class from DATABASE_URL scheme
    base.py               # ABC with: apply_tenant_filter(), execute(), paginate()
    postgres.py           # SET LOCAL + PG dialect overrides
    generic.py            # WHERE org_id = :oid for MySQL/SQLite
    locks.py              # ABC with PG (advisory_lock) and generic (in-memory lock) impls
```

Core engine code uses `BaseRepository` — never touches `AsyncSession` directly. The Hub picks the implementation from `MODULO_DB` setting or `DATABASE_URL` scheme.

What this enables:
- Postgres (self-hosted or Supabase) → `PostgresRepository`
- MariaDB → `GenericRepository` (with WHERE-clause tenant filtering)
- SQLite (dev) → `GenericRepository`
- No code changes in routes, engine, or services

### Layer 3: Backend-specific features (conditional — future)

Things like advisory locks, `FOR UPDATE SKIP LOCKED`, and check constraints need conditional paths:

```python
# db/repositories/locks.py — ABC with PG (advisory_lock) and generic (in-memory lock) impls
# db/migrations/ — Alembic env.py checks backend, skips PG-only DDL
```

## Phasing

| Phase | When | What |
|---|---|---|
| **1** | Now | Swap `postgresql.UUID` → `Uuid` across all model files, add Repository ABC + Hub |
| **2** | Now | Add `GenericRepository` with WHERE-clause tenant filtering (MariaDB/SQLite) |
| **3** | Now | Prove MariaDB async driver works via `docker-compose.override.yml` |
| **4** | Next | Move Alembic migrations to conditionally skip PG-only DDL |
| **5** | Post-alpha | Abstract advisory locks and `FOR UPDATE` patterns |

## Key Constraints

- **All existing Postgres deployments must continue to work** with zero config changes. The abstraction is additive — code uses `BaseRepository` which dispatches to `PostgresRepository` for PG URLs.
- **Supabase deployments are not affected** — Supabase IS Postgres. The demo path is independent of this work.
- **The Repository pattern must not leak into API routes or services.** Only engine-level code (pipeline execution, run management) deals with repositories directly.

## Consequences

- **Positive**: Users can run Modulo with SQLite for dev or MariaDB for prod without changing application code
- **Positive**: Lower barrier to entry — `docker compose up` with a single DB container
- **Positive**: More resilient testing — can run integration tests against SQLite without Docker
- **Negative**: Some Postgres-native features (RLS, advisory locks) need parallel implementations
- **Negative**: Maintenance burden of testing against multiple backends in CI

## Related Documents

- Previous planning: `Website/demo-website-plan.md` (now trimmed to demo-only content)
- Docker Compose: `docker-compose.yml`, `docker-compose.local.yml` (Postgres), `docker-compose.mariadb.yml` (new)
- Models: `backend/src/modulo/db/models/`
