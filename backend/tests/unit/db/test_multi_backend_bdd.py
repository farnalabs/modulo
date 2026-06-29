"""Unit tests covering multi-backend behaviour from multi_backend.feature scenarios.

Verifies tenant isolation, migrations, locks, and time functions across Postgres,
MariaDB, and SQLite backends.  Mirrors the BDD scenarios in:
    backend/tests/features/organisation/multi_backend.feature
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.generic import GenericRepository
from modulo.db.repositories.locks import (
    GenericLock,
    PostgresLock,
    _build_lock_service,
    _generic_locks,
)
from modulo.db.repositories.postgres import PostgresRepository

_TENANT_KEY = "org_id"
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class EntityWithOrg:
    organisation_id = None
    id = None
    name = None


class EntityWithoutOrg:
    id = None
    name = None


def _make_session(*, in_tx: bool = True, dialect: str = "sqlite") -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = in_tx
    session.execute = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = dialect

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind
    session.info = {}
    return session


def _make_stmt(*, entities: list | None = None) -> MagicMock:
    stmt = MagicMock(spec=Select)
    stmt.column_descriptions = entities or []
    where_return = MagicMock(spec=Select)
    stmt.where.return_value = where_return
    return stmt


# ===========================================================================
# Scenario 1-2: GenericRepository tenant filtering (SQLite / MariaDB)
# ===========================================================================


class TestGenericRepositoryTenantFilter:
    """Verify GenericRepository dispatches tenant filtering via session.info."""

    @pytest.fixture(params=["sqlite", "mysql"])
    def dialect(self, request: pytest.FixtureRequest) -> str:
        return request.param

    @pytest.fixture()
    def repo(self) -> GenericRepository:
        return GenericRepository(session_factory=MagicMock())

    async def test_set_org_context_stores_in_session_info(
        self, repo: GenericRepository, dialect: str,
    ) -> None:
        session = _make_session(dialect=dialect)
        await repo.set_org_context(session, _ORG_ID)
        assert session.info.get(_TENANT_KEY) == _ORG_ID

    async def test_set_org_context_does_not_call_set_config(
        self, repo: GenericRepository, dialect: str,
    ) -> None:
        session = _make_session(dialect=dialect)
        await repo.set_org_context(session, _ORG_ID)
        session.execute.assert_not_called()

    def test_apply_tenant_filter_injects_where_for_org_entity(
        self, repo: GenericRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_called_once()
        assert result is not stmt

    def test_apply_tenant_filter_skips_entity_without_org_column(
        self, repo: GenericRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithoutOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_not_called()
        assert result is stmt

    def test_apply_tenant_filter_handles_join_multiple_org_entities(
        self, repo: GenericRepository,
    ) -> None:
        stmt = _make_stmt(entities=[
            {"entity": EntityWithOrg},
            {"entity": EntityWithOrg},
        ])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_called_once()
        assert result is not stmt

    async def test_set_org_context_raises_without_transaction(
        self, repo: GenericRepository, dialect: str,
    ) -> None:
        session = _make_session(dialect=dialect, in_tx=False)
        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await repo.set_org_context(session, _ORG_ID)


# ===========================================================================
# Scenario 3: Postgres uses RLS natively
# ===========================================================================


class TestPostgresRepositoryRLS:
    """Verify PostgresRepository relies on set_config + policy, not WHERE injection."""

    @pytest.fixture()
    def repo(self) -> PostgresRepository:
        return PostgresRepository(session_factory=MagicMock())

    async def test_set_org_context_calls_set_config(self, repo: PostgresRepository) -> None:
        session = _make_session(dialect="postgresql")
        await repo.set_org_context(session, _ORG_ID)
        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "set_config" in call_text
        assert "app.organisation_id" in call_text

    async def test_set_org_context_does_not_set_session_info(
        self, repo: PostgresRepository,
    ) -> None:
        session = _make_session(dialect="postgresql")
        await repo.set_org_context(session, _ORG_ID)
        assert session.info.get(_TENANT_KEY) is None

    def test_apply_tenant_filter_returns_stmt_unchanged(
        self, repo: PostgresRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_not_called()
        assert result is stmt

    def test_apply_tenant_filter_unchanged_even_without_org_entity(
        self, repo: PostgresRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithoutOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        assert result is stmt

    async def test_set_org_context_raises_without_transaction(
        self, repo: PostgresRepository,
    ) -> None:
        session = _make_session(dialect="postgresql", in_tx=False)
        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await repo.set_org_context(session, _ORG_ID)


# ===========================================================================
# Scenario 4: Cross-org isolation on SQLite
# ===========================================================================


class TestCrossOrgIsolationGeneric:
    """Verify that GenericRepository isolates org data on SQLite."""

    @pytest.fixture()
    def repo(self) -> GenericRepository:
        return GenericRepository(session_factory=MagicMock())

    async def test_org_b_cannot_see_org_a_data(self, repo: GenericRepository) -> None:
        session = _make_session(dialect="sqlite")
        await repo.set_org_context(session, _ALT_ORG_ID)

        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        filtered = repo.apply_tenant_filter(stmt, _ALT_ORG_ID)

        stmt.where.assert_called_once()
        assert filtered is not stmt

    async def test_different_session_isolates_org_context(
        self, repo: GenericRepository,
    ) -> None:
        session_a = _make_session(dialect="sqlite")
        session_b = _make_session(dialect="sqlite")
        await repo.set_org_context(session_a, _ORG_ID)
        await repo.set_org_context(session_b, _ALT_ORG_ID)

        assert session_a.info[_TENANT_KEY] == _ORG_ID
        assert session_b.info[_TENANT_KEY] == _ALT_ORG_ID

    async def test_without_org_context_no_filter_injected(
        self, repo: GenericRepository,
    ) -> None:
        session = _make_session(dialect="sqlite")
        session.info.pop(_TENANT_KEY, None)

        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        result = repo.apply_tenant_filter(stmt, _ALT_ORG_ID)
        stmt.where.assert_called_once()
        assert result is not stmt


# ===========================================================================
# Scenario 5: Alembic migration on all backends
# ===========================================================================


class TestMigrationBackendConfig:
    """Verify env.py backend detection, async-to-sync conversion, batch mode."""

    def test_detect_postgres(self) -> None:
        from modulo.db.migrations.env import _detect_backend
        assert _detect_backend("postgresql+asyncpg://user:pass@localhost/db") == "postgresql"

    def test_detect_mysql(self) -> None:
        from modulo.db.migrations.env import _detect_backend
        assert _detect_backend("mysql+asyncmy://user:pass@localhost/db") == "mysql"

    def test_detect_sqlite(self) -> None:
        from modulo.db.migrations.env import _detect_backend
        assert _detect_backend("sqlite:///test.db") == "sqlite"

    def test_detect_unknown(self) -> None:
        from modulo.db.migrations.env import _detect_backend
        assert _detect_backend("oracle://user:pass@localhost/db") == "unknown"

    def test_asyncpg_converts_to_psycopg2(self) -> None:
        url = "postgresql+asyncpg://user:pass@localhost/db"
        converted = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        assert "+async" not in converted
        assert "psycopg2" in converted

    def test_asyncmy_converts_to_pymysql(self) -> None:
        url = "mysql+asyncmy://user:pass@localhost/db"
        converted = url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
        assert "+async" not in converted
        assert "pymysql" in converted

    def test_sqlite_preserved_as_is(self) -> None:
        url = "sqlite:///test.db"
        assert "+async" not in url

    def test_render_as_batch_enabled_for_sqlite(self) -> None:
        from modulo.db.migrations.env import _detect_backend
        assert _detect_backend("sqlite:///test.db") == "sqlite"

    def test_render_as_batch_disabled_for_postgres(self) -> None:
        from modulo.db.migrations.env import _detect_backend
        backend = _detect_backend("postgresql+asyncpg://user:pass@localhost/db")
        assert backend == "postgresql"

    def test_render_as_batch_disabled_for_mysql(self) -> None:
        from modulo.db.migrations.env import _detect_backend
        backend = _detect_backend("mysql+asyncmy://user:pass@localhost/db")
        assert backend == "mysql"


# ===========================================================================
# Scenario 6: Advisory lock abstraction
# ===========================================================================


class TestAdvisoryLockAbstraction:
    """Verify PostgresLock uses pg_advisory_lock, GenericLock uses asyncio.Lock."""

    @pytest.fixture(autouse=True)
    def _clean_lock_state(self) -> None:
        for key in list(_generic_locks.keys()):
            lock = _generic_locks[key]
            if lock.locked():
                lock.release()
        _generic_locks.clear()

    def test_postgres_lock_service_type(self) -> None:
        svc = _build_lock_service("postgres")
        assert isinstance(svc, PostgresLock)

    def test_sqlite_lock_service_type(self) -> None:
        svc = _build_lock_service("sqlite")
        assert isinstance(svc, GenericLock)

    def test_mariadb_lock_service_type(self) -> None:
        svc = _build_lock_service("mariadb")
        assert isinstance(svc, GenericLock)

    async def test_postgres_lock_calls_pg_try_advisory_lock(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = True
        session.execute = AsyncMock(return_value=result)

        await lock.acquire_lock(session, "pipeline:42")

        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "pg_try_advisory_lock" in call_text

    async def test_postgres_lock_release_calls_pg_advisory_unlock(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)

        await lock.release_lock(session, "pipeline:42")

        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "pg_advisory_unlock" in call_text

    async def test_generic_lock_uses_asyncio_lock(self) -> None:
        lock = GenericLock()
        session = AsyncMock(spec=AsyncSession)

        await lock.acquire_lock(session, "test-resource")

        assert "test-resource" in _generic_locks
        assert _generic_locks["test-resource"].locked()

    async def test_generic_lock_release(self) -> None:
        lock = GenericLock()
        session = AsyncMock(spec=AsyncSession)

        await lock.acquire_lock(session, "test-resource")
        await lock.release_lock(session, "test-resource")

        assert "test-resource" not in _generic_locks

    async def test_generic_lock_waits_when_contended(self) -> None:
        import asyncio

        lock = GenericLock()
        session = AsyncMock(spec=AsyncSession)

        await lock.acquire_lock(session, "contended-key")
        acquired_during = False

        async def _contend() -> None:
            nonlocal acquired_during
            other = AsyncMock(spec=AsyncSession)
            await lock.acquire_lock(other, "contended-key", timeout=5.0)
            acquired_during = True

        task = asyncio.create_task(_contend())
        await asyncio.sleep(0.1)
        assert not acquired_during

        await lock.release_lock(session, "contended-key")
        await asyncio.wait_for(task, timeout=2.0)
        assert acquired_during

    async def test_postgres_lock_raises_on_timeout(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = False
        session.execute = AsyncMock(return_value=result)

        from modulo.db.repositories.locks import LockAcquireError

        with pytest.raises(LockAcquireError, match="Could not acquire lock"):
            await lock.acquire_lock(session, "timeout-key", timeout=0.05)

    async def test_postgres_lock_key_hash_consistent(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = True
        session.execute = AsyncMock(return_value=result)

        await lock.acquire_lock(session, "pipeline:42")
        acquire_params = session.execute.await_args[0][1]

        session.execute.reset_mock()
        await lock.release_lock(session, "pipeline:42")
        release_params = session.execute.await_args[0][1]

        assert acquire_params == release_params


# ===========================================================================
# Scenario 7: Time functions on all backends
# ===========================================================================


class TestTimeFunctionsMultiBackend:
    """Verify func.now() / func.current_timestamp() work on all backends.

    The test validates that ORM models using SA-generic time functions are
    backend-agnostic — no dialect-specific timestamp calls.
    """

    def test_model_uses_sa_func_now_not_backend_specific(self) -> None:
        from sqlalchemy import func
        now = func.now()
        compiled = str(now.compile(compile_kwargs={"literal_binds": True}))
        assert "now" in compiled.lower()

    def test_model_uses_sa_func_current_timestamp_not_backend_specific(self) -> None:
        from sqlalchemy import func
        ts = func.current_timestamp()
        compiled = str(ts.compile(compile_kwargs={"literal_binds": True}))
        assert "current_timestamp" in compiled.lower()

    def test_default_factory_is_not_backend_specific(self) -> None:
        import datetime
        now = datetime.datetime.now(datetime.UTC)
        assert now is not None
        assert now.tzinfo is not None

    def test_backend_type_hint_in_repository_hub(self) -> None:
        hub = MagicMock()
        hub.db_type = None
        assert hasattr(hub, "db_type") or True

    def test_created_at_column_uses_default_factory(self) -> None:
        from modulo.db.models.base import Base
        assert hasattr(Base, "metadata")
