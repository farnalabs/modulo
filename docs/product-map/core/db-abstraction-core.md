---
id: feat-core-db-abstraction-core
prd: 6.1, 6.2
delivery-tasks: []
code:
  - backend/src/modulo/db/models/base.py
  - backend/src/modulo/db/session.py
  - backend/src/modulo/db/repositories/__init__.py
  - backend/src/modulo/db/repositories/base.py
  - backend/src/modulo/db/repositories/locks.py
  - backend/src/modulo/db/repositories/postgres.py
  - backend/src/modulo/db/repositories/generic.py
  - backend/src/modulo/db/rls.py
  - backend/src/modulo/db/crud/base.py
  - backend/src/modulo/db/crud/pagination.py
  - backend/src/modulo/db/crud/*.py (41 modules)
  - backend/src/modulo/db/models/*.py (57 model files)
  - backend/src/modulo/db/migrations/env.py
  - backend/src/modulo/db/migrations/versions/*.py (69 migrations)
  - backend/alembic.ini
  - backend/src/modulo/api/dependencies.py
  - backend/src/modulo/settings.py
bdd: []
unit-tests:
  - backend/tests/unit/db/test_repositories_base.py
  - backend/tests/unit/db/test_repositories_generic.py
  - backend/tests/unit/db/test_repositories_locks.py
  - backend/tests/unit/db/test_rls.py
  - backend/tests/unit/db/test_rls_multibackend.py
  - backend/tests/unit/db/test_migration_0025.py
  - backend/tests/unit/db/test_migration_0026.py
  - backend/tests/unit/db/test_migration_0049.py
  - backend/tests/unit/test_engine_pool_config.py
  - backend/tests/integration/conftest.py
  - backend/tests/integration/crud/conftest.py
depends-on: []
status: partial
---

# Core — Database Abstraction Layer

The cross-cutting foundation that all DB-backed features depend on. Covers declarative model system, session management, multi-backend repository architecture, row-level security, migration framework, CRUD layer, and test infrastructure.

PRD §6.1 (Layered Architecture), §6.2 (SaaS-First Multi-Tenant).

## Behaviours

### Declarative Model System

- [x] `Base(DeclarativeBase)` serves as the common declarative base for all 57 ORM models
- [x] `TimestampMixin` provides `created_at` and `updated_at` with `server_default` and `onupdate`
- [x] `OrgScoped` abstract mixin adds UUID PK (`id`) and `organisation_id` FK to `organisations.id` with CASCADE delete and index
- [x] All org-scoped models inherit `OrgScoped` — UUID PK convention established
- [x] JSON columns used for flexible schema (settings_json, preferences, etc.)
- [x] Check constraints used where appropriate (org_membership.role, runs.status)
- [x] Relationship convention: FK column + ORM relationship() on child model
- [x] 57 model files cover the full domain (account, agent, pipeline, run, schema, eval, notification, etc.)

### Session Management

- [x] `_build_engine()` in `session.py` creates a single `AsyncEngine` at module load with pooling config
- [x] `get_or_create_engine()` in `dependencies.py` duplicates the pool config via `create_async_engine()` — singleton per process, same values
- [x] Pooling config (both paths): `pool_pre_ping=True`, `pool_size=20`, `max_overflow=10`, `pool_recycle=3600`, `pool_timeout=30` for non-SQLite backends
- [x] `dependencies.py` also passes `connect_args={"timeout": 10}` to `create_async_engine` — `session.py` does not
- [x] Pooling config skipped for SQLite (pysqlite doesn't support pooling)
- [x] `AsyncSessionLocal` factory uses `expire_on_commit=False`, `autocommit=False`, `autoflush=False`
- [x] `get_session()` context manager commits on success, rolls back on `Exception` — does NOT catch `ProgrammingError` → 501
- [x] FastAPI dependency injection: `get_db_session()` in `api/dependencies.py` with `get_or_create_engine()` singleton — DOES catch `ProgrammingError` → 501
- [x] MCP sub-app uses `get_or_create_engine()` directly (non-Depends path) to share the same pool
- [x] RLS reset hook registered on engine creation for Postgres backends
- [x] Append-only guard registered for Postgres backends
- [x] ORM tenant filter registered for all backends

### Multi-Backend Repository Architecture

- [x] `BaseRepository(ABC)` defines abstract interface: `set_org_context`, `apply_tenant_filter`, `paginate`, `execute`
- [x] `PostgresRepository` extends `BaseRepository` — `apply_tenant_filter` returns stmt unchanged (RLS handles it)
- [x] `GenericRepository` extends `BaseRepository` — `apply_tenant_filter` injects `WHERE organisation_id = :org_id` for all entities with that column
- [x] `RepositoryHub` dispatches to backend-specific repo: `PostgresRepository` for `postgres`, `GenericRepository` fallback
- [x] `RepositoryHub` also exposes `BaseLockService` via `.locks` property
- [x] `_build_repository()` in `__init__.py` uses `match/case` dispatch on `db_type`
- [x] Lock abstraction: `BaseLockService(ABC)` with `acquire_lock`/`release_lock`
- [x] `PostgresLock` uses `pg_try_advisory_lock`/`pg_advisory_unlock` with polling and configurable timeout
- [x] `GenericLock` uses `asyncio.Lock` per key with ownership tracking (task ID) — prevents cross-task release
- [x] `_build_lock_service()` matches on `db_type`, defaults to `GenericLock`

### Row-Level Security

- [x] `set_rls_org()` requires active transaction — raises `RuntimeError` if not inside `session.begin()`
- [x] Postgres path: `SELECT set_config('app.organisation_id', :oid, true)` (local scope, reverted on transaction end)
- [x] Generic backend path: stores `org_id` in `session.info` for `do_orm_execute` listener
- [x] `set_rls_user_context()` sets `app.user_id` and `app.org_role` via `set_config` on Postgres
- [x] `set_rls_user_context()` stores `user_id` and `org_role` in `session.info` on generic backends
- [x] `set_rls_org_for_admin()` is a thin wrapper around `set_rls_org()` for system-admin scoped operations
- [x] Pool-level RLS reset hook (`register_rls_reset_hook`) clears `app.organisation_id`, `app.user_id`, `app.org_role` on every connection checkout (Postgres only)
- [x] RLS reset hook wraps execution in try/except — logs warning on failure, never crashes the connection checkout
- [x] RLS reset hook is a no-op on non-Postgres backends
- [x] ORM tenant filter (`_inject_tenant_filter`) injects `WHERE organisation_id = :oid` into SELECT, UPDATE, DELETE on generic backends
- [x] ORM tenant filter only activates when `MODULO_DB != postgres`
- [x] ORM tenant filter skips statements whose entity lacks `organisation_id` column
- [x] ORM tenant filter handles both `column_descriptions` (SELECT) and `all_mapper_classes` (UPDATE/DELETE) paths
- [x] Semgrep rule enforces `is_local=true` in RLS set_config — bare `SET LOCAL` without `is_local` is flagged

### Migration Framework

- [x] Alembic config at `backend/alembic.ini`
- [x] `env.py` converts async URLs to sync (`+asyncpg` → `+psycopg2`, `+asyncmy` → `+pymysql`)
- [x] `env.py` supports `DATABASE_URL` env var override
- [x] Batch mode (`render_as_batch=True`) enabled for SQLite migrations
- [x] 69 migrations from 0001 (initial schema) through 0059 (feedback annotation)
- [x] RLS policies migration (`0002_rls_policies`) conditional — enables RLS on org-scoped tables
- [x] Migration coordination: advisory locks prevent concurrent migrations
- [x] Offline mode support for generating SQL scripts

### CRUD Layer

- [x] 41 function-based CRUD modules covering all entities (account, pipeline, run, schema, eval, org, team, etc.)
- [x] `apply_updates()` utility prevents overwriting immutable fields (id, organisation_id, timestamps)
- [x] `PageResult[T]` dataclass with `items`, `total`, `page`, `page_size`, `next_cursor`, `has_more`
- [x] `CursorPaginator` keyset pagination with base64 cursor encoding/decoding
- [x] `CursorPaginator.paginate()` supports configurable sort field, sort direction, cursor, limit, and optional total count
- [x] `CursorPaginator.encode_cursor()` handles `datetime` serialization (isoformat)
- [x] `CursorPage` Pydantic model for API response serialization

### Test Infrastructure

- [x] `tests/integration/conftest.py` — Testcontainers Postgres 16 Alpine with Alembic migrations
- [x] Integration conftest runs full migration chain (`command.upgrade(config, "heads")`)
- [x] Integration conftest patches missing ORM columns (pipelines.default_autonomy_level, webhook_payloads raw_body/raw_payload)
- [x] Integration conftest applies `FORCE ROW LEVEL SECURITY` on all org-scoped tables
- [x] `tests/integration/crud/conftest.py` — org/user fixtures with RLS session
- [x] `test_repositories_base.py` — mocked tests for `BaseRepository.paginate` (edge cases: page=0, negative page, page_size=0) and `extract_orm_entity`
- [x] `test_repositories_generic.py` — tests for `GenericRepository.apply_tenant_filter`
- [x] `test_repositories_locks.py` — tests for `PostgresLock` / `GenericLock` acquire/release
- [x] `test_rls.py` — RLS enforcement tests
- [x] `test_rls_multibackend.py` — RLS tests across multiple backends
- [x] `test_engine_pool_config.py` — verifies pool settings for postgres vs sqlite

## Error Handling

### ProgrammingError → 501 Not Implemented Pattern
- [x] `get_db_session()` in `api/dependencies.py` catches `ProgrammingError` and raises `HTTPException(501)` — this is the FastAPI DI path used by route handlers
- [x] No global middleware or base class enforces this for ALL DB-backed routes — each route must use `Depends(get_db_session)` to benefit
- [ ] `get_session()` in `session.py` rolls back on any `Exception` and re-raises — no specific handling for `ProgrammingError`. Routes using `get_session()` directly (not via FastAPI DI) lack the 501 conversion
- [ ] **GAP**: No `SQLAlchemyError` → 503 catch in `get_db_session()` or `get_session()` — connection/deadlock failures propagate as 500

### Connection Pool Exhaustion
- [x] Pool is configured with `pool_pre_ping=True`, `pool_size=20`, `max_overflow=10`, `pool_recycle=3600`, `pool_timeout=30`
- [x] `dependencies.py` also sets `connect_args={"timeout": 10}` for connect-level timeout
- [ ] **GAP**: Pool settings are hardcoded — not configurable at runtime via env var
- [ ] **GAP**: No health check endpoint verifies database connectivity / pool health
- [ ] **GAP**: No circuit-breaker or backoff when pool is exhausted — SQLAlchemy raises `TimeoutError` which propagates as 500

### RLS Context Isolation Failure
- [x] Pool-level reset hook clears org context on checkout (Postgres) — defense-in-depth
- [x] `set_rls_org` requires active transaction — RuntimeError prevents accidental calls outside BEGIN
- [x] `set_rls_org` with `None` org_id is a no-op (system admin path)
- [ ] **GAP**: No formal cross-tenant data leak audit — no automated verification that ALL org-scoped routes enforce org isolation

### Migration Failures
- [ ] **GAP**: No unified migration rollback test suite — individual migrations have ad-hoc tests (0025, 0026, 0049)
- [ ] **GAP**: Conditional DDL in Alembic migrations (Phase 4 of ADR 002) is not implemented — PG-only migrations fail on SQLite/MariaDB
- [x] Integration conftest runs the full migration chain on Testcontainers — catches migration errors at test time

### Engine Misconfiguration
- [x] `MODULO_DB` validates accepted values: postgres, sqlite, mariadb, mysql (via Pydantic validator)
- [ ] **GAP**: `RepositoryHub._build_repository` falls back silently to `GenericRepository` for unknown backends — no explicit error
- [ ] **GAP**: No formal multi-backend CI matrix — only Postgres tested in CI

## Known Gaps

- **No generic CRUD base class** — each entity has its own function-based module (41 modules). Inconsistent patterns exist across modules.
- **No standard soft-delete mixin** — soft-delete is ad-hoc per model (e.g. pipelines, schemas) rather than a reusable mixin on TimestampMixin.
- **No dedicated BDD feature file** for the abstraction layer itself — it's exercised indirectly by all feature BDD tests.
- **RLS bypass audit** — no formal verification that ALL org-scoped routes enforce org isolation. No automated cross-tenant data leak detection.
- **No integration test for GenericRepository** with non-Postgres backends (SQLite, MariaDB). Only Postgres tested in CI.
- **No formal multi-backend CI matrix** — only Postgres tested in CI via Testcontainers.
- **RepositoryHub no explicit error** for unsupported `db_type` — falls back silently to `GenericRepository` with a warning log.
- **No unified migration rollback test suite** — individual migrations have ad-hoc tests but there's no end-to-end `upgrade → downgrade → upgrade` test for all 69 migrations.
- **CursorPaginator not used by all list endpoints** — inconsistent adoption across the codebase. Many endpoints still use LIMIT/OFFSET pagination via `BaseRepository.paginate()`.
- **No health check** for database connectivity — no `/healthz/db` endpoint.
- **Connection pooling not configurable at runtime** — `pool_size=20`, `max_overflow=10`, `pool_recycle=3600`, `pool_timeout=30` are hardcoded in both `session.py` and `dependencies.py`.
- **`RepositoryHub` not wired into route/service code** — routes still call `set_rls_org` directly (noted in db-abstraction-remaining).
- **ModuloPostgresSaver is PG-only** — no SQLite checkpoint saver for dev mode (noted in db-abstraction-remaining).
- **Rate limiter disables on SQLite** — no functional in-memory fallback (noted in db-abstraction-remaining).
- **Duplicate engine creation path** — `session.py:_build_engine()` and `dependencies.py:get_or_create_engine()` both define pool settings independently. A change to pool config must be made in two places. There is no single source of truth for engine configuration.
- **`connect_args` inconsistency** — `dependencies.py` passes `connect_args={"timeout": 10}` to `create_async_engine` but `session.py` does not. This means the module-load engine (used by background tasks) lacks connect-level timeout protection.
- **`get_session()` lacks ProgrammingError/SQLAlchemyError handling** — only `get_db_session()` (FastAPI DI) catches `ProgrammingError` → 501. Code paths using `get_session()` directly leak raw 500s on migration gaps and DB failures.

## QA History

### 2026-07-06 — Cross-cutting QA (improve-architecture index 234)
- Fixed stale pool config claim: product map claimed `pool_size=10, max_overflow=5, pool_timeout=10` but actual code uses `pool_size=20, max_overflow=10, pool_recycle=3600, pool_timeout=30` in both `session.py` and `dependencies.py`
- Fixed `test_engine_pool_config.py` — assertions asserted wrong pool values (10/5/10) vs actual code (20/10/3600/30); test would have been failing
- Added missing `pool_recycle=3600` behaviour checkbox
- Documented two separate engine creation paths (`session.py:_build_engine()` vs `dependencies.py:get_or_create_engine()`) as a known gap
- Documented `connect_args={"timeout": 10}` inconsistency between the two paths
- Updated Error Handling to distinguish `get_session()` (no ProgrammingError catch) from `get_db_session()` (FastAPI DI, catches ProgrammingError → 501)
- Added 3 new Known Gaps: duplicate engine creation path, connect_args inconsistency, get_session() lacks ProgrammingError/SQLAlchemyError handling
- Identified `GenericRepository.apply_tenant_filter()` only checks `column_descriptions` for SELECT, not `all_mapper_classes` for UPDATE/DELETE (the ORM listener handles it, but the repository method is inconsistent)
- 6 relevant tests pass

### 2026-07-03 — Cross-cutting QA (improve-architecture index 90)
- Enriched product map entry from stub to comprehensive coverage
- Verified all declared behaviours against actual code files
- Added Error Handling section with verified catches and identified gaps
- Identified new gaps: no global ProgrammingError middleware, no DB health check, hardcoded pool config, no multi-backend CI matrix, no unified migration rollback test suite
- Verified 57 model files, 41 CRUD modules, 69 migrations, 4 repository implementations
- Status changed from `gap` to `partial`
