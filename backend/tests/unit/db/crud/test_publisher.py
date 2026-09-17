"""Unit tests for Publisher CRUD (mocked session)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.crud.publisher import (
    TRUST_TIER_AMBER,
    TRUST_TIER_GREEN,
    create_publisher,
    delete_publisher,
    get_publisher,
    get_publisher_by_key,
    get_publisher_by_name,
    list_publishers,
    update_publisher,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PUB_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _mock_publisher(**overrides: object) -> MagicMock:
    pub = MagicMock()
    pub.id = overrides.get("id", _PUB_ID)
    pub.name = overrides.get("name", "pub-a")
    pub.contact_email = overrides.get("contact_email", "a@example.com")
    pub.public_key_hex = overrides.get("public_key_hex", "ab" * 32)
    pub.trust_tier = overrides.get("trust_tier", TRUST_TIER_AMBER)
    pub.verified_since = overrides.get("verified_since")
    pub.website_url = overrides.get("website_url")
    pub.organisation_id = overrides.get("org_id", _ORG_ID)
    return pub


def _exec_scalar_one_or_none(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _exec_scalars(items: list[object]) -> MagicMock:
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=items)
    result.scalars = MagicMock(return_value=scalars_mock)
    return result


def _exec_count(value: int) -> MagicMock:
    """Mock for ``result.scalar()`` used by list_publishers count query."""
    result = MagicMock()
    result.scalar = MagicMock(return_value=value)
    return result


def _exec_list_result(count: int, items: list[object]) -> MagicMock:
    """Mock for the two sequential execute calls in list_publishers.

    list_publishers calls:
      1. (await session.execute(count_q)).scalar()  → uses .scalar()
      2. list((await session.execute(items_stmt)).scalars())  → iterates .scalars() directly
    """
    result = MagicMock()
    result.scalar = MagicMock(return_value=count)
    result.scalars = MagicMock(return_value=items)
    return result


# ── get_publisher ──────────────────────────────────────────────────


class TestGetPublisher:
    async def test_returns_publisher(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        result = await get_publisher(mock_session, _PUB_ID, org_id=_ORG_ID)

        assert result is pub

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await get_publisher(mock_session, uuid.uuid4(), org_id=_ORG_ID)

        assert result is None


# ── get_publisher_by_key ──────────────────────────────────────────


class TestGetPublisherByKey:
    async def test_returns_publisher(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        result = await get_publisher_by_key(mock_session, _ORG_ID, "ab" * 32)

        assert result is pub

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await get_publisher_by_key(mock_session, _ORG_ID, "xx" * 32)

        assert result is None


# ── get_publisher_by_name ─────────────────────────────────────────


class TestGetPublisherByName:
    async def test_returns_publisher(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        result = await get_publisher_by_name(mock_session, _ORG_ID, "pub-a")

        assert result is pub

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await get_publisher_by_name(mock_session, _ORG_ID, "nonexistent")

        assert result is None


# ── list_publishers ───────────────────────────────────────────────


class TestListPublishers:
    async def test_returns_page_result(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher()
        # First call: count query uses .scalar(). Second call: items query uses .scalars() iterable.
        mock_session.execute = AsyncMock(side_effect=[_exec_count(1), MagicMock(scalars=MagicMock(return_value=[pub]))])

        result = await list_publishers(mock_session, org_id=_ORG_ID)

        assert isinstance(result, PageResult)
        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0] is pub
        assert result.page == 1
        assert result.page_size == 20

    async def test_empty_result(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_exec_count(0), MagicMock(scalars=MagicMock(return_value=[]))])

        result = await list_publishers(mock_session, org_id=_ORG_ID)

        assert result.total == 0
        assert not result.items

    async def test_trust_tier_filter(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_exec_count(0), MagicMock(scalars=MagicMock(return_value=[]))])

        await list_publishers(mock_session, org_id=_ORG_ID, trust_tier=TRUST_TIER_GREEN)

        assert mock_session.execute.await_count == 2

    async def test_search_filter(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_exec_count(0), MagicMock(scalars=MagicMock(return_value=[]))])

        await list_publishers(mock_session, org_id=_ORG_ID, search="test")

        assert mock_session.execute.await_count == 2

    async def test_search_whitespace_only_ignored(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_exec_count(0), MagicMock(scalars=MagicMock(return_value=[]))])

        await list_publishers(mock_session, org_id=_ORG_ID, search="   ")

        assert mock_session.execute.await_count == 2

    async def test_custom_pagination(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_exec_count(5), MagicMock(scalars=MagicMock(return_value=[]))])

        result = await list_publishers(mock_session, org_id=_ORG_ID, page=2, page_size=2)

        assert result.page == 2
        assert result.page_size == 2

    async def test_programming_error_returns_empty(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))

        result = await list_publishers(mock_session, org_id=_ORG_ID)

        assert not result.items
        assert result.total == 0


# ── create_publisher ──────────────────────────────────────────────


class TestCreatePublisher:
    async def test_creates_amber_publisher(self, mock_session: AsyncMock) -> None:
        await create_publisher(
            mock_session,
            org_id=_ORG_ID,
            name="new-pub",
            contact_email="pub@example.com",
            public_key_hex="ab" * 32,
            trust_tier=TRUST_TIER_AMBER,
        )

        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        added = mock_session.add.call_args[0][0]
        assert added.trust_tier == TRUST_TIER_AMBER
        assert added.verified_since is None

    async def test_creates_green_publisher_with_verified_since(self, mock_session: AsyncMock) -> None:
        await create_publisher(
            mock_session,
            org_id=_ORG_ID,
            name="green-pub",
            contact_email=None,
            public_key_hex="cd" * 32,
            trust_tier=TRUST_TIER_GREEN,
            website_url="https://example.com",
        )

        added = mock_session.add.call_args[0][0]
        assert added.trust_tier == TRUST_TIER_GREEN
        assert added.verified_since is not None
        assert added.website_url == "https://example.com"

    async def test_raises_on_invalid_trust_tier(self, mock_session: AsyncMock) -> None:
        with pytest.raises(ValueError, match="Invalid trust_tier"):
            await create_publisher(
                mock_session,
                org_id=_ORG_ID,
                name="bad",
                contact_email=None,
                public_key_hex="ab" * 32,
                trust_tier="invalid",
            )

    async def test_optional_fields_default_none(self, mock_session: AsyncMock) -> None:
        await create_publisher(
            mock_session,
            org_id=_ORG_ID,
            name="minimal",
            contact_email=None,
            public_key_hex="ef" * 32,
        )

        added = mock_session.add.call_args[0][0]
        assert added.contact_email is None
        assert added.website_url is None


# ── update_publisher ──────────────────────────────────────────────


class TestUpdatePublisher:
    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await update_publisher(mock_session, uuid.uuid4(), {}, org_id=_ORG_ID)

        assert result is None

    async def test_updates_name(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        result = await update_publisher(mock_session, _PUB_ID, {"name": "new-name"}, org_id=_ORG_ID)

        assert result is pub
        assert pub.name == "new-name"
        mock_session.flush.assert_awaited_once()

    async def test_tier_change_to_green_sets_verified_since(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher(trust_tier=TRUST_TIER_AMBER, verified_since=None)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        await update_publisher(mock_session, _PUB_ID, {"trust_tier": TRUST_TIER_GREEN}, org_id=_ORG_ID)

        assert pub.verified_since is not None

    async def test_tier_change_from_green_to_amber_clears_verified_since(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher(trust_tier=TRUST_TIER_GREEN, verified_since=datetime.now(UTC))
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        await update_publisher(mock_session, _PUB_ID, {"trust_tier": TRUST_TIER_AMBER}, org_id=_ORG_ID)

        assert pub.verified_since is None

    async def test_green_to_green_preserves_verified_since(self, mock_session: AsyncMock) -> None:
        existing_time = datetime(2025, 1, 1, tzinfo=UTC)
        pub = _mock_publisher(trust_tier=TRUST_TIER_GREEN, verified_since=existing_time)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        await update_publisher(mock_session, _PUB_ID, {"trust_tier": TRUST_TIER_GREEN}, org_id=_ORG_ID)

        # When tier is already green and stays green, verified_since is not updated
        assert pub.verified_since == existing_time

    async def test_raises_on_invalid_trust_tier(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        with pytest.raises(ValueError, match="Invalid trust_tier"):
            await update_publisher(mock_session, _PUB_ID, {"trust_tier": "bad"}, org_id=_ORG_ID)

    async def test_non_trust_tier_update_skips_tier_logic(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher(verified_since=None)
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        await update_publisher(mock_session, _PUB_ID, {"name": "updated"}, org_id=_ORG_ID)

        assert pub.verified_since is None


# ── delete_publisher ──────────────────────────────────────────────


class TestDeletePublisher:
    async def test_deletes_existing(self, mock_session: AsyncMock) -> None:
        pub = _mock_publisher()
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(pub))

        result = await delete_publisher(mock_session, _PUB_ID, org_id=_ORG_ID)

        assert result is True
        mock_session.delete.assert_called_once_with(pub)
        mock_session.flush.assert_awaited_once()

    async def test_returns_false_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalar_one_or_none(None))

        result = await delete_publisher(mock_session, uuid.uuid4(), org_id=_ORG_ID)

        assert result is False
        mock_session.delete.assert_not_called()
