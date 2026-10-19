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
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.orm import Session as SASession

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_TENANT_KEY = "org_id"
_TENANT_COLUMN = "organisation_id"

# The RLS GUC names the pool-checkout reset hook clears. Module-level constant
# so any consumer of the hook is validated against one authoritative set.
_RLS_CONFIG_NAMES: tuple[str, ...] = (
    "app.organisation_id",
    "app.user_id",
    "app.org_role",
    "app.execution_context",
)


def _set_config_statement(config_name: str) -> str:
    """Build the pool-level ``SELECT set_config(...)`` statement for a GUC reset.

    The DBAPI2 sync cursor API used by the checkout listener has no portable
    parameter binding, so the statement is interpolated — but the value is a
    GUC NAME that must come from ``_RLS_CONFIG_NAMES`` (fail closed rather
    than interpolate an unvalidated name). FAR-915 / GitHub #126 hygiene:
    callers pass only known constants today; the guard keeps it that way.
    """
    if config_name not in _RLS_CONFIG_NAMES:
        raise ValueError(f"Unknown RLS config name: {config_name!r} — must be one of {_RLS_CONFIG_NAMES}")
    return f"SELECT set_config('{config_name}', '', false)"


class OutputsRlsMismatch(RuntimeError):  # noqa: N818  # name mandated by the reviewed FAR-583 design
    """The session's RLS org context disagrees with the run_node_outputs org.

    Raised by the run_node_outputs repo module (FAR-583): the new table's
    Python-side org gate must catch what the database does not — Postgres'
    strict fail-closed policy kills a NULL-context write with 42501 anyway,
    but SQLite's ``_inject_tenant_filter`` does not cover INSERT (and raw
    text() reads bypass it entirely), so write paths REQUIRE a bound,
    matching org context and read paths reject a mismatched one here.
    """


async def read_rls_org(session: AsyncSession) -> uuid.UUID | None:
    """Read the RLS org id currently bound to *session*, or ``None``.

    The authoritative source is selected by the session's DIALECT FIRST, so the
    mechanism the dialect actually enforces is the one that is read:

    * Postgres — the ``current_setting('app.organisation_id', true)`` GUC is the
      only authoritative source. ``set_rls_org`` writes the GUC (never
      ``session.info``) on Postgres, so a populated ``session.info['org_id']`` is
      unrelated to the transaction's real RLS context and must not shadow it — a
      stale or foreign info value would otherwise report a different org than RLS
      actually enforces.
    * Generic backends (MariaDB, SQLite) — store the org in
      ``session.info['org_id']`` and have no GUC, so the info lookup is
      authoritative there.

    ``auth/secret_storage._read_rls_org_id`` delegates here, so this module is
    the ONE implementation of the ordering: the two paths cannot drift into
    divergent precedence (a drifted ordering on Postgres would resolve a decrypt
    against a stale or foreign org). The empty string (GUC unset) normalises to
    ``None``; a value that cannot be coerced to a UUID is treated as absent. Any
    failure executing the GUC read (non-Postgres backend without the function,
    closed session) degrades to ``None``.
    """
    dialect = await _resolve_session_dialect(session)
    if dialect == "postgresql" or dialect is None:
        # Postgres reads the GUC. An UNREADABLE dialect (``None`` — a bind
        # existed but its name could not be read) fails CLOSED to this same
        # probe rather than re-entering the info-first ordering FAR-1884
        # removes: a bind that cannot be identified might be Postgres, and
        # consulting ``session.info`` first there is the defect this change
        # exists to eliminate. A session with NO bind at all returns ``""`` and
        # keeps the generic path below.
        return await _read_guc_org(session)

    info = getattr(session, "info", None)
    if isinstance(info, dict):
        value = info.get(_TENANT_KEY)
        if value is not None:
            parsed = _coerce_org_id(value)
            if parsed is not None:
                return parsed

    # Generic backends have no ``app.organisation_id`` GUC, so this probe
    # degrades to ``None`` there (the function is absent). It is retained so a
    # session double that exposes only ``execute`` — and no resolvable dialect —
    # behaves exactly as it did before the dialect split.
    return await _read_guc_org(session)


async def _resolve_session_dialect(session: AsyncSession) -> str | None:
    """Resolve the session bind's dialect name, distinguishing the failure modes.

    Mirrors the bind dance the other dialect guards use
    (``db.crud.run.get_dialect_name``, ``db.crud.row_lock._dialect_name``) but
    classifies a failure instead of collapsing every one to ``""``:

    * a dialect name (e.g. ``"postgresql"``) when the bind's dialect is readable;
    * ``""`` when there is NO bind at all — no ``get_bind`` attribute (a session
      double exposing only ``execute``/``info``) or ``get_bind()`` raised
      ``sqlalchemy.exc.UnboundExecutionError`` (an unbound session; it is a
      subclass of ``InvalidRequestError``, which is the class caught below).
      ``""`` keeps the pre-FAR-1884 generic (``session.info``-first) read order;
    * ``None`` when a bind EXISTED but its dialect could not be read. This must
      NOT silently degrade to the generic order — that is exactly the ordering
      FAR-1884 removes on the authoritative backend — so the caller fails closed
      to the GUC probe instead, and the condition is logged loudly here.
    """
    get_bind = getattr(session, "get_bind", None)
    if get_bind is None:
        return ""
    try:
        bind = get_bind()
        if asyncio.iscoroutine(bind):
            bind = await bind
    except InvalidRequestError:
        # No bind at all: either ``get_bind()`` raised because nothing is bound
        # (``sqlalchemy.exc.UnboundExecutionError`` — a subclass of
        # ``InvalidRequestError``, so the base catch covers it), or the session
        # is a lightweight double whose bind lookup is unavailable. Either way
        # the pre-FAR-1884 generic path is correct and must be preserved.
        return ""
    try:
        return str(bind.dialect.name)
    except AttributeError:
        # A bind existed but carried no readable dialect. Never swallow this:
        # the caller falls closed to the GUC probe rather than re-entering the
        # info-first ordering FAR-1884 exists to eliminate.
        _log.warning(
            "read_rls_org: session bind %r exposes no readable dialect; failing closed to the Postgres GUC probe",
            bind,
            exc_info=True,
        )
        return None


async def _read_guc_org(session: AsyncSession) -> uuid.UUID | None:
    """Read ``current_setting('app.organisation_id', true)``, degrading to ``None``.

    Non-Postgres backends have no such function; a closed session or a driver
    error degrades to ``None`` rather than raising — the public contract of
    :func:`read_rls_org`. The WHOLE read (``execute`` AND the ``scalar()`` fetch)
    is inside the guard: a fetch-time failure (server-side cursor, connection
    closed mid-fetch) must honour this contract too, not escape.
    """
    try:
        result = await session.execute(text("SELECT current_setting('app.organisation_id', true)"))
        raw: object = result.scalar()
    except Exception:
        return None
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    return _coerce_org_id(raw)


def _coerce_org_id(value: object) -> uuid.UUID | None:
    """Return a ``UUID`` when *value* is a valid UUID/str, else ``None``."""
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


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

    @event.listens_for(engine.sync_engine, "checkout")
    def _reset_org_on_checkout(
        dbapi_connection: object,
        _connection_record: object,
        _connection_proxy: object,
    ) -> None:
        try:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                for config_name in _RLS_CONFIG_NAMES:
                    cursor.execute(_set_config_statement(config_name))
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


def _operation_label(execute_state: ORMExecuteState) -> str:
    """Human label for the statement kind in the missing-context debug log."""
    if execute_state.is_select:
        return "select/update/delete"
    if execute_state.is_update:
        return "update/delete"
    return "delete"


def _log_missing_org_context(execute_state: ORMExecuteState) -> None:
    """Debug-log a scoped statement executed with no org context set."""
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return
    _log.debug(
        "_inject_tenant_filter: no org_id in session.info for %s on %s",
        _operation_label(execute_state),
        execute_state.statement,
    )


def _apply_entity_tenant_filter(stmt: Any, entity: Any, org_id: Any) -> tuple[Any, bool]:
    """Add the tenant predicate for one entity; return (statement, applied)."""
    if entity is None or entity is object:
        return stmt, False
    if not hasattr(entity, _TENANT_COLUMN):
        return stmt, False
    return stmt.where(getattr(entity, _TENANT_COLUMN) == org_id), True


def _inject_from_column_descriptions(
    stmt: Any,
    execute_state: ORMExecuteState,
    org_id: Any,
) -> tuple[Any, bool]:
    """Inject for every entity exposed via ``column_descriptions`` (SELECTs)."""
    if not hasattr(stmt, "column_descriptions"):
        return stmt, False
    injected = False
    for desc in stmt.column_descriptions:
        stmt, applied = _apply_entity_tenant_filter(stmt, desc.get("entity"), org_id)
        injected = injected or applied
    return stmt, injected


def _inject_from_mapper_classes(
    stmt: Any,
    execute_state: ORMExecuteState,
    org_id: Any,
) -> tuple[Any, bool]:
    """Inject for every entity exposed via ``all_mapper_classes`` (UPDATE/DELETE)."""
    if not (hasattr(execute_state, "all_mapper_classes") and execute_state.all_mapper_classes):
        return stmt, False
    injected = False
    for mapper in execute_state.all_mapper_classes:
        stmt, applied = _apply_entity_tenant_filter(stmt, mapper.class_, org_id)
        injected = injected or applied
    return stmt, injected


def _inject_tenant_filter(execute_state: ORMExecuteState) -> None:
    """ORM execute listener that injects ``WHERE organisation_id = :oid``.

    Reads ``org_id`` from ``session.info`` (set by ``set_rls_org``) and adds
    the WHERE clause to every SELECT, UPDATE, and DELETE statement targeting
    a model that has an ``organisation_id`` column.
    """
    org_id = execute_state.session.info.get(_TENANT_KEY)
    if org_id is None:
        _log_missing_org_context(execute_state)
        return

    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return

    stmt = execute_state.statement

    # SELECT / bulk operations expose entities via column_descriptions.
    stmt, injected = _inject_from_column_descriptions(stmt, execute_state, org_id)

    # ORM UPDATE/DELETE expose entities via all_mapper_classes.
    if not injected:
        stmt, injected = _inject_from_mapper_classes(stmt, execute_state, org_id)

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
