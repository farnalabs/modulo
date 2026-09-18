"""Tests for modulo.api.main._verify_db_connectivity retry logic.

QA lens pass (correctness, bugs, maintainability, deps) on the startup DB
connectivity probe. The probe must never block application startup: it retries
up to three times with linear backoff, succeeds on the first working attempt,
and logs a clear final warning if the database stays unreachable. The previous
tests covered the happy path and the give-up path; this pass locks the
backoff schedule, the succeed-on-retry path, and the log contract.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.main import _verify_db_connectivity
from modulo.settings import Settings

_LOGGER = "modulo.api.main"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _mock_engine(failing_attempts: int = 0) -> tuple[MagicMock, AsyncMock]:
    """Return an engine whose connect succeeds on the first call by default.

    ``failing_attempts`` simulates a DB that comes back after N failed
    connect attempts.
    """
    engine = MagicMock()
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)

    def _enter() -> AsyncMock:
        return conn

    conn.__aenter__.side_effect = [ConnectionError("db down")] * failing_attempts + [_enter()]

    engine.connect = MagicMock(return_value=conn)
    return engine, conn


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
            # Must return normally (not raise) after exhausting retries.
            await _verify_db_connectivity(settings)
            mock_engine.assert_called_once_with(settings)
            assert engine.connect.call_count == 3
            assert mock_sleep.await_count == 2
            assert conn.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_sleeps_linear_backoff_between_retries(self) -> None:
        settings = _make_settings()
        engine = MagicMock()
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(side_effect=ConnectionError("db down"))
        conn.__aexit__ = AsyncMock(return_value=None)
        engine.connect = MagicMock(return_value=conn)

        with (
            patch("modulo.api.main.get_or_create_engine", return_value=engine),
            patch("modulo.api.main.asyncio.sleep") as mock_sleep,
        ):
            await _verify_db_connectivity(settings)

        # Backoff is linear: attempt 1 -> sleep 2s, attempt 2 -> sleep 4s.
        assert [call.args[0] for call in mock_sleep.await_args_list] == [2, 4]

    @pytest.mark.asyncio
    async def test_succeeds_on_retry_after_transient_failure(self) -> None:
        settings = _make_settings()
        engine, conn = _mock_engine(failing_attempts=2)

        with (
            patch("modulo.api.main.get_or_create_engine", return_value=engine),
            patch("modulo.api.main.asyncio.sleep") as mock_sleep,
        ):
            await _verify_db_connectivity(settings)

        assert engine.connect.call_count == 3
        assert conn.execute.await_count == 1
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self) -> None:
        settings = _make_settings()
        engine, conn = _mock_engine(failing_attempts=1)

        with (
            patch("modulo.api.main.get_or_create_engine", return_value=engine),
            patch("modulo.api.main.asyncio.sleep") as mock_sleep,
        ):
            await _verify_db_connectivity(settings)

        assert engine.connect.call_count == 2
        assert conn.execute.await_count == 1
        assert mock_sleep.await_count == 1

    @pytest.mark.asyncio
    async def test_logs_connected_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings()
        engine, _ = _mock_engine(failing_attempts=1)

        with (
            patch("modulo.api.main.get_or_create_engine", return_value=engine),
            patch("modulo.api.main.asyncio.sleep"),
            caplog.at_level(logging.INFO, logger=_LOGGER),
        ):
            await _verify_db_connectivity(settings)

        assert any("startup.db_connected" in rec.message for rec in caplog.records)
        assert not any("startup.db_unreachable" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_logs_unreachable_after_giving_up(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings()
        engine, _ = _mock_engine(failing_attempts=3)

        with (
            patch("modulo.api.main.get_or_create_engine", return_value=engine),
            patch("modulo.api.main.asyncio.sleep"),
            caplog.at_level(logging.INFO, logger=_LOGGER),
        ):
            await _verify_db_connectivity(settings)

        assert any("startup.db_unreachable" in rec.message for rec in caplog.records)
        assert any("startup.continuing_without_db" in rec.message for rec in caplog.records)
        assert not any("startup.db_connected" in rec.message for rec in caplog.records)
