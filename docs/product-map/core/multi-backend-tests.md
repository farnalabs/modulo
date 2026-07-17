---
id: feat-core-multi-backend-tests
prd: 6.2, 12
bdd:
  - backend/tests/bdd/features/organisation/multi_backend.feature
code:
  - backend/tests/unit/test_multi_backend_config.py
  - backend/tests/unit/test_multi_backend_sqlite.py
  - backend/tests/unit/db/test_rls_multibackend.py
  - backend/tests/unit/db/test_rls.py
  - backend/tests/unit/db/test_repositories_base.py
  - backend/tests/unit/db/test_repositories_generic.py
  - backend/tests/unit/db/test_repositories_locks.py
  - backend/tests/unit/db/test_multi_backend_bdd.py
  - backend/tests/bdd/steps/test_multi_backend.py
  - backend/tests/integration/test_rls_isolation.py
  - backend/tests/integration/test_cross_tenant_isolation.py
unit-tests:
  - backend/tests/unit/test_multi_backend_config.py
  - backend/tests/unit/test_multi_backend_sqlite.py
  - backend/tests/unit/db/test_rls_multibackend.py
  - backend/tests/unit/db/test_rls.py
  - backend/tests/unit/db/test_repositories_base.py
  - backend/tests/unit/db/test_repositories_generic.py
  - backend/tests/unit/db/test_repositories_locks.py

delivery-tasks: []
depends-on: [feat-core-db-abstraction-core]
status: partial
---

# Multi-backend database test suite

Tests that verify Modulo's database abstraction layer works across all three supported backends: PostgreSQL, SQLite, and MariaDB/MySQL. Includes settings config, RLS dispatch, RepositoryHub, generic lock services, and integration tests for tenant isolation.

## Behaviours

- [x] Settings model parses `MODULO_DB` for postgres, sqlite, mariadb
- [x] Settings model normalises case (POSTGRES, SQLite, MariaDB)
- [x] Settings model raises on invalid backend string
- [x] Settings model rewrites DATABASE_URL when backend is sqlite or mariadb
- [x] SQLite engine can create all tables and perform CRUD
- [x] SQLite engine supports tenant filtering via session.info
- [x] SQLite engine handles Organisation and User models
- [x] `set_rls_org` dispatches to `set_config` on postgres dialect
- [x] `set_rls_org` stores in session.info for sqlite dialect
- [x] `set_rls_org` stores in session.info for mysql dialect
- [x] `set_rls_org` raises RuntimeError without active transaction
- [x] `set_rls_org` normalises postgresql dialect to postgres
- [x] `set_rls_user_context` sets user_id and org_role on postgres
- [x] `set_rls_user_context` raises without active transaction
- [x] `_inject_tenant_filter` adds WHERE clause for single entity select
- [x] `_inject_tenant_filter` skips when no org_id in session.info
- [x] `_inject_tenant_filter` skips non-SELECT/UPDATE/DELETE statements
- [x] `_inject_tenant_filter` skips entities without organisation_id column
- [x] `_inject_tenant_filter` skips None and object entities
- [x] `_inject_tenant_filter` adds WHERE for all org entities in JOINs
- [x] `_inject_tenant_filter` uses all_mapper_classes for UPDATE/DELETE
- [x] `_inject_tenant_filter` skips DML when mapper has no org column
- [x] `extract_orm_entity` returns entity from column descriptions
- [x] `extract_orm_entity` returns None when no entity found
- [x] `extract_orm_entity` skips None entities
- [x] `GenericRepository.set_org_context` delegates to set_rls_org
- [x] `GenericRepository.apply_tenant_filter` adds WHERE clause
- [x] `GenericRepository.apply_tenant_filter` handles JOINs
- [x] `GenericRepository.apply_tenant_filter` skips entities without org column
- [x] `GenericRepository.apply_tenant_filter` skips None/object entities
- [x] `GenericRepository.apply_tenant_filter` returns stmt unchanged on no match
- [x] `PostgresRepository.apply_tenant_filter` returns stmt unchanged
- [x] `BaseRepository.paginate` returns PageResult with offset/limit
- [x] `BaseRepository.paginate` raises on page < 1
- [x] `BaseRepository.paginate` raises on page_size < 1
- [x] `BaseRepository.execute` proxies to session.execute
- [x] Lock service factory returns PostgresLock for postgres
- [x] Lock service factory returns GenericLock for sqlite
- [x] Lock service factory returns GenericLock for mariadb
- [x] Lock service factory returns GenericLock for unknown backends
- [x] `PostgresLock.acquire_lock` calls pg_try_advisory_lock
- [x] `PostgresLock.release_lock` calls pg_advisory_unlock
- [x] `PostgresLock.acquire_lock` raises LockAcquireError on timeout
- [x] `PostgresLock.acquire_lock` retries on contention
- [x] `PostgresLock` uses consistent key hash between acquire and release
- [x] `GenericLock.acquire_lock` acquires asyncio.Lock
- [x] `GenericLock.release_lock` releases asyncio.Lock
- [x] `GenericLock` release of non-existent lock is no-op
- [x] `GenericLock` acquire with timeout succeeds
- [x] `GenericLock` acquire raises LockAcquireError when lock held and timeout
- [x] `GenericLock` ownership prevents cross-task release
- [x] `GenericLock` multiple keys are independent
- [x] `GenericLock` acquire twice same key blocks
- [x] `register_rls_reset_hook` registers checkout listener for postgres
- [x] RLS set_config is scoped to transaction and resets after commit
- [x] RLS set_config is scoped to transaction and resets after rollback
- [x] Second transaction on pooled connection does not inherit org_id
- [x] RLS policy exists on all org-scoped tables
- [x] RLS actually filters rows for non-superuser roles
- [x] RepositoryHub construction/dispatch unit test
- [x] `register_tenant_filter()` registration behaviour test (skips for postgres, registers for others)
- [x] `register_rls_reset_hook` skip behaviour for non-postgres backends
- [ ] `_build_engine()` test for sqlite and mariadb backend config
- [ ] MariaDB live integration test (docker-compose.mariadb.yml in CI; MariaDB deprecated 2026-07-11 — not a priority)
- [ ] SQLite live integration test (full CRUD suite against SQLite, not just smoke test)
- [x] BDD feature file for multi-backend behaviour
- [ ] Alembic migration conditional DDL for non-Postgres backends
- [ ] API behaviour difference tests for sqlite/mariadb (pool config, rate limiter)
- [ ] Cross-tenant isolation integration tests for GenericRepository (WHERE-clause filtering)

## Error Handling

- [x] Test suite covers `set_rls_org` without active transaction — raises `RuntimeError`
- [x] Test suite covers `GenericRepository.apply_tenant_filter` with None/object entities — skips gracefully
- [x] Test suite covers `extract_orm_entity` with no entity found — returns None
- [x] Test suite covers `BaseRepository.paginate` with invalid page/page_size — raises error
- [x] Test suite covers `GenericLock` release of non-existent lock — no-op
- [x] Test suite covers `GenericLock` acquire with timeout when lock held — raises `LockAcquireError`
- [x] Test suite covers `GenericLock` cross-task release prevention — ownership check
- [x] Test suite covers `PostgresLock.acquire_lock` timeout on contention — raises `LockAcquireError`
- [ ] No test for controller-level error propagation across backend types
- [ ] No test for migration failure on non-Postgres backends

## Edge Cases

- [x] Test suite covers empty tenant filter (no org_id in session.info) — skips
- [x] Test suite covers non-SELECT/UPDATE/DELETE statements in tenant filter — skips
- [x] Test suite covers entities without `organisation_id` column — skips
- [x] Test suite covers JOIN queries with mixed org-scoped and unscoped entities
- [x] Test suite covers RLS isolation after transaction commit/rollback on pooled connections
- [ ] No test for MariaDB backend behaviour (deprecated — deferred)
- [ ] No test for concurrent lock acquisition across multiple processes

## Security

- [x] RLS isolation integration tests verify cross-tenant data leak prevention
- [x] RLS set_config isolation scoped to transaction — verified after commit and rollback
- [x] Second transaction on pooled connection does not inherit previous org_id
- [ ] No test for RLS bypass via direct DB connection
- [ ] No test for session.info leakage across concurrent requests

## Known Gaps
- 2026-07-06: improve-architecture (index 226) — Fixed product map `bdd:` frontmatter (was `[]`, now points to `multi_backend.feature`). Added `RepositoryHub` construction/dispatch unit tests (3 repo types × 3 lock types = 6 tests). Added `register_tenant_filter()` registration behaviour test (skips postgres, registers for sqlite/mariadb). Added `register_rls_reset_hook` skip behaviour test (skips sqlite/mysql, registers for postgres). Fixed `test_backend_type_hint_in_repository_hub` — removed `or True` that made it always pass. Removed stale Known Gap about missing BDD feature file. Status: partial (6 known gaps remain — `_build_engine()` test, MariaDB CI, SQLite CRUD suite, Alembic conditional DDL, API difference tests, GenericRepository integration test).
- 2026-07-12: improve-architecture (r2) — Fixed `prd:` frontmatter (was `12`, no longer exists; now `6.2` for multi-tenant architecture). Added MariaDB deprecation notes to Known Gaps and unchecked behaviours. Fixed N806 naming violations (`_USER_ID` → `user_id`, `_ORG_ROLE` → `org_role`) in test_rls_multibackend.py. Added `except asyncio.CancelledError: raise` guard before `except Exception` in rls.py `_reset_org_on_checkout` (per project convention).
