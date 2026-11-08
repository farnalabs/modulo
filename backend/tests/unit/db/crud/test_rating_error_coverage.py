"""Real async-session error and payload branches for rating CRUD."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import ProgrammingError

from modulo.db.crud.base import PageResult
from modulo.db.crud.rating import (
    get_rating_aggregate,
    list_abuse_reports,
    list_ratings_for_primitive,
    submit_rating,
    update_primitive_ratings_aggregate,
)


def _session_with_results(*results: MagicMock) -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(side_effect=list(results))
    return session


def _programming_error() -> ProgrammingError:
    return ProgrammingError("missing table", {}, RuntimeError("missing table"))


@pytest.mark.anyio
async def test_submit_rating_without_account_skips_guards_and_persists_payload() -> None:
    session = _session_with_results()
    org_id = uuid.uuid4()
    primitive_id = uuid.uuid4()
    rating = await submit_rating(
        session,
        org_id=org_id,
        primitive_id=primitive_id,
        thumbs_up=False,
        comment="Useful but incomplete",
    )
    assert rating.organisation_id == org_id
    assert rating.primitive_id == primitive_id
    assert rating.account_id is None
    assert rating.thumbs_up is False
    assert rating.comment == "Useful but incomplete"
    session.flush.assert_awaited_once()


@pytest.mark.anyio
async def test_rating_readers_fail_safe_on_programming_errors() -> None:
    aggregate_session = _session_with_results()
    aggregate_session.execute = AsyncMock(side_effect=_programming_error())
    assert await get_rating_aggregate(aggregate_session, uuid.uuid4()) == (None, 0)

    list_session = _session_with_results()
    list_session.execute = AsyncMock(side_effect=_programming_error())
    ratings = await list_ratings_for_primitive(list_session, uuid.uuid4(), page=3, page_size=4)
    assert isinstance(ratings, PageResult)
    assert not ratings.items
    assert ratings.page == 3
    assert ratings.page_size == 4

    abuse_session = _session_with_results()
    abuse_session.execute = AsyncMock(side_effect=_programming_error())
    reports = await list_abuse_reports(abuse_session, uuid.uuid4(), status_filter="pending", page=2, page_size=5)
    assert isinstance(reports, PageResult)
    assert not reports.items
    assert reports.page == 2
    assert reports.page_size == 5


@pytest.mark.anyio
async def test_rating_aggregate_updates_only_when_primitive_exists() -> None:
    missing = MagicMock()
    missing.scalar_one_or_none.return_value = None
    count = MagicMock()
    count.scalar_one.return_value = 0
    session = _session_with_results(count, missing)
    await update_primitive_ratings_aggregate(session, uuid.uuid4())
    session.flush.assert_not_awaited()


@pytest.mark.anyio
async def test_rating_list_has_more_when_second_page_exists() -> None:
    count = MagicMock(scalar_one=MagicMock(return_value=3))
    ratings = MagicMock()
    ratings.scalars.return_value = [MagicMock(), MagicMock()]
    session = _session_with_results(count, ratings)
    result = await list_ratings_for_primitive(session, uuid.uuid4(), page=1, page_size=2)
    assert result.has_more is True
    assert len(result.items) == 2
