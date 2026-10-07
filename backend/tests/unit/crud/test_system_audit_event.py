"""Unit tests for the ``system_audit_events`` read helper (FAR-1538).

No Postgres: a capturing fake session records every statement the helper
builds, so the assertions are against the REAL SQLAlchemy statement the helper
emits - the filters, the ordering, the offset/limit and the total count - not
against a mock of the helper itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from modulo.db.crud.system_audit_event import (
    LIST_DEFAULT_PAGE_SIZE,
    LIST_MAX_PAGE_SIZE,
    list_system_audit_events,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-000000000002")


class _BeginCtx:
    def __init__(self, session: _CaptureSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        self._session.in_tx = True

    async def __aexit__(self, *_exc: object) -> bool:
        self._session.in_tx = False
        return False


class _CaptureSession:
    """Fake async session that records statements and replays a canned page."""

    def __init__(self, rows: list[Any] | None = None, total: int = 0) -> None:
        self.rows = rows or []
        self.total = total
        self.statements: list[Any] = []
        self.in_tx = False

    def begin(self) -> _BeginCtx:
        return _BeginCtx(self)

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
        assert self.in_tx, "execute() ran outside session.begin() (autobegin=False)"
        self.statements.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.total
        result.scalars.return_value.all.return_value = self.rows
        return result


def _sql(stmt: Any) -> str:
    return str(stmt)


def _params(stmt: Any) -> dict[str, Any]:
    return dict(stmt.compile().params)


def _sql_literal(stmt: Any) -> str:
    """Render bind params inline so LIMIT/OFFSET show their actual values."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


async def _run(session: _CaptureSession, **kwargs: Any) -> tuple[list[Any], int]:
    """Call the helper the way the route does - inside the caller's transaction.

    The DI session factory is ``autobegin=False``, so the helper only ever runs
    under an explicit ``session.begin()``; the fake session asserts that.
    """
    async with session.begin():
        return await list_system_audit_events(session, **kwargs)


async def test_returns_rows_and_total() -> None:
    row = MagicMock()
    session = _CaptureSession(rows=[row], total=7)

    rows, total = await _run(session)

    assert rows == [row]
    assert total == 7
    # One COUNT statement + one page statement, both inside the caller's tx.
    assert len(session.statements) == 2
    assert "COUNT" in _sql(session.statements[0]).upper()


async def test_no_filters_builds_an_unfiltered_query() -> None:
    session = _CaptureSession()

    await _run(session)

    page_sql = _sql(session.statements[1])
    assert "system_audit_events" in page_sql
    assert "WHERE" not in page_sql


async def test_event_type_filter_reaches_the_where_clause() -> None:
    session = _CaptureSession()

    await _run(session, event_type="org_deletion_completed")

    page_stmt = session.statements[1]
    assert "system_audit_events.event_type =" in _sql(page_stmt)
    assert "org_deletion_completed" in _params(page_stmt).values()


async def test_org_id_filter_reaches_the_where_clause() -> None:
    session = _CaptureSession()

    await _run(session, org_id=_ORG)

    page_stmt = session.statements[1]
    assert "system_audit_events.org_id =" in _sql(page_stmt)
    assert _ORG in _params(page_stmt).values()
    # The count query honours the same filter, so `total` matches the page.
    assert _ORG in _params(session.statements[0]).values()


async def test_date_range_filter_reaches_both_bounds() -> None:
    session = _CaptureSession()
    from_date = datetime(2026, 1, 1, tzinfo=UTC)
    to_date = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)

    await _run(session, from_date=from_date, to_date=to_date)

    page_stmt = session.statements[1]
    page_sql = _sql(page_stmt)
    assert "system_audit_events.created_at >=" in page_sql
    assert "system_audit_events.created_at <=" in page_sql
    bound = set(_params(page_stmt).values())
    assert from_date in bound
    assert to_date in bound


async def test_no_date_bounds_when_omitted() -> None:
    session = _CaptureSession()

    await _run(session, from_date=None, to_date=None)

    page_sql = _sql(session.statements[1])
    assert "created_at >=" not in page_sql
    assert "created_at <=" not in page_sql


async def test_rows_are_ordered_newest_first_with_a_stable_tiebreak() -> None:
    session = _CaptureSession()

    await _run(session)

    page_sql = _sql(session.statements[1])
    assert "ORDER BY system_audit_events.created_at DESC, system_audit_events.id DESC" in page_sql


async def test_page_size_is_clamped_to_the_maximum() -> None:
    session = _CaptureSession()

    await _run(session, page_size=LIST_MAX_PAGE_SIZE + 500)

    page_sql = _sql_literal(session.statements[1])
    assert f"LIMIT {LIST_MAX_PAGE_SIZE}" in page_sql
    assert "OFFSET 0" in page_sql


async def test_page_size_is_clamped_to_at_least_one() -> None:
    session = _CaptureSession()

    await _run(session, page_size=0)

    assert "LIMIT 1" in _sql_literal(session.statements[1])


async def test_page_one_offsets_by_zero() -> None:
    session = _CaptureSession()

    await _run(session, page=1, page_size=LIST_DEFAULT_PAGE_SIZE)

    page_sql = _sql_literal(session.statements[1])
    assert f"LIMIT {LIST_DEFAULT_PAGE_SIZE}" in page_sql
    assert "OFFSET 0" in page_sql


async def test_page_n_offsets_by_a_full_page() -> None:
    session = _CaptureSession()

    await _run(session, page=3, page_size=25)

    page_sql = _sql_literal(session.statements[1])
    assert "LIMIT 25" in page_sql
    assert "OFFSET 50" in page_sql


async def test_page_below_one_is_clamped_to_the_first_page() -> None:
    session = _CaptureSession()

    await _run(session, page=0, page_size=10)

    page_sql = _sql_literal(session.statements[1])
    assert "LIMIT 10" in page_sql
    assert "OFFSET 0" in page_sql
