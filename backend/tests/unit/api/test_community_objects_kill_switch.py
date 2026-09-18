"""Tests for the org-level community-objects kill switch (FAR-959).

Verifies:
1. When community_objects_enabled is OFF, list/get/install return 403.
2. When community_objects_enabled is ON (default), endpoints work unchanged.
3. The flag read is fail-open (errors leave community objects enabled).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status


class TestCommunityObjectsGateListCommunity:
    """FAR-959: list_community returns 403 when the kill switch is OFF."""

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library._community_objects_enabled",
        new_callable=AsyncMock,
        return_value=False,
    )
    async def test_disabled_returns_403(self, mock_flag) -> None:
        from modulo.api.routes.community_library import list_community

        session = AsyncMock()
        principal = AsyncMock()
        principal.organisation_id = "00000000-0000-0000-0000-000000000001"

        with pytest.raises(HTTPException) as exc_info:
            await list_community(session, principal)
        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "disabled" in exc_info.value.detail.lower()
        assert mock_flag is not None

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library.get_cached_manifest",
        new_callable=AsyncMock,
    )
    @patch(
        "modulo.api.routes.community_library.list_community_entries",
        new_callable=AsyncMock,
        return_value=[],
    )
    @patch(
        "modulo.api.routes.community_library._community_objects_enabled",
        new_callable=AsyncMock,
        return_value=True,
    )
    async def test_enabled_proceeds_normally(self, mock_flag, mock_entries, mock_manifest) -> None:
        from modulo.api.routes.community_library import list_community

        session = AsyncMock()
        principal = AsyncMock()
        principal.organisation_id = "00000000-0000-0000-0000-000000000001"

        result = await list_community(session, principal)
        assert not result["items"]
        assert result["total"] == 0
        assert mock_flag is not None


class TestCommunityObjectsGateGetEntry:
    """FAR-959: get_entry returns 403 when the kill switch is OFF."""

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library._community_objects_enabled",
        new_callable=AsyncMock,
        return_value=False,
    )
    async def test_disabled_returns_403(self, mock_flag) -> None:
        from modulo.api.routes.community_library import get_entry

        session = AsyncMock()
        principal = AsyncMock()
        principal.organisation_id = "00000000-0000-0000-0000-000000000001"

        with pytest.raises(HTTPException) as exc_info:
            await get_entry("some-entry-id", session, principal)
        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "disabled" in exc_info.value.detail.lower()
        assert mock_flag is not None


class TestCommunityObjectsGateInstall:
    """FAR-959: install returns 403 when the kill switch is OFF."""

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library._community_objects_enabled",
        new_callable=AsyncMock,
        return_value=False,
    )
    async def test_disabled_returns_403(self, mock_flag) -> None:
        from modulo.api.routes.community_library import InstallRequest, install

        session = AsyncMock()
        principal = AsyncMock()
        principal.organisation_id = "00000000-0000-0000-0000-000000000001"

        with pytest.raises(HTTPException) as exc_info:
            await install("some-entry-id", InstallRequest(), session, principal)
        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "disabled" in exc_info.value.detail.lower()
        assert mock_flag is not None


class TestCommunityObjectsFlagFailOpen:
    """FAR-959: flag read failures leave community objects enabled (fail-open)."""

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library.read_org_flag",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db down"),
    )
    async def test_flag_read_error_returns_true(self, mock_flag) -> None:
        from modulo.api.routes.community_library import _community_objects_enabled

        session = AsyncMock()
        result = await _community_objects_enabled(session, "org-id")
        assert result is True
        assert mock_flag is not None


class TestCommunityObjectsFlagOwnsTransaction:
    """Regression: the flag read must not leave an implicit transaction open.

    ``read_org_flag`` issues a SELECT (SQLAlchemy autobegin). The install
    route then opens ``async with session.begin()`` on the same session; a
    pre-existing transaction there raises ``InvalidRequestError: A transaction
    is already begun on this Session``, surfaced to the client as a 503.
    """

    @pytest.mark.asyncio
    async def test_read_leaves_session_transaction_free(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from modulo.api.routes.community_library import _community_objects_enabled

        async def _autobegin_read(session, org_id, flag_name, *, default):
            # A real SELECT autobegins a transaction, exactly like read_org_flag.
            await session.execute(text("SELECT 1"))
            return default

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                with patch(
                    "modulo.api.routes.community_library.read_org_flag",
                    new=_autobegin_read,
                ):
                    assert await _community_objects_enabled(session, uuid.uuid4()) is True
                assert session.in_transaction() is False
                # The install route's own transaction can now begin cleanly.
                async with session.begin():
                    await session.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
