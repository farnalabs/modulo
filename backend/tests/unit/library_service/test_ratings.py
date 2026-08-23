"""Unit tests for rating CRUD — validations, abuse reports, aggregate."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud.base import PageResult
from modulo.db.crud.rating import (
    CopyToAdaptError,
    DuplicateRatingError,
    RatingCooldownError,
    SelfRatingError,
    get_rating_aggregate,
    list_abuse_reports,
    list_ratings_for_primitive,
    review_abuse_report,
    submit_abuse_report,
    submit_rating,
    update_primitive_ratings_aggregate,
)
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.primitive_abuse_report import PrimitiveAbuseReport
from modulo.db.models.primitive_rating import PrimitiveRating


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def _given_execute(session, *return_values):
    """Set up session.execute to return given values sequentially.

    Each return_value can be:
    - A MagicMock: used directly as the execute result (must have scalar_one etc.)
    - A list: wrapped in a result with .scalars().all() = list
    - An int: wrapped in a result with .scalar_one() = int
    - None: wrapped in a result with .scalar_one_or_none() = None
    """
    results = []
    for rv in return_values:
        if isinstance(rv, MagicMock):
            m = rv  # Use as-is (already a result mock)
        else:
            m = MagicMock()
            if isinstance(rv, list):
                m.scalars.return_value.all = MagicMock(return_value=rv)
            elif isinstance(rv, int):
                m.scalar_one.return_value = rv
            elif rv is None:
                m.scalar_one_or_none.return_value = None
        results.append(m)

    async def execute_side(*args, **kwargs):
        if results:
            return results.pop(0)
        fallback = MagicMock()
        fallback.scalar_one_or_none.return_value = None
        return fallback

    session.execute = execute_side


# ---------------------------------------------------------------------------
# Validation guard tests
# ---------------------------------------------------------------------------


class TestSelfRatingGuard:
    async def test_blocks_self_rating(self, mock_session):
        """A user cannot rate their own primitive."""
        user_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = MagicMock(spec=LibraryPrimitive)
        prim.account_id = user_id

        # Execute result that returns the LibraryPrimitive via scalar_one_or_none
        result = MagicMock()
        result.scalar_one_or_none.return_value = prim
        _given_execute(mock_session, result)

        with pytest.raises(SelfRatingError, match="own primitive"):
            await submit_rating(
                mock_session,
                org_id=uuid.uuid4(),
                primitive_id=prim_id,
                thumbs_up=True,
                account_id=user_id,
            )

    async def test_allows_rating_others_primitive(self, mock_session):
        """Rating another user's primitive should be allowed (after other guards)."""
        user_id = uuid.uuid4()
        prim = MagicMock(spec=LibraryPrimitive)
        prim.account_id = uuid.uuid4()  # different user

        # Set up: self-rating query (returns prim), duplicate count (0), cooldown count (0), copy count (1)
        result = MagicMock()
        result.scalar_one_or_none.return_value = prim
        count_0 = MagicMock()
        count_0.scalar_one.return_value = 0
        count_1 = MagicMock()
        count_1.scalar_one.return_value = 1

        _given_execute(mock_session, result, count_0, count_0, count_1)

        result = await submit_rating(
            mock_session,
            org_id=uuid.uuid4(),
            primitive_id=prim.id,
            thumbs_up=True,
            account_id=user_id,
        )
        assert isinstance(result, PrimitiveRating)


class TestDuplicateRatingGuard:
    async def test_blocks_duplicate_rating(self, mock_session):
        """A user cannot rate a primitive they have already rated."""
        user_id = uuid.uuid4()
        prim = MagicMock(spec=LibraryPrimitive)
        prim.account_id = uuid.uuid4()

        result = MagicMock()
        result.scalar_one_or_none.return_value = prim
        count_1 = MagicMock()
        count_1.scalar_one.return_value = 1  # a rating already exists

        _given_execute(mock_session, result, count_1)

        with pytest.raises(DuplicateRatingError, match="already rated"):
            await submit_rating(
                mock_session,
                org_id=uuid.uuid4(),
                primitive_id=uuid.uuid4(),
                thumbs_up=True,
                account_id=user_id,
            )


class TestCooldownGuard:
    async def test_blocks_rapid_rating(self, mock_session):
        """User cannot rate the same primitive within 10 minutes."""
        user_id = uuid.uuid4()
        prim = MagicMock(spec=LibraryPrimitive)
        prim.account_id = uuid.uuid4()

        result = MagicMock()
        result.scalar_one_or_none.return_value = prim
        count_0 = MagicMock()
        count_0.scalar_one.return_value = 0
        count_1 = MagicMock()
        count_1.scalar_one.return_value = 1  # recent rating exists

        _given_execute(mock_session, result, count_0, count_1)

        with pytest.raises(RatingCooldownError, match="wait"):
            await submit_rating(
                mock_session,
                org_id=uuid.uuid4(),
                primitive_id=uuid.uuid4(),
                thumbs_up=True,
                account_id=user_id,
            )


class TestCopyToAdaptGuard:
    async def test_blocks_without_copy(self, mock_session):
        """User must copy a primitive before rating it."""
        user_id = uuid.uuid4()
        prim = MagicMock(spec=LibraryPrimitive)
        prim.account_id = uuid.uuid4()

        result = MagicMock()
        result.scalar_one_or_none.return_value = prim
        count_0 = MagicMock()
        count_0.scalar_one.return_value = 0
        count_0b = MagicMock()
        count_0b.scalar_one.return_value = 0  # no copy made

        _given_execute(mock_session, result, count_0, count_0, count_0b)

        with pytest.raises(CopyToAdaptError, match="copy"):
            await submit_rating(
                mock_session,
                org_id=uuid.uuid4(),
                primitive_id=uuid.uuid4(),
                thumbs_up=True,
                account_id=user_id,
            )

    async def test_allows_after_copy(self, mock_session):
        """User who has copied the primitive can rate it."""
        user_id = uuid.uuid4()
        prim = MagicMock(spec=LibraryPrimitive)
        prim.account_id = uuid.uuid4()

        result = MagicMock()
        result.scalar_one_or_none.return_value = prim
        count_0 = MagicMock()
        count_0.scalar_one.return_value = 0
        count_1 = MagicMock()
        count_1.scalar_one.return_value = 1  # has copy

        _given_execute(mock_session, result, count_0, count_0, count_1)

        result = await submit_rating(
            mock_session,
            org_id=uuid.uuid4(),
            primitive_id=prim.id,
            thumbs_up=True,
            account_id=user_id,
        )
        assert isinstance(result, PrimitiveRating)


# ---------------------------------------------------------------------------
# Aggregate tests
# ---------------------------------------------------------------------------


class TestGetRatingAggregate:
    async def test_no_ratings(self, mock_session):
        prim_id = uuid.uuid4()
        count_mock = MagicMock()
        count_mock.scalar_one.return_value = 0
        _given_execute(mock_session, count_mock)

        avg, count = await get_rating_aggregate(mock_session, prim_id)
        assert avg is None
        assert count == 0

    async def test_all_thumbs_up(self, mock_session):
        prim_id = uuid.uuid4()
        total_mock = MagicMock()
        total_mock.scalar_one.return_value = 5
        thumbs_mock = MagicMock()
        thumbs_mock.scalar_one.return_value = 5
        _given_execute(mock_session, total_mock, thumbs_mock)

        avg, count = await get_rating_aggregate(mock_session, prim_id)
        assert avg == 5
        assert count == 5

    async def test_mixed_ratings(self, mock_session):
        prim_id = uuid.uuid4()
        total_mock = MagicMock()
        total_mock.scalar_one.return_value = 10
        thumbs_mock = MagicMock()
        thumbs_mock.scalar_one.return_value = 7
        _given_execute(mock_session, total_mock, thumbs_mock)

        avg, count = await get_rating_aggregate(mock_session, prim_id)
        assert avg == 3.5  # 7/10 * 5
        assert count == 10


class TestUpdateAggregate:
    async def test_updates_primitive_record(self, mock_session):
        prim_id = uuid.uuid4()
        total_mock = MagicMock()
        total_mock.scalar_one.return_value = 2
        thumbs_mock = MagicMock()
        thumbs_mock.scalar_one.return_value = 1

        prim = MagicMock(spec=LibraryPrimitive)
        prim.average_rating = None
        prim.review_count = None

        prim_result = MagicMock()
        prim_result.scalar_one_or_none.return_value = prim

        _given_execute(mock_session, total_mock, thumbs_mock, prim_result)

        await update_primitive_ratings_aggregate(mock_session, prim_id)
        assert prim.average_rating == 2.5
        assert prim.review_count == 2


# ---------------------------------------------------------------------------
# List ratings
# ---------------------------------------------------------------------------


class TestListRatings:
    async def test_list_ratings_empty(self, mock_session):
        prim_id = uuid.uuid4()
        count_mock = MagicMock()
        count_mock.scalar_one.return_value = 0
        result_mock = MagicMock()
        result_mock.scalars.return_value = []
        _given_execute(mock_session, count_mock, result_mock)

        ratings = await list_ratings_for_primitive(mock_session, prim_id)
        assert isinstance(ratings, PageResult)
        assert not ratings.items
        assert ratings.total == 0

    async def test_list_ratings_with_results(self, mock_session):
        prim_id = uuid.uuid4()
        rating_1 = MagicMock(spec=PrimitiveRating)
        rating_1.id = uuid.uuid4()
        rating_2 = MagicMock(spec=PrimitiveRating)
        rating_2.id = uuid.uuid4()

        count_mock = MagicMock()
        count_mock.scalar_one.return_value = 2
        result_mock = MagicMock()
        result_mock.scalars.return_value = [rating_1, rating_2]
        _given_execute(mock_session, count_mock, result_mock)

        ratings = await list_ratings_for_primitive(mock_session, prim_id)
        assert len(ratings.items) == 2
        assert ratings.total == 2


# ---------------------------------------------------------------------------
# Abuse report tests
# ---------------------------------------------------------------------------


class TestSubmitAbuseReport:
    async def test_creates_report(self, mock_session):
        async def execute_side(*args, **kwargs):
            return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        mock_session.execute = execute_side

        org_id = uuid.uuid4()
        primitive_id = uuid.uuid4()
        rating_id = uuid.uuid4()
        reporter_account_id = uuid.uuid4()

        report = await submit_abuse_report(
            mock_session,
            org_id=org_id,
            primitive_id=primitive_id,
            rating_id=rating_id,
            reporter_account_id=reporter_account_id,
            reason="This rating is inappropriate",
        )
        assert isinstance(report, PrimitiveAbuseReport)
        assert report.status == "pending"
        mock_session.add.assert_called_once()
        added = mock_session.add.call_args.args[0]
        assert added.organisation_id == org_id
        assert added.primitive_id == primitive_id
        assert added.rating_id == rating_id
        assert added.reporter_account_id == reporter_account_id
        assert added.reason == "This rating is inappropriate"
        assert added.status == "pending"
        mock_session.flush.assert_awaited_once()


class TestListAbuseReports:
    async def test_lists_reports(self, mock_session):
        report = MagicMock(spec=PrimitiveAbuseReport)
        report.id = uuid.uuid4()
        org_id = uuid.uuid4()

        count_mock = MagicMock()
        count_mock.scalar_one.return_value = 1
        result_mock = MagicMock()
        result_mock.scalars.return_value = [report]
        _given_execute(mock_session, count_mock, result_mock)

        reports = await list_abuse_reports(mock_session, org_id)
        assert len(reports.items) == 1
        assert reports.total == 1


class TestReviewAbuseReport:
    async def test_review_dismisses(self, mock_session):
        report_id = uuid.uuid4()
        report = MagicMock(spec=PrimitiveAbuseReport)
        report.status = "pending"
        report.reviewer_account_id = None
        report.reviewed_at = None

        result = MagicMock()
        result.scalar_one_or_none.return_value = report
        _given_execute(mock_session, result)

        result = await review_abuse_report(
            mock_session, report_id, new_status="dismissed", reviewer_account_id=uuid.uuid4()
        )
        assert result is not None
        assert result.status == "dismissed"

    async def test_review_missing_report(self, mock_session):
        _given_execute(mock_session, None)

        result = await review_abuse_report(mock_session, uuid.uuid4(), new_status="dismissed")
        assert result is None
