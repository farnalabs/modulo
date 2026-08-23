"""Row-level security and tenant-filtering helpers.

On Postgres, tenant scoping uses set_config('app.organisation_id', value, is_local=true),
which is equivalent to SET LOCAL and supports bound parameters. The semgrep rule
rls_set_local enforces that bare SET (without is_local) is never used.

On generic backends (MariaDB, SQLite), tenant scoping stores the org_id in
``session.info`` and a ``do_orm_execute`` listener injects ``WHERE organisation_id = :oid``
into every SELECT/UPDATE/DELETE automatically.
"""

import asyncio
import logging
import uuid

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.orm import Session as SASession

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_TENANT_KEY = "org_id"
_TENANT_COLUMN = "organisation_id"


async def _ensure_active_transaction(session: AsyncSession) -> str:
    """Verify an active transaction exists and return the dialect name.

    Shared preamble for set_rls_org and set_rls_user_context to avoid
    duplicating the in_transaction guard and get_bind call.

    Works with both async (AsyncSession) and sync (Session) sessions.
    For sync sessions, get_bind() returns a sync Engine directly;
    for async sessions, it returns a coroutine that must be awaited.
    """
    if not session.in_transaction():
        raise RuntimeError("set_rls_* requires an active transaction; wrap the call in `async with session.begin():`")
    bind = session.get_bind()
    if asyncio.iscoroutine(bind):
        # AsyncSession — get_bind() returns a coroutine
        bind = await bind
    # Sync Session — get_bind() returns Engine directly
    return bind.dialect.name


async def set_rls_org(session: AsyncSession, org_id: uuid.UUID | None) -> None:
    """Activate RLS / tenant scoping for *org_id* within the current transaction.

    Requires an active transaction — raises RuntimeError otherwise so callers
    cannot accidentally call this outside a BEGIN block and get silent no-ops.

    When *org_id* is ``None`` (system admin with no org claim), RLS is skipped
    so the operation runs unscoped — the caller must scope manually.

    Postgres: calls ``SELECT set_config('app.organisation_id', :oid, true)``.
    Generic backends (MariaDB, SQLite): stores *org_id* in ``session.info``
    for the ``do_orm_execute`` tenant-filter listener to pick up.
    """
    if org_id is None:
        return

    dialect = await _ensure_active_transaction(session)

    if dialect == "postgresql":
        # set_config(name, value, is_local=true) is equivalent to SET LOCAL and
        # supports parameterised queries; bare SET LOCAL does not accept $1 placeholders.
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
    else:
        session.info[_TENANT_KEY] = org_id


async def set_rls_user_context(session: AsyncSession, user_id: uuid.UUID, org_role: str) -> None:
    """Set the current user identity and role for team-scoped RLS policies.

    Must be called inside an active transaction alongside set_rls_org.

    Postgres: calls ``set_config`` for ``app.user_id`` and ``app.org_role``.
    Generic backends: stores values in ``session.info`` for future use.
    """
    dialect = await _ensure_active_transaction(session)

    if dialect == "postgresql":
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(user_id)},
        )
        await session.execute(
            text("SELECT set_config('app.org_role', :role, true)"),
            {"role": org_role},
        )
    else:
        session.info["user_id"] = user_id
        session.info["org_role"] = org_role


async def set_rls_execution_context(session: AsyncSession) -> None:
    """Mark the transaction as internal execution (org-scoped, team-blind).

    Background machinery (executor, cron, dispatch, recovery, cost controller,
    housekeeping) reads team-scoped tables with org scope only — there is no
    single user context for a run. The ``rls_team_isolation`` policy ORs in
    ``app.execution_context`` so internal reads see all org rows while
    user-facing sessions (which never set it) stay team-filtered.

    Must be called inside an active transaction alongside set_rls_org.
    """
    dialect = await _ensure_active_transaction(session)

    if dialect == "postgresql":
        await session.execute(
            text("SELECT set_config('app.execution_context', 'true', true)"),
        )
    else:
        session.info["execution_context"] = True


def register_rls_reset_hook(engine: AsyncEngine) -> None:
    """Register a pool-checkout listener that clears stale org context.

    Postgres: Sets ``app.organisation_id`` to the empty string at session level
    whenever a connection is checked out from the pool. Combined with
    set_config(is_local=true) (which reverts to the session-level value on
    transaction end), this guarantees no org_id leaks across requests sharing
    a pooled connection.

    Generic backends: no-op (session.info is scoped to the session, not the
    connection pool, so there is nothing to reset at the pool level).

    Must be called once after the engine is created, typically in session.py.

    Note: For asyncpg, ``set_config`` is already transaction-scoped via
    ``is_local=true``, so the pool-level reset is defense-in-depth only.
    The sync connection API (``cursor()``) is compatible with all DBAPI2
    drivers including asyncpg's sync proxy.
    """
    dialect = engine.dialect.name
    if dialect != "postgresql":
        _log.info("Skipping pool-level RLS reset hook — %s backend", dialect)
        return

    _rls_config_names = ["app.organisation_id", "app.user_id", "app.org_role", "app.execution_context"]

    @event.listens_for(engine.sync_engine, "checkout")
    def _reset_org_on_checkout(
        dbapi_connection: object,
        _connection_record: object,
        _connection_proxy: object,
    ) -> None:
        try:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                for config_name in _rls_config_names:
                    cursor.execute(f"SELECT set_config('{config_name}', '', false)")
            finally:
                cursor.close()
        except AttributeError:
            _log.warning(
                "rls_reset_hook: sync cursor API not available on this driver",
                exc_info=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "rls_reset_hook: failed to clear RLS session context on checkout",
                exc_info=True,
            )


def _inject_tenant_filter(execute_state: ORMExecuteState) -> None:
    """ORM execute listener that injects ``WHERE organisation_id = :oid``.

    Reads ``org_id`` from ``session.info`` (set by ``set_rls_org``) and adds
    the WHERE clause to every SELECT, UPDATE, and DELETE statement targeting
    a model that has an ``organisation_id`` column.
    """
    org_id = execute_state.session.info.get(_TENANT_KEY)
    if org_id is None:
        if execute_state.is_select or execute_state.is_update or execute_state.is_delete:
            _log.debug(
                "_inject_tenant_filter: no org_id in session.info for %s on %s",
                "select/update/delete"
                if execute_state.is_select
                else "update/delete"
                if execute_state.is_update
                else "delete",
                execute_state.statement,
            )
        return

    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return

    stmt = execute_state.statement
    injected = False

    # SELECT / bulk operations expose entities via column_descriptions.
    if hasattr(stmt, "column_descriptions"):
        for desc in stmt.column_descriptions:
            entity = desc.get("entity")
            if entity is None or entity is object:
                continue
            if hasattr(entity, _TENANT_COLUMN):
                stmt = stmt.where(getattr(entity, _TENANT_COLUMN) == org_id)  # type: ignore[attr-defined,union-attr]
                injected = True

    # ORM UPDATE/DELETE expose entities via all_mapper_classes.
    if not injected and hasattr(execute_state, "all_mapper_classes") and execute_state.all_mapper_classes:
        for mapper in execute_state.all_mapper_classes:
            entity = mapper.class_
            if hasattr(entity, _TENANT_COLUMN):
                stmt = stmt.where(getattr(entity, _TENANT_COLUMN) == org_id)  # type: ignore[attr-defined]
                injected = True

    if injected:
        execute_state.statement = stmt


def register_tenant_filter() -> None:
    """Register a ``do_orm_execute`` listener on the ORM ``Session`` class
    (propagates to ``AsyncSession`` instances).

    Only activates for non-Postgres backends where RLS is unavailable.
    Reads ``org_id`` from ``session.info`` (set by ``set_rls_org``) and injects
    ``WHERE organisation_id = :oid`` into every SELECT, UPDATE, and DELETE.

    This is the generic-backend counterpart to Postgres RLS — it makes the
    same 200+ CRUD functions and 30+ route handlers work on MariaDB and SQLite
    without any code changes.
    """
    db_type = get_settings().modulo_db.lower()
    if db_type == "postgres":
        _log.info("Skipping ORM tenant filter — Postgres RLS handles scoping")
        return

    _log.info("Registering ORM tenant filter for %s backend", db_type)
    event.listen(SASession, "do_orm_execute", _inject_tenant_filter)
