"""Unit tests pinning the runs-list query column deferral (Runs-page timeout fix).

``db_list_runs`` backs GET /api/v1/runs, the MCP list_runs tool, the MCP
``modulo://pipelines/{id}/runs`` resource, and the viewmodel RunSummary. The
page SELECT must NOT load the heavy per-run payload columns
(``crud.run._RUNS_LIST_DEFERRED_COLUMNS``) — on sandbox-heavy orgs each can
carry megabytes of outputs/telemetry, and no list consumer reads them.

Pinned here:

* every deferred column is absent from the compiled list SELECT (and from the
  count SELECT — which selects no run columns at all);
* the contract column the responses DO read stays selected: ``input_payload``
  (masked into every REST list item);
* ``cost_breakdown`` IS deferred: its only list-path reader is the MCP
  pipeline-runs resource, which now loads it through an awaited query
  (``crud.run.get_run_cost_breakdowns``) rather than reading the attribute off
  the ORM instance — under asyncio a deferred attribute read raises
  ``MissingGreenlet`` even while the session is open;
* the deferral tuple contains only heavy JSON/Text payload columns, so a future
  edit cannot quietly defer a light column a consumer needs.

They run without a database (mock session, statement capture).
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import JSON, Text

from modulo.db.crud.pagination import CursorPaginator
from modulo.db.crud.run import _RUNS_LIST_DEFERRED_COLUMNS
from modulo.db.crud.run import list_runs as db_list_runs
from modulo.db.models.run import Run

_MUST_STAY_LOADED = ("input_payload",)

# Both pagination modes must defer: the offset/limit page path and the keyset
# cursor path (the cursor branch rebuilds the statement through
# CursorPaginator, so the deferral options must survive it too).
_CURSOR = CursorPaginator.encode_cursor(datetime(2026, 9, 1, tzinfo=UTC), uuid.UUID(int=1))
_PAGINATION_MODES = (
    pytest.param({}, id="offset-page"),
    pytest.param({"cursor": _CURSOR}, id="keyset-cursor"),
)


def _compile(stmt: Any) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _make_capturing_session() -> tuple[AsyncMock, list[Any]]:
    """A mock session whose execute() captures every statement it is given."""
    session = AsyncMock()
    captured: list[Any] = []
    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = 0
    rows_result = MagicMock()
    # The offset path iterates .scalars() directly; the keyset-cursor path
    # calls .scalars().all(). MagicMock is iterable (empty by default) and the
    # explicit .all.return_value covers the cursor path.
    rows_result.scalars.return_value.all.return_value = []

    async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
        captured.append(stmt)
        if "count(" in str(stmt).lower():
            return count_result
        return rows_result

    session.execute = fake_execute
    return session, captured


def _items_statement(captured: list[Any]) -> Any:
    return next(stmt for stmt in captured if "count(" not in str(stmt).lower())


@pytest.mark.parametrize("pagination_kwargs", _PAGINATION_MODES)
@pytest.mark.parametrize("column", _RUNS_LIST_DEFERRED_COLUMNS)
async def test_list_runs_select_omits_heavy_payload_column(pagination_kwargs: dict[str, Any], column: str) -> None:
    session, captured = _make_capturing_session()

    page = await db_list_runs(session, page=1, page_size=20, **pagination_kwargs)

    assert page.total == 0
    assert captured, "list_runs must execute its queries through the session"
    items_sql = _compile(_items_statement(captured)).lower()
    assert f"runs.{column}" not in items_sql, (
        f"the runs-list SELECT must not load heavy payload column {column!r} — "
        "no list consumer reads it and detoasting it dominates the page latency"
    )


@pytest.mark.parametrize("pagination_kwargs", _PAGINATION_MODES)
async def test_list_runs_select_keeps_contract_columns(pagination_kwargs: dict[str, Any]) -> None:
    session, captured = _make_capturing_session()

    await db_list_runs(session, page=1, page_size=20, **pagination_kwargs)

    items_sql = _compile(_items_statement(captured)).lower()
    for column in _MUST_STAY_LOADED:
        assert f"runs.{column}" in items_sql, (
            f"the runs-list SELECT must keep loading {column!r} — list responses read it"
        )


def test_deferred_columns_are_heavy_payload_columns_only() -> None:
    """The deferral tuple must only ever name JSON/Text payload columns."""
    for name in _RUNS_LIST_DEFERRED_COLUMNS:
        column = Run.__table__.columns[name]
        assert isinstance(column.type, (JSON, Text)), (
            f"{name!r} is not a heavy payload column (type {column.type!r}) — "
            "only megabyte-scale JSON/Text columns belong in _RUNS_LIST_DEFERRED_COLUMNS"
        )


def test_deferral_excludes_consumer_read_columns() -> None:
    """input_payload must never land in the deferral tuple."""
    assert not set(_MUST_STAY_LOADED) & set(_RUNS_LIST_DEFERRED_COLUMNS)


def test_cost_breakdown_is_deferred() -> None:
    """cost_breakdown is deferred — its list-path reader loads it via an awaited query.

    Prove-the-fix anchor: the parametrised SQL-omission test above only covers
    whatever columns the tuple currently names, so this pins the membership
    explicitly — a revert of the crud change (dropping cost_breakdown from the
    tuple) must fail here.
    """
    assert "cost_breakdown" in _RUNS_LIST_DEFERRED_COLUMNS
