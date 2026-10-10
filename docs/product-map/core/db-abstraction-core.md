---
id: feat-core-db-abstraction-core
prd: N/A
adr:
  - ADR 002 (database-abstraction-strategy)
code:
  - backend/src/modulo/db/session.py
  - backend/src/modulo/db/rls.py
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

- [x] Repository base (`BaseRepository`) provides the shared `paginate` /
      `execute` helpers, the abstract `apply_tenant_filter` hook and the shared
      concrete `set_org_context` helper used by the generic and Postgres
      repositories
- [x] `GenericRepository` works across backends (explicit `WHERE organisation_id`
      via `apply_tenant_filter`); `PostgresRepository` overrides only
      `apply_tenant_filter` (returns the statement unchanged — RLS does the
      filtering), while the shared `BaseRepository.set_org_context` →
      `db/rls.set_rls_org` writes `set_config('app.organisation_id', …, true)` on
      Postgres for transaction-local tenant isolation
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
      dialect-specific features (e.g. `SET LOCAL` RLS) are not available on
      non-Postgres, which instead records the org in `session.info` and relies on
      the `do_orm_execute` listener to inject `WHERE organisation_id = :oid`
      (`db/rls.py`).
- **Migrations run against Postgres only** in CI (per ADR 002 conformance scope).

## QA History

- 2026-10-10: **qa-iterate product-map pass** — corrected drifted claims. Added
  `db/rls.py` (the real `set_rls_org` / tenant-filter listener implementation) to
  `code:` — the entry previously attributed the org-context write to
  `PostgresRepository`, which only overrides `apply_tenant_filter`. Fixed the
  non-existent `RepositoryBase` name to `BaseRepository` and its described role.
  Corrected the Known Gaps wording (non-Postgres is not "stubbed" — it uses
  `session.info` + the `do_orm_execute` WHERE injection). Status: covered.

- 2026-08-25: **product-map review pass** — entry added to close the
  dangling `depends-on: feat-core-db-abstraction-core` edge in `teams/org-entity.md`.
  Behaviours re-verified against `db/repositories/*`, `db/session.py`, and the
  multi-backend unit/BDD suites. Status: covered.
