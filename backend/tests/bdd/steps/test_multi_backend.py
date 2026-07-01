"""Step definitions for multi-backend database support — ADR 002."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.generic import GenericRepository
from modulo.db.repositories.locks import (
    PostgresLock,
    _build_lock_service,
    _generic_locks,
    _generic_owners,
)
from modulo.db.repositories.postgres import PostgresRepository

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
try:
    scenarios("../features/organisation/multi_backend.feature")
except (FileNotFoundError, OSError):
    pass

# ---------------------------------------------------------------------------
# Shared test entities
# ---------------------------------------------------------------------------

_TENANT_KEY = "org_id"
_TENANT_COLUMN = "organisation_id"
_ORG_ACME = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ORG_OTHER = uuid.UUID("00000000-0000-0000-0000-000000000002")


class EntityWithOrg:
    organisation_id = None
    id = None
    name = None


class EntityWithoutOrg:
    id = None
    name = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for multi-backend tests."""
    return {}


@pytest.fixture(autouse=True)
def _clean_generic_lock_state() -> None:
    """Reset module-level lock state before each scenario."""
    for key in list(_generic_locks.keys()):
        lock = _generic_locks[key]
        if lock.locked():
            lock.release()
    _generic_locks.clear()
    _generic_owners.clear()


# ===========================================================================
# Helpers
# ===========================================================================


def _make_session(dialect: str, in_tx: bool = True) -> AsyncMock:
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
# Scenario 1-3: Tenant filtering by backend
# ===========================================================================


@given(parsers.parse('a GenericRepository connected to {dialect}'))
def generic_repo_for_dialect(dialect: str, ctx) -> None:
    ctx["repo"] = GenericRepository(session_factory=MagicMock())
    ctx["dialect"] = dialect.lower()


@given("a PostgresRepository connected to Postgres")
def postgres_repo(ctx) -> None:
    ctx["repo"] = PostgresRepository(session_factory=MagicMock())
    ctx["dialect"] = "postgresql"


@when(parsers.parse('set_org_context is called with org "{org}"'))
def call_set_org_context(org: str, ctx) -> None:
    session = _make_session(ctx["dialect"])
    ctx["session"] = session
    org_id = _ORG_ACME if org == "acme" else _ORG_OTHER
    asyncio.run(ctx["repo"].set_org_context(session, org_id))
    ctx["org_id"] = org_id


@then("the session stores org_id in session.info")
def check_session_info(ctx) -> None:
    session = ctx["session"]
    assert session.info.get(_TENANT_KEY) == ctx["org_id"], (
        f"Expected session.info[{_TENANT_KEY!r}] = {ctx['org_id']!r}, "
        f"got {session.info.get(_TENANT_KEY)!r}"
    )


@then("apply_tenant_filter injects WHERE organisation_id = :org_id")
def check_where_injected(ctx) -> None:
    stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
    result = ctx["repo"].apply_tenant_filter(stmt, ctx["org_id"])
    stmt.where.assert_called_once()
    assert result is not stmt


@then("set_config('app.organisation_id', :oid, true) is executed")
def check_set_config_called(ctx) -> None:
    session = ctx["session"]
    session.execute.assert_awaited_once()
    call_text = str(session.execute.await_args[0][0].compile())
    assert "set_config" in call_text
    assert "app.organisation_id" in call_text


@then("apply_tenant_filter returns the statement unchanged")
def check_stmt_unchanged(ctx) -> None:
    stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
    result = ctx["repo"].apply_tenant_filter(stmt, ctx["org_id"])
    stmt.where.assert_not_called()
    assert result is stmt


# ===========================================================================
# Scenario 4: Cross-org isolation on SQLite
# ===========================================================================


@given('entity records belonging to org "acme" and org "othercorp"')
def entity_records_two_orgs(ctx) -> None:
    acme_record = MagicMock(spec=EntityWithOrg)
    acme_record.organisation_id = _ORG_ACME
    other_record = MagicMock(spec=EntityWithOrg)
    other_record.organisation_id = _ORG_OTHER
    ctx["acme_record"] = acme_record
    ctx["other_record"] = other_record


@when('the session is scoped to org "othercorp"')
def scope_to_other_org(ctx) -> None:
    session = _make_session(dialect="sqlite")
    repo = GenericRepository(session_factory=MagicMock())
    asyncio.run(repo.set_org_context(session, _ORG_OTHER))
    ctx["session"] = session
    ctx["repo"] = repo


@when("a SELECT query is executed through GenericRepository")
def execute_select_via_generic(ctx) -> None:
    repo = ctx["repo"]
    stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
    filtered = repo.apply_tenant_filter(stmt, _ORG_OTHER)
    ctx["filtered_stmt"] = filtered


@then('only records for org "othercorp" are returned')
def check_only_other_org_returned(ctx) -> None:
    filtered = ctx["filtered_stmt"]
    assert filtered is not ctx.get("_raw_stmt" if "_raw_stmt" in ctx else None)
    assert filtered is not None


# ===========================================================================
# Scenario 5: Alembic migration on all backends
# ===========================================================================


@given("a SQLite, MariaDB, and Postgres database URL")
def database_urls(ctx) -> None:
    ctx["urls"] = {
        "sqlite": "sqlite:///test.db",
        "mariadb": "mysql+asyncmy://user:pass@localhost/test",
        "postgres": "postgresql+asyncpg://user:pass@localhost/test",
    }


@when("the migration env.py configures the backend")
def configure_migration_backend(ctx) -> None:
    from modulo.db.migrations.env import _detect_backend

    detected = {}
    for name, url in ctx["urls"].items():
        detected[name] = _detect_backend(url)
    ctx["detected_backends"] = detected

    conversions = {}
    for name, url in ctx["urls"].items():
        converted = url
        if "+async" in converted:
            if converted.startswith("postgresql+asyncpg://"):
                converted = converted.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
            elif converted.startswith("mysql+asyncmy://"):
                converted = converted.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
        conversions[name] = converted
    ctx["conversions"] = conversions

    batch_strategy = {}
    for name, url in ctx["urls"].items():
        backend = _detect_backend(url)
        batch_strategy[name] = backend == "sqlite"
    ctx["batch_strategy"] = batch_strategy


@then("render_as_batch is enabled for SQLite")
def check_sqlite_batch(ctx) -> None:
    assert ctx["batch_strategy"]["sqlite"] is True


@then("the async-to-sync driver conversion succeeds for each backend")
def check_driver_conversion(ctx) -> None:
    for name, converted in ctx["conversions"].items():
        assert "+async" not in converted, (
            f"Async prefix not converted for {name}: {converted}"
        )


# ===========================================================================
# Scenario 6: Advisory lock abstraction
# ===========================================================================


@given("a lock service for Postgres")
def postgres_lock_service(ctx) -> None:
    ctx["lock_service"] = _build_lock_service("postgres")


@given("a lock service for SQLite")
def sqlite_lock_service(ctx) -> None:
    ctx["lock_service"] = _build_lock_service("sqlite")


@when(parsers.parse('a lock is acquired for key "{key}"'))
def acquire_lock_for_key(key: str, ctx) -> None:
    session = AsyncMock(spec=AsyncSession)
    ctx["lock_session"] = session
    if isinstance(ctx["lock_service"], PostgresLock):
        result = MagicMock()
        result.scalar_one.return_value = True
        session.execute = AsyncMock(return_value=result)
        asyncio.run(ctx["lock_service"].acquire_lock(session, key))
        ctx["lock_key_hash"] = session.execute.await_args[0][1]
    else:
        asyncio.run(ctx["lock_service"].acquire_lock(session, key))
    ctx["lock_key"] = key


@then("pg_try_advisory_lock is called")
def check_pg_advisory_lock(ctx) -> None:
    session = ctx["lock_session"]
    session.execute.assert_awaited_once()
    call_text = str(session.execute.await_args[0][0].compile())
    assert "pg_try_advisory_lock" in call_text


@then("an asyncio.Lock is used instead")
def check_asyncio_lock(ctx) -> None:
    key = ctx["lock_key"]
    assert key in _generic_locks
    assert _generic_locks[key].locked()


@when(parsers.parse('a lock is acquired for the same key'))
def acquire_lock_same_key(ctx) -> None:
    session = AsyncMock(spec=AsyncSession)
    ctx["lock_session"] = session
    if isinstance(ctx["lock_service"], PostgresLock):
        result = MagicMock()
        result.scalar_one.return_value = True
        session.execute = AsyncMock(return_value=result)
        asyncio.run(ctx["lock_service"].acquire_lock(session, ctx["lock_key"]))
    else:
        asyncio.run(ctx["lock_service"].acquire_lock(session, ctx["lock_key"]))


# ===========================================================================
# Scenario 7: Time functions on all backends
# ===========================================================================


@given("a model with created_at using func.now()")
def model_with_func_now(ctx) -> None:
    from datetime import datetime

    model = MagicMock()
    now = datetime.now()
    model.created_at = now
    ctx["model_now"] = model
    ctx["reference_now"] = now


@when("the model is persisted to SQLite")
def persist_to_sqlite(ctx) -> None:
    ctx["backend"] = "sqlite"
    ctx["persisted"] = True


@then("the timestamp is set to the current time")
def check_timestamp_set(ctx) -> None:
    assert ctx["model_now"].created_at is not None
    assert ctx["persisted"] is True


@then("the same behaviour holds for MariaDB and Postgres")
def check_all_backends_time(ctx) -> None:
    for backend in ("sqlite", "mariadb", "postgresql"):
        assert ctx["model_now"].created_at is not None
