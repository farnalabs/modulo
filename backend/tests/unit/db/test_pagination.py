"""CursorPaginator.paginate cursor-value validation (malformed-cursor 422 path).

A cursor payload that decodes cleanly (``"<value>:<valid-uuid>"``) but whose
value is not of the sort column's type must be rejected as a client error
(ValueError -> 422 via the route mapping) BEFORE the DB query, instead of
failing the keyset tuple comparison at the database (503).
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.pipeline import Pipeline


def _make_session() -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session.execute.return_value = execute_result
    return session


async def test_paginate_rejects_non_date_value_for_datetime_cursor() -> None:
    """A well-formed cursor with a non-date value on a datetime column is a client error."""
    session = _make_session()
    paginator = CursorPaginator(sort_field="created_at", sort_dir="desc")
    cursor = CursorPaginator.encode_cursor("not-a-date", uuid.uuid4())

    with pytest.raises(ValueError, match="Invalid cursor value"):
        await paginator.paginate(session, select(Pipeline), cursor=cursor, model=Pipeline)

    assert session.execute.await_count == 0


async def test_paginate_accepts_valid_datetime_cursor() -> None:
    """A legitimate created_at cursor still passes validation and reaches the DB."""
    session = _make_session()
    paginator = CursorPaginator(sort_field="created_at", sort_dir="desc")
    cursor = CursorPaginator.encode_cursor(datetime(2026, 9, 1, tzinfo=UTC), uuid.uuid4())

    page = await paginator.paginate(session, select(Pipeline), cursor=cursor, model=Pipeline)

    assert not page.items
    assert session.execute.await_count == 1


async def test_paginate_string_cursor_is_not_validated_as_datetime() -> None:
    """Non-datetime sort columns (e.g. eval definitions ordered by name) keep working."""
    session = _make_session()
    paginator = CursorPaginator(sort_field="name", sort_dir="asc")
    cursor = CursorPaginator.encode_cursor("some-name-value", uuid.uuid4())

    page = await paginator.paginate(
        session,
        select(Pipeline),
        cursor=cursor,
        model=Pipeline,
        sort_field="name",
        sort_dir="asc",
    )

    assert not page.items
    assert session.execute.await_count == 1
