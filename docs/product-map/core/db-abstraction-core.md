---
id: feat-core-db-abstraction-core
prd: N/A
adr:
  - docs/adr/002-database-abstraction-strategy.md
code:
  - backend/src/modulo/db/session.py
  - backend/src/modulo/db/repositories/base.py
  - backend/src/modulo/db/repositories/generic.py
  - backend/src/modulo/db/repositories/postgres.py
  - backend/src/modulo/db/repositories/locks.py
  - backend/src/modulo/db/migrations
unit-tests:
  - backend/tests/unit/db/test_session.py
  - backend/tests/unit/db/test_repositories_base.py
  - backend/tests/unit/db/test_repositories_generic.py
  - backend/tests/unit/db/test_repositories_locks.py
  - backend/tests/unit/db/test_multi_backend_bdd.py
  - backend/tests/unit/db/test_rls_multibackend.py
bdd:
  - backend/tests/bdd/features/organisation/multi_backend.feature
  - backend/tests/bdd/steps/test_multi_backend.py
depends-on: []
status: covered
---

# Database Abstraction Core

The multi-backend data layer (ADR 002): PostgreSQL 16 as the primary backend with
conformance support for SQLite and MariaDB/MySQL, repository + session plumbing,
and org-scoped tenant context injection that is backend-aware.

## Behaviours

- [x] Repository base (`RepositoryBase`) provides CRUD primitives shared by generic
      and Postgres repositories
- [x] `GenericRepository` works across backends; `PostgresRepository` adds
      `SET LOCAL`/`set_config` org-context for transaction-local tenant isolation
- [x] `set_org_context` requires an active transaction (raises otherwise) and skips
      the DB call when a non-Postgres backend is active
- [x] `apply_tenant_filter` injects a `WHERE organisation_id = ...` predicate for
      org-scoped entities; skips entities without an org column and handles joined
      multi-org entities
- [x] Advisory locks are backend-portable (`locks.py`)
- [x] ALTER/revision path is Alembic-migratable against Postgres (migrations dir)
- [x] Multi-backend conformance is exercised by the `multi_backend` BDD feature and
      `test_multi_backend_bdd.py`

## Known Gaps

- **SQLite/MariaDB are conformance-only** — Postgres is the supported primary;
      dialect-specific features (e.g. `SET LOCAL` RLS) are stubbed on non-Postgres.
- **Migrations run against Postgres only** in CI (per ADR 002 conformance scope).

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — entry added to close the
  dangling `depends-on: feat-core-db-abstraction-core` edge in `teams/org-entity.md`.
  Behaviours re-verified against `db/repositories/*`, `db/session.py`, and the
  multi-backend unit/BDD suites. Status: covered.