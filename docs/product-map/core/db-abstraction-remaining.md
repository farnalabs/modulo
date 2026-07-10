---
id: feat-core-db-abstraction-remaining
prd: 6.1, 6.2
adr: [docs/adr/002-database-abstraction-strategy.md]
delivery-tasks: [task-nv12-db-abstraction-remaining]
bdd:
  - backend/tests/bdd/features/security/rls_enforcement.feature
  - backend/tests/bdd/features/auth/tenant_isolation.feature
code:
  - backend/src/modulo/db/repositories/
  - backend/src/modulo/db/rls.py
  - backend/src/modulo/db/session.py
depends-on: [feat-core-db-abstraction-core]
unit-tests:
  - backend/tests/unit/db/test_rls_multibackend.py
  - backend/tests/unit/db/test_repositories_generic.py
  - backend/tests/unit/db/test_repositories_locks.py
  - backend/tests/integration/test_rls_isolation.py
  - backend/tests/unit/db/test_multi_backend_bdd.py
status: partial
---

# Core — Database Abstraction (remaining work)

Multi-backend database abstraction. Phase 1 (UUID swap, Repository ABC + Hub) is done. This entry tracks Phases 2–5 from ADR 002 and other remaining gaps.

## Behaviours

### Repository Layer

- [x] `BaseRepository.set_org_context` dispatches to `set_rls_org` for both Postgres and generic backends — `base.py:49-56`
- [x] `BaseRepository.apply_tenant_filter` returns stmt unchanged on Postgres (RLS handles scoping) — `postgres.py:20`
- [x] `BaseRepository.apply_tenant_filter` injects `WHERE organisation_id = :org_id` on generic backends — `generic.py:26-36`
- [x] `BaseRepository.paginate` applies LIMIT/OFFSET and returns `PageResult` with total count — `base.py:66-93`
- [x] `RepositoryHub` selects `PostgresRepository` when `MODULO_DB=postgres` — `__init__.py:31-33`
- [x] `RepositoryHub` selects `GenericRepository` for all other backends (sqlite, mariadb, mysql) — `__init__.py:34-36`
- [x] `RepositoryHub.repo` exposes the selected repository implementation — `__init__.py:59-60`
- [x] `RepositoryHub.locks` exposes the selected lock service — `__init__.py:63-64`
- [x] `PostgresLock.acquire_lock` uses `pg_try_advisory_lock` with polling and timeout — `locks.py:63-84`
- [x] `PostgresLock.release_lock` calls `pg_advisory_unlock` — `locks.py:86-96`
- [x] `GenericLock.acquire_lock` uses `asyncio.Lock` per key with timeout support — `locks.py:119-138`
- [x] `GenericLock.release_lock` only releases if called by the owning task — `locks.py:140-161`

### RLS / Tenant Isolation

- [x] `set_rls_org` requires active transaction — raises `RuntimeError` if not inside `session.begin()` — `rls.py:39-40`
- [x] `set_rls_org` calls `SELECT set_config('app.organisation_id', :oid, true)` on Postgres — `rls.py:70-73`
- [x] `set_rls_org` stores org_id in `session.info` on generic backends — `rls.py:75`
- [x] `set_rls_user_context` sets `app.user_id` and `app.org_role` via set_config on Postgres — `rls.py:88-95`
- [x] `set_rls_user_context` stores user_id and org_role in `session.info` on generic backends — `rls.py:97-99`
- [x] Pool-level RLS reset hook clears `app.organisation_id` on connection checkout (Postgres only) — `rls.py:112-152`
- [x] RLS reset hook is a no-op on non-Postgres backends — `rls.py:131-134`
- [x] `_inject_tenant_filter` ORM listener injects `WHERE organisation_id = :oid` into every SELECT, UPDATE, DELETE on generic backends — `rls.py:155-191`
- [x] ORM listener only activates when `MODULO_DB != postgres` — `register_tenant_filter` at `rls.py:207-208`
- [x] ORM listener skips statements whose entity lacks `organisation_id` column — `rls.py:178, 186`

### Session / Engine

- [x] Engine configures pool_pre_ping, pool_size, max_overflow for non-SQLite backends — `session.py:31-35`
- [x] RLS reset hook registered only when `MODULO_DB=postgres` — `session.py:39-40`
- [x] Append-only guard registered only when `MODULO_DB=postgres` — `session.py:41`
- [x] ORM tenant filter registered for all backends (internally checks db_type via `register_tenant_filter`) — `session.py:45`
- [x] `get_session()` context manager commits on success, rolls back on error — `session.py:60-68`

### LangGraph Checkpoint Saver

- [x] `ModuloPostgresSaver` adds `organisation_id` to all checkpoint tables — `modulo_saver.py:70-149`
- [x] `agettuple` filters by `organisation_id` — cross-org checkpoints invisible — `modulo_saver.py:287-294`
- [x] `alist` filters by `organisation_id` — `modulo_saver.py:350-355`
- [x] `aput` writes checkpoint with `organisation_id` — `modulo_saver.py:422`
- [x] `aput_writes` writes pending writes with `organisation_id` — `modulo_saver.py:462`
- [x] Checkpoint JSON encrypted at rest via Fernet when key is provided — `modulo_saver.py:190-193, 199-204`
- [x] Sync methods (`get_tuple`, `put`, etc.) raise `RuntimeError` if called from async context — `modulo_saver.py:555-557`
- [ ] SQLite equivalent of checkpoint saver (`ModuloSqliteSaver`) does not exist — dev mode uses Postgres checkpointer or no checkpoints

### Migrations

- [x] `env.py` detects backend from `DATABASE_URL` scheme — `env.py:48-55`
- [x] Alembic uses batch mode (`render_as_batch`) for SQLite — `env.py:69, 82`
- [ ] RLS policy migration (`0002_rls_policies`) is NOT conditional — `op.execute` calls will fail on non-Postgres (documented as conditional but not implemented)
- [ ] Team visibility RLS migration (`0025_team_visibility_rls`) is NOT conditional — `op.execute` calls will fail on non-Postgres
- [x] LangGraph checkpointer `${ModuloPostgresSaver.from_conn_string}` only called for Postgres in `executor.py` — SQLite mode skips checkpointing

### Rate Limiter

- [x] Rate limiter disables Redis connection when `MODULO_DB=sqlite` — `rate_limiter.py:35-38`
- [x] In-memory rate limiting fallback (`TokenBucket`) works when Redis is unavailable — `rate_limiter.py:30-49`, `RateLimiterRegistry` docstring

### BDD / Tests

- [x] Tenant isolation scenarios cover cross-org pipeline visibility, raw query RLS enforcement, and cross-org run forbidden — `tenant_isolation.feature` has 3 real scenarios with step definitions in `test_auth.py`
- [x] `rls_enforcement.feature` has 7 real scenarios with step definitions in `test_security.py` — **was stale placeholder claim**
- [x] Tests exist for `GenericRepository` tenant filtering — `test_repositories_generic.py` (6 test methods)
- [x] Tests exist for `GenericLock` acquire/release — `test_repositories_locks.py` (7 test methods for GenericLock)
- [x] Tests exist for `RepositoryHub` dispatch logic — `test_multi_backend_bdd.py:467-494` (6 test methods covering Postgres/SQLite/MariaDB repo and lock dispatch)
- [x] Tests exist for `_inject_tenant_filter` ORM listener — `test_rls_multibackend.py` (7 test methods in `TestInjectTenantFilter`)
- [x] Tests exist for `set_rls_org` on generic backends — `test_rls_multibackend.py` (`test_generic_stores_in_session_info`, `test_mariadb_stores_in_session_info`)
- [x] Tests exist for `set_rls_user_context` on generic backend — `test_rls_multibackend.py::TestSetRlsUserContextMultiBackend` covers SQLite, MariaDB, and Postgres paths

### Integration / Wiring

- [ ] `RepositoryHub` is wired into route/service code — routes still call `set_rls_org` directly
- [ ] `apply_tenant_filter` is used in CRUD operations that do custom queries — defined but never called outside repository files
- [ ] `connector_hub.locking` uses the lock abstraction from `RepositoryHub` — connector_hub has its own locking
- [ ] Migration coordination advisory locks use the lock abstraction — not connected
- [ ] MariaDB async driver works via `docker-compose.mariadb.yml` — not verified
- [ ] `MODULO_DB` setting validates accepted values: postgres, sqlite, mariadb, mysql — need to check

## Error Handling

- [x] `_ensure_active_transaction` raises `RuntimeError` when `set_rls_*` is called outside an active transaction — `rls.py:39-40`
- [x] `set_rls_org` with `org_id=None` returns early (system admin un-scoped path) — `rls.py:62-63`
- [x] Pool-level RLS reset hook catches all exceptions and logs warning — does not crash startup — `rls.py:148-152`
- [x] `_inject_tenant_filter` silently returns when no `org_id` in `session.info` (no-op) — `rls.py:162-164`
- [x] `_inject_tenant_filter` skips non-SELECT/UPDATE/DELETE operations (INSERT pass-through) — `rls.py:166-167`
- [x] `BaseRepository.paginate` raises `ValueError` on invalid `page` (<1) or `page_size` (<1 or >1000) — `base.py:79-84`
- [x] `GenericLock.acquire_lock` raises `LockAcquireError` on timeout — `locks.py:133-134`
- [x] `GenericLock.release_lock` warns on double-release and cross-task release (never crashes) — `locks.py:145, 151-152`
- [x] `PostgresLock.release_lock` warns when lock was not held by session — `locks.py:93-96`
- [x] `GenericRepository.apply_tenant_filter` raises `ValueError` on `None` org_id and `TypeError` on non-UUID — `generic.py:28-30`
- [x] `ModuloPostgresSaver` decrypt methods warn on decryption failure (`blob.decrypt_fallback`) — never crashes — `modulo_saver.py:247-248`
- [x] `ModuloPostgresSaver` sync methods raise `RuntimeError` if called from async context — `modulo_saver.py:555-557`
- [x] `register_rls_reset_hook` is a no-op on non-Postgres backends — `rls.py:131-134`
- [x] `register_tenant_filter` is a no-op on Postgres backends — `rls.py:207-208`

## Known Gaps

- Phase 4 from ADR 002 (conditional DDL in Alembic migrations) is not implemented — `0002_rls_policies` and `0025_team_visibility_rls` will fail on SQLite/MariaDB with non-conditional `op.execute()` calls
- Phase 5 (advisory lock abstraction fully wired) is incomplete — `connector_hub.locking` has its own locking
- `ModuloPostgresSaver` is PG-only; no `ModuloSqliteSaver` exists for dev mode
- `RepositoryHub` is not wired into route/service code — routes still call `set_rls_org` directly
- `apply_tenant_filter` is defined but never called outside repository files
- No unit test for `set_rls_user_context` on generic backend (session.info path)
- ORM listener (`_inject_tenant_filter`) may not handle all query shapes (subqueries, CTEs, inheritance) — undocumented
- ~~`MODULO_DB` setting does not validate accepted values — any string accepted~~ **(RESOLVED — `@field_validator("modulo_db")` at `settings.py:174-179` rejects invalid values)**
- MariaDB async driver has no Docker Compose profile or integration test
- Migration coordination advisory locks do not use the repository lock abstraction
- **No RLS DML enforcement integration test for Postgres**: only SELECT filtering is tested; INSERT/UPDATE/DELETE under `SET ROLE` + `SET LOCAL` are untested
- **Mock-chain assertions instead of SQL predicate verification**: All `apply_tenant_filter` unit tests verify `.where.assert_called_once()` but never the actual SQL expression — a wrong predicate passes all tests
- **`test_multi_backend_bdd.py` naming is misleading**: Not a BDD file
- `rls.py` conflates three distinct concerns (transaction-scoped helpers, pool-level reset hook, ORM tenant listener) in one module
- No concurrent-GenericLock-key-creation test — `_generic_dict_lock` race guard is untested
- No test for `RepositoryHub(db_type=None)` — settings-read code path is untested
- SQLAlchemy default `autobegin=True` allowed stale `session.info[_TENANT_KEY]` on rollback (now fixed with `autobegin=False`)

## QA History

- 2026-07-04: Cross-cutting QA (index 123). Marked ~45`[ ]`→`[x]` behaviours across all sections (Repository Layer, RLS/Tenant Isolation, Session/Engine, LangGraph Checkpointer, Migrations, Rate Limiter, BDD/Tests). Added unit-tests frontmatter (6 test file refs — was empty `[]`). Added Error Handling section (14 behaviour checkboxes covering all guard/error paths). Resolved 4 stale known gaps: (1) `rls_enforcement.feature` is NOT a placeholder — 7 real scenarios with step definitions; (2) unit tests exist for GenericRepository, GenericLock, _inject_tenant_filter, set_rls_org on SQLite; (3) rate limiter has in-memory TokenBucket fallback — confirmed working; (4) corrected "conditional migration" claim — both 0002 and 0025 are NOT conditional. Added 4 new known gaps (no RepositoryHub tests, no set_rls_user_context generic path test, MODULO_DB validation missing, MariaDB profile absent). Website docs stub already exists at `Website/src/docs/database-abstraction.md`.

- 2026-07-08: Cross-cutting QA (index 274).
- 2026-07-10: Cross-cutting QA (index 305). Fixed CRITICAL — `GenericLock.acquire_lock` added `except asyncio.CancelledError` handler that releases the lock and cleans up `_generic_owners`/`_generic_locks` on task cancellation (previously leaked locked state permanently). Fixed CRITICAL — `session.py` `AsyncSessionLocal` changed to `autobegin=False` and `get_session` rollback handler now clears `session.info` to prevent stale `org_id` cross-org data leak. Fixed CRITICAL — `test_postgres_lock_release_calls_pg_advisory_unlock` added `result_mock.scalar_one.return_value = True` (previously used bare `MagicMock` which is always truthy — release branch logic was untested); added companion test `test_postgres_lock_release_warns_when_not_held` with `scalar_one.return_value = False`. Fixed MAJOR — narrowed `except Exception` to `except (json.JSONDecodeError, KeyError, InvalidToken)` in `_decrypt_checkpoint` and `except InvalidToken` in `_decrypt_blobs`/`_decrypt_writes` (programming errors no longer silently masked). Fixed MAJOR — product map RepositoryHub dispatch tests checkbox marked `[x]` (tests exist at `test_multi_backend_bdd.py:467-494`). Removed stale Known Gap for RepositoryHub tests. Added 5 new Known Gaps (rls.py module cohesion, no concurrent key-creation test, no `RepositoryHub(db_type=None)` test, `autobegin=False` fix). All 75 DB unit tests pass. Fixed CRITICAL — `prd: 8.17` was stale (8.17 = Eval System, not DB abstraction). Changed to `prd: 6.1, 6.2` + added `adr: [docs/adr/002-database-abstraction-strategy.md]`. Fixed MAJOR — `tenant_isolation.feature` BDD API paths used `/api/pipelines` instead of `/api/v1/pipelines` (step text mismatch); updated step definition in `test_auth.py` to match. Fixed MAJOR — misnamed test `test_without_org_context_no_filter_injected` renamed to `test_apply_tenant_filter_still_injects_when_session_lacks_org_context` (the test verified filter IS injected, not the opposite). Fixed MINOR — removed dead assertion `assert now is not None` (datetime.datetime.now never returns None) from `test_default_factory_is_not_backend_specific`. Added 4 new Known Gaps: (1) no RLS DML enforcement integration test for Postgres (INSERT/UPDATE/DELETE untested), (2) race-prone `asyncio.sleep(0.1)` in lock tests should use `asyncio.Event`, (3) unit tests use mock-chain assertions that verify `.where()` was called but not the actual SQL predicate, (4) `test_multi_backend_bdd.py` naming is misleading (not a real BDD file). Status: partial.

- 2026-07-09: Cross-cutting QA (index 340). Fixed MAJOR — added `TestSetRlsUserContextMultiBackend` with 5 unit tests covering `set_rls_user_context` on Postgres (set_config), SQLite (session.info), MariaDB (session.info), no-transaction error, and cross-session isolation (no leak). Resolved stale Known Gap — `MODULO_DB` validation already exists at `settings.py:174-179` (validator rejects any value not in "postgres", "sqlite", "mariadb", "mysql"). Marked `[ ]`→`[x]` for `set_rls_user_context` generic backend test coverage. 2 files changed, 87 insertions, 0 deletions.
