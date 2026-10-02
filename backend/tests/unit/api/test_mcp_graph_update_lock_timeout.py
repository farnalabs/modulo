"""FAR-1361: the MCP graph-update transaction's row-lock wait must be BOUNDED.

``_update_pipeline_graph_impl`` opens its OWN transaction (``mcp_server._session``)
and never runs the REST layer's in-txn team gate, so before FAR-1361 the graph
write's ``SELECT ... FOR UPDATE`` (``replace_pipeline_graph``, whose FIRST
statement is that lock) ran with NO ``lock_timeout`` at all: a contended MCP
graph write parked a pooled connection on the row lock until the holder
committed - exactly the unbounded wait the REST mutation endpoints refuse
(``_set_mutation_row_lock_timeout``, FAR-1313).

Four things are pinned here against a session double reporting the
``postgresql`` dialect (so the helper's dialect gate takes its live branch):

1. **WIRING + ORDER** - the real
   ``SELECT set_config('lock_timeout', ...)`` is issued BEFORE the real
   ``replace_pipeline_graph`` takes its ``FOR UPDATE``. The double records the
   SQL and raises a sentinel at the lock, so the statement ORDER is asserted
   against the real service function, not a stand-in for it.
2. **MAPPING** - a 55P03 (``lock_not_available``) from that lock degrades to a
   structured ``lock_timeout`` error dict instead of the generic "Failed to
   update pipeline graph" the tool wrapper would otherwise return. Catching it
   INSIDE the impl also keeps it out of ``_RETRY_DB`` (which retries
   ``OperationalError`` three times with backoff and would otherwise multiply
   the bounded wait).
3. **ESCALATION** - a ``ProgrammingError`` from the same lock still reaches the
   wrapper's ``migration_required`` arm, so the new handler is a targeted
   55P03 map, not a swallow of every SQLAlchemy error.
4. **THE CONDITION ITSELF** - a different SQLSTATE (40001, serialization
   failure) is re-raised, never reported as "another change is in progress".

The real-Postgres contention behaviour (a held row lock actually timing out
within ``Settings.mutation_row_lock_timeout_ms``) is covered by
``tests/integration/test_mcp_graph_update_lock_timeout.py``.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from modulo.api import mcp_server as ms

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class _StopAtLockError(Exception):
    """Sentinel raised by the session double at the FOR UPDATE, after recording it."""


def _pipeline_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.owner_team_id = None
    row.graph_nodes_json = []
    return row


def _session_double(*, lock_error: BaseException | None = None) -> tuple[AsyncMock, list[str]]:
    """AsyncSession double reporting the ``postgresql`` dialect.

    Records every statement so the test can assert ORDER, and raises
    *lock_error* (a ``_StopAtLockError`` sentinel by default) the moment the graph
    write issues its ``SELECT ... FOR UPDATE`` on ``pipelines``.
    """
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}
    recorded: list[str] = []
    row = _pipeline_row()

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        recorded.append(sql)
        if "FOR UPDATE" in sql.upper() and "FROM pipelines" in sql:
            raise lock_error if lock_error is not None else _StopAtLockError()
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        result.first.return_value = None
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session, recorded


@asynccontextmanager
async def _fake_session(session: AsyncMock):
    """Stand-in for ``mcp_server._session`` yielding the double above."""
    yield session


def _set_context() -> tuple[Any, Any, Any]:
    """Set the MCP request context the impl reads (org + role + user)."""
    return (
        ms._ctx_org_id.set(_ORG_ID),
        ms._ctx_role.set("admin"),
        ms._ctx_user_id.set(_USER_ID),
    )


def _reset_context(tokens: tuple[Any, Any, Any]) -> None:
    ms._ctx_org_id.reset(tokens[0])
    ms._ctx_role.reset(tokens[1])
    ms._ctx_user_id.reset(tokens[2])


def _patches(session: AsyncMock) -> list:
    """Everything the impl needs around the session, for both call shapes."""
    return [
        patch.object(ms, "_session", lambda _org_id: _fake_session(session)),
        patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
        patch.object(ms, "_check_agent_tool_scope"),
        patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=_pipeline_row())),
        patch("modulo.core.team_visibility.find_connector_team_mismatches", new=AsyncMock(return_value=[])),
    ]


async def test_graph_update_sets_the_bounded_lock_timeout_before_its_first_lock() -> None:
    """WIRING + ORDER: ``set_config('lock_timeout', ...)`` precedes the FOR UPDATE.

    Both statements come from the REAL code: the helper issues the bound, and
    ``replace_pipeline_graph`` (unpatched) issues the lock. The double raises a
    sentinel at that lock, so nothing past it runs - the assertion is purely
    about the order of what the transaction actually executed.
    """
    session, recorded = _session_double()
    tokens = _set_context()
    try:
        with ExitStack() as stack:
            for p in _patches(session):
                stack.enter_context(p)
            with pytest.raises(_StopAtLockError):
                await ms._update_pipeline_graph_impl(str(uuid.uuid4()), [], [])
    finally:
        _reset_context(tokens)

    bound_at = [i for i, sql in enumerate(recorded) if "lock_timeout" in sql]
    locked_at = [i for i, sql in enumerate(recorded) if "FOR UPDATE" in sql.upper()]
    assert bound_at, f"no bounded lock_timeout statement was issued; statements={recorded}"
    assert locked_at, f"the graph write never took its FOR UPDATE; statements={recorded}"
    assert bound_at[0] < locked_at[0], (
        f"the bound must be set BEFORE the first lock (lock_timeout at {bound_at[0]}, FOR UPDATE at {locked_at[0]})"
    )


async def test_graph_update_lock_timeout_degrades_to_a_structured_error() -> None:
    """A 55P03 from the graph write answers ``lock_timeout``, not a generic error."""
    driver_error = SimpleNamespace(sqlstate="55P03")
    session, _recorded = _session_double(lock_error=OperationalError("SELECT ... FOR UPDATE", {}, driver_error))
    tokens = _set_context()
    try:
        with ExitStack() as stack:
            for p in _patches(session):
                stack.enter_context(p)
            result = await ms.update_pipeline_graph(pipeline_id=str(uuid.uuid4()), nodes=[], edges=[])
    finally:
        _reset_context(tokens)

    assert result["error"] == "lock_timeout", result
    assert "another change is in progress" in result["detail"]
    # Not the generic tool failure the wrapper would otherwise return.
    assert result["detail"] != "Failed to update pipeline graph"


async def test_graph_update_other_db_errors_still_escape_the_impl() -> None:
    """A ProgrammingError from the same lock reaches the wrapper's migration arm.

    If the new ``except SQLAlchemyError`` swallowed every SQLAlchemy error,
    this would come back as the generic tool error instead.
    """
    session, _recorded = _session_double(lock_error=ProgrammingError("SELECT ... FOR UPDATE", {}, Exception()))
    tokens = _set_context()
    try:
        with ExitStack() as stack:
            for p in _patches(session):
                stack.enter_context(p)
            result = await ms.update_pipeline_graph(pipeline_id=str(uuid.uuid4()), nodes=[], edges=[])
    finally:
        _reset_context(tokens)

    assert result["error"] == "migration_required", result


async def test_graph_update_does_not_map_an_unrelated_sqlstate() -> None:
    """The map is conditional on 55P03: 40001 is re-raised, never reported as a lock.

    Driven through ``_impl`` directly (not the ``@_RETRY_DB`` wrapper) so the
    assertion is the re-raise itself rather than the wrapper's generic arm -
    the wrapper would retry this ``OperationalError`` three times with backoff.
    """
    driver_error = SimpleNamespace(sqlstate="40001")
    session, _recorded = _session_double(lock_error=OperationalError("SELECT ... FOR UPDATE", {}, driver_error))
    tokens = _set_context()
    try:
        with ExitStack() as stack:
            for p in _patches(session):
                stack.enter_context(p)
            with pytest.raises(OperationalError):
                await ms._update_pipeline_graph_impl(str(uuid.uuid4()), [], [])
    finally:
        _reset_context(tokens)
