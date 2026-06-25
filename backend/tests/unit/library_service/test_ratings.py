"""Unit tests for rating CRUD."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud.base import PageResult
from modulo.db.crud.rating import (
    get_rating_aggregate,
    list_ratings_for_primitive,
    submit_rating,
)
from modulo.db.models.primitive_rating import PrimitiveRating


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.flush = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


class TestSubmitRating:
    async def test_submit_rating_creates_record(self, mock_session):
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        result = await submit_rating(
            mock_session,
            org_id=org_id,
            primitive_id=prim_id,
            thumbs_up=True,
            comment="Great workflow!",
            user_id=uuid.uuid4(),
        )
        assert isinstance(result, PrimitiveRating)
        assert result.thumbs_up is True
        assert result.comment == "Great workflow!"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()

    async def test_submit_rating_no_comment(self, mock_session):
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        result = await submit_rating(
            mock_session,
            org_id=org_id,
            primitive_id=prim_id,
            thumbs_up=False,
        )
        assert result.thumbs_up is False
        assert result.comment is None


class TestGetRatingAggregate:
    async def test_no_ratings(self, mock_session):
        prim_id = uuid.uuid4()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=count_result)

        avg, count = await get_rating_aggregate(mock_session, prim_id)
        assert avg is None
        assert count == 0

    async def test_all_thumbs_up(self, mock_session):
        prim_id = uuid.uuid4()
        count_result = MagicMock()
        count_result.scalar_one.side_effect = [5, 5]
        mock_session.execute = AsyncMock(return_value=count_result)

        avg, count = await get_rating_aggregate(mock_session, prim_id)
        assert avg == Decimal("5")
        assert count == 5


class TestListRatings:
    async def test_list_ratings_empty(self, mock_session):
        prim_id = uuid.uuid4()
        count_mock = MagicMock()
        count_mock.scalar_one.return_value = 0
        result_mock = MagicMock()
        result_mock.scalars.return_value = []
        mock_session.execute = AsyncMock(side_effect=[count_mock, result_mock])

        ratings = await list_ratings_for_primitive(mock_session, prim_id)
        assert isinstance(ratings, PageResult)
        assert ratings.items == []
        assert ratings.total == 0
