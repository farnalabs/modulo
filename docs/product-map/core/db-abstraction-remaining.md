---
id: feat-core-db-abstraction-remaining
prd: 8.17
delivery-tasks: [task-nv12-db-abstraction-remaining]
bdd:
  - backend/tests/bdd/features/security/rls_enforcement.feature
  - backend/tests/bdd/features/auth/tenant_isolation.feature
code:
  - backend/src/modulo/db/repositories/
  - backend/src/modulo/db/rls.py
  - backend/src/modulo/db/session.py
depends-on: [feat-core-db-abstraction-core]
status: partial
---
# Core — Database Abstraction (remaining work)

Multi-backend database abstraction. Phase 1 (UUID swap, Repository ABC + Hub) is done. This entry tracks Phases 2–5 from ADR 002 and other remaining gaps.

## Behaviours

### Repository Layer - [ ] `BaseRepository.set_org_context` dispatches to `set_rls_org` for both Postgres and generic backends
- [ ] `BaseRepository.apply_tenant_filter` returns stmt unchanged on Postgres (RLS handles scoping)
- [ ] `BaseRepository.apply_tenant_filter` injects `WHERE organisation_id = :org_id` on generic backends
- [ ] `BaseRepository.paginate` applies LIMIT/OFFSET and returns `PageResult` with total count
- [ ] `RepositoryHub` selects `PostgresRepository` when `MODULO_DB=postgres`
- [ ] `RepositoryHub` selects `GenericRepository` for all other backends (sqlite, mariadb, mysql)
- [ ] `RepositoryHub.repo` exposes the selected repository implementation
- [ ] `RepositoryHub.locks` exposes the selected lock service
- [ ] `PostgresLock.acquire_lock` uses `pg_try_advisory_lock` with polling and timeout
- [ ] `PostgresLock.release_lock` calls `pg_advisory_unlock`
- [ ] `GenericLock.acquire_lock` uses `asyncio.Lock` per key with timeout support
- [ ] `GenericLock.release_lock` only releases if called by the owning task ### RLS / Tenant Isolation - [ ] `set_rls_org` requires active transaction — raises `RuntimeError` if not inside `session.begin()`
- [ ] `set_rls_org` calls `SELECT set_config('app.organisation_id', :oid, true)` on Postgres
- [ ] `set_rls_org` stores org_id in `session.info` on generic backends
- [ ] `set_rls_user_context` sets `app.user_id` and `app.org_role` via set_config on Postgres
- [ ] `set_rls_user_context` stores user_id and org_role in `session.info` on generic backends
- [ ] Pool-level RLS reset hook clears `app.organisation_id` on connection checkout (Postgres only)
- [ ] RLS reset hook is a no-op on non-Postgres backends
- [ ] `_inject_tenant_filter` ORM listener injects `WHERE organisation_id = :oid` into every SELECT, UPDATE, DELETE on generic backends
- [ ] ORM listener only activates when `MODULO_DB != postgres`
- [ ] ORM listener skips statements whose entity lacks `organisation_id` column ### Session / Engine - [ ] Engine configures pool_pre_ping, pool_size, max_overflow for non-SQLite backends
- [ ] RLS reset hook registered only when `MODULO_DB=postgres`
- [ ] Append-only guard registered only when `MODULO_DB=postgres`
- [ ] ORM tenant filter registered for all backends (internally checks db_type)
- [ ] `get_session()` context manager commits on success, rolls back on error ### LangGraph Checkpoint Saver - [ ] `ModuloPostgresSaver` adds `organisation_id` to all checkpoint tables
- [ ] `agettuple` filters by `organisation_id` — cross-org checkpoints invisible
- [ ] `alist` filters by `organisation_id`
- [ ] `aput` writes checkpoint with `organisation_id`
- [ ] `aput_writes` writes pending writes with `organisation_id`
- [ ] Checkpoint JSON encrypted at rest via Fernet when key is provided
- [ ] Sync methods (`get_tuple`, `put`, etc.) raise `RuntimeError` if called from async context
- [ ] SQLite equivalent of checkpoint saver exists for dev mode ### Migrations - [ ] `env.py` detects backend from `DATABASE_URL` scheme
- [ ] Alembic uses batch mode (`render_as_batch`) for SQLite
- [ ] RLS policy migration (`0002_rls_policies`) is conditional — skipped on non-Postgres
- [ ] Team visibility RLS migration (`0025_team_visibility_rls`) is conditional — skipped on non-Postgres
- [ ] LangGraph checkpointer `_init_checkpointer` handles SQLite mode gracefully ### Rate Limiter - [ ] Rate limiter disables Redis connection when `MODULO_DB=sqlite`
- [ ] In-memory rate limiting fallback works when Redis is unavailable ### BDD / Tests - [ ] Tenant isolation scenarios cover cross-org pipeline visibility, raw query RLS enforcement, and cross-org run forbidden
- [ ] `rls_enforcement.feature` has real scenarios (currently a placeholder)
- [ ] Tests exist for `GenericRepository` tenant filtering
- [ ] Tests exist for `GenericLock` acquire/release
- [ ] Tests exist for `RepositoryHub` dispatch logic
- [ ] Tests exist for `_inject_tenant_filter` ORM listener
- [ ] Tests exist for `set_rls_org` on SQLite backend
- [ ] Tests exist for `set_rls_user_context` on generic backend ### Integration / Wiring - [ ] `RepositoryHub` is wired into route/service code (currently routes call `set_rls_org` directly)
- [ ] `apply_tenant_filter` is used in CRUD operations that do custom queries
- [ ] `connector_hub.locking` uses the lock abstraction from `RepositoryHub`
- [ ] Migration coordination advisory locks use the lock abstraction
- [ ] MariaDB async driver works via `docker-compose.mariadb.yml`
- [ ] `MODULO_DB` setting validates accepted values: postgres, sqlite, mariadb, mysql ## Known Gaps - Phase 4 from ADR 002 (conditional DDL in Alembic migrations) is not implemented — PG-only migrations will fail on SQLite/MariaDB
- Phase 5 (advisory lock abstraction fully wired) is incomplete — `connector_hub.locking` has its own locking
- `ModuloPostgresSaver` is PG-only; no `ModuloSqliteSaver` exists for dev mode
- `RepositoryHub` is not wired into route/service code — routes still call `set_rls_org` directly
- `apply_tenant_filter` is defined but never called outside repository files
- `rls_enforcement.feature` is a BDD placeholder (TODO) — no scenarios
- No unit tests for `GenericRepository`, `GenericLock`, `RepositoryHub`, `_inject_tenant_filter`, or `set_rls_org` on SQLite
- Rate limiter has no functional in-memory fallback — just disables on SQLite
- ORM listener (`_inject_tenant_filter`) may not handle all query shapes (subqueries, CTEs, inheritance) — undocumented 