"""Tests for _verify_db_connectivity retry logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.main import _verify_db_connectivity
from modulo.settings import Settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


class TestDbConnectivity:
    @pytest.mark.asyncio
    async def test_db_connectivity_passes(self) -> None:
        settings = _make_settings()
        engine = MagicMock()
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=None)
        engine.connect = MagicMock(return_value=conn)

        with patch("modulo.api.main.get_or_create_engine", return_value=engine) as mock_engine:
            await _verify_db_connectivity(settings)
            mock_engine.assert_called_once_with(settings)
            conn.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_connectivity_retries_without_blocking_startup(self) -> None:
        settings = _make_settings()
        engine = MagicMock()
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(side_effect=ConnectionError("db down"))
        conn.__aexit__ = AsyncMock(return_value=None)
        engine.connect = MagicMock(return_value=conn)

        with (
            patch("modulo.api.main.get_or_create_engine", return_value=engine) as mock_engine,
            patch("modulo.api.main.asyncio.sleep") as mock_sleep,
        ):
            await _verify_db_connectivity(settings)
            mock_engine.assert_called_once_with(settings)
            assert engine.connect.call_count == 3
            assert mock_sleep.await_count == 2
            assert conn.execute.await_count == 0
