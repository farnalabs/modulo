"""Tests for community_library.py narrowed exception handlers (FAR-882).

Verifies that:
1. SQLAlchemyError is still caught (fail-open for DB unavailability) in list_community.
2. Unexpected exception types propagate as 500 (fail-closed for programming errors).
3. _fetch_entry_content catches ValueError but propagates unexpected errors.
4. get_entry catches (ValueError, KeyError) but propagates unexpected errors.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError


class TestListCommunityExceptionNarrowing:
    """FAR-882: list_community catches SQLAlchemyError (fail-open) but lets
    unexpected errors propagate as 500."""

    @pytest.mark.asyncio
    @patch("modulo.api.routes.community_library.get_cached_manifest", new_callable=AsyncMock)
    @patch(
        "modulo.api.routes.community_library.list_community_entries",
        new_callable=AsyncMock,
        side_effect=SQLAlchemyError("db down"),
    )
    async def test_db_error_degrades_to_empty(self, mock_entries: AsyncMock, mock_manifest: AsyncMock) -> None:
        """SQLAlchemyError from list_community_entries -> returns empty list (fail-open)."""
        from modulo.api.routes.community_library import list_community

        session = AsyncMock()
        principal = MagicMock()
        principal.organisation_id = "00000000-0000-0000-0000-000000000001"

        result = await list_community(session, principal)
        assert not result["items"]
        assert result["total"] == 0
        # Suppress unused variable warnings — these are injected by @patch
        assert mock_entries is not None
        assert mock_manifest is not None

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library.list_community_entries",
        new_callable=AsyncMock,
        side_effect=RuntimeError("unexpected"),
    )
    async def test_unexpected_error_propagates(self, mock_entries: AsyncMock) -> None:
        """RuntimeError from list_community_entries -> propagates (fail-closed)."""
        from modulo.api.routes.community_library import list_community

        session = AsyncMock()
        principal = MagicMock()
        principal.organisation_id = "00000000-0000-0000-0000-000000000001"

        with pytest.raises(RuntimeError, match="unexpected"):
            await list_community(session, principal)
        assert mock_entries is not None


class TestFetchEntryContentExceptionNarrowing:
    """FAR-882: _fetch_entry_content catches ValueError (JSON parse) but lets
    unexpected errors propagate."""

    @pytest.mark.asyncio
    @patch("modulo.api.routes.community_library.get_settings")
    @patch("modulo.api.routes.community_library.LibraryClient")
    async def test_value_error_returns_none(self, mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
        """ValueError from blob parsing -> returns None (fail-open)."""
        mock_settings.return_value = MagicMock(
            modulo_library_endpoint="http://localhost",
            modulo_library_root_public_key="key",
            modulo_library_sync_timeout_seconds=15,
        )
        mock_client = AsyncMock()
        mock_client.fetch_blob = AsyncMock(side_effect=ValueError("bad json"))
        mock_client.close = AsyncMock()
        mock_client_cls.return_value = mock_client

        from modulo.api.routes.community_library import _fetch_entry_content

        result = await _fetch_entry_content("abc123")
        assert result is None

    @pytest.mark.asyncio
    @patch("modulo.api.routes.community_library.get_settings")
    @patch("modulo.api.routes.community_library.LibraryClient")
    async def test_unexpected_error_propagates(self, mock_client_cls: MagicMock, mock_settings: MagicMock) -> None:
        """RuntimeError from blob fetch -> propagates (fail-closed)."""
        mock_settings.return_value = MagicMock(
            modulo_library_endpoint="http://localhost",
            modulo_library_root_public_key="key",
            modulo_library_sync_timeout_seconds=15,
        )
        mock_client = AsyncMock()
        mock_client.fetch_blob = AsyncMock(side_effect=RuntimeError("unexpected"))
        mock_client.close = AsyncMock()
        mock_client_cls.return_value = mock_client

        from modulo.api.routes.community_library import _fetch_entry_content

        with pytest.raises(RuntimeError, match="unexpected"):
            await _fetch_entry_content("abc123")


class TestGetEntryExceptionNarrowing:
    """FAR-882: get_entry catches (ValueError, KeyError) but lets unexpected
    errors propagate as 500."""

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library.get_community_entry",
        new_callable=AsyncMock,
        side_effect=ValueError("bad manifest"),
    )
    async def test_value_error_returns_none(self, mock_entry: AsyncMock) -> None:
        """ValueError from get_community_entry -> entry is None -> raises 404."""
        from fastapi import HTTPException

        from modulo.api.routes.community_library import get_entry

        session = AsyncMock()
        principal = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_entry("nonexistent", session, principal)
        assert exc_info.value.status_code == 404
        assert mock_entry is not None

    @pytest.mark.asyncio
    @patch(
        "modulo.api.routes.community_library.get_community_entry",
        new_callable=AsyncMock,
        side_effect=RuntimeError("unexpected"),
    )
    async def test_unexpected_error_propagates(self, mock_entry: AsyncMock) -> None:
        """RuntimeError from get_community_entry -> propagates (fail-closed)."""
        from modulo.api.routes.community_library import get_entry

        session = AsyncMock()
        principal = MagicMock()

        with pytest.raises(RuntimeError, match="unexpected"):
            await get_entry("nonexistent", session, principal)
        assert mock_entry is not None
