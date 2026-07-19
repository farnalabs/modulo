"""Unit tests for api/dependencies.py — engine creation, session management, plan context."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError


class TestGetOrCreateEngine:
    """Test engine creation with pool config."""

    def setup_method(self):
        import modulo.api.dependencies as deps

        deps._engine = None

    def test_engine_with_postgres_pool_config(self):
        """Postgres backends get pool_pre_ping, pool_size, max_overflow, pool_recycle, pool_timeout."""
        from modulo.api.dependencies import get_or_create_engine

        settings = MagicMock()
        settings.modulo_db = "postgres"
        settings.database_url = "postgresql+asyncpg://u:p@localhost/db"

        with patch("modulo.api.dependencies.create_async_engine") as mock_create:
            get_or_create_engine(settings)
            mock_create.assert_called_once()
            kw = mock_create.call_args[1]
            assert kw["pool_pre_ping"] is True
            assert kw["pool_size"] == 20
            assert kw["max_overflow"] == 10
            assert kw["pool_recycle"] == 3600
            assert kw["pool_timeout"] == 30
            assert kw["connect_args"]["timeout"] == 10

    def test_engine_with_sqlite_skips_pool_config(self):
        """SQLite backends skip pool settings (not supported by pysqlite)."""
        from modulo.api.dependencies import get_or_create_engine

        settings = MagicMock()
        settings.modulo_db = "sqlite"
        settings.database_url = "sqlite+aiosqlite:///./test.db"

        with patch("modulo.api.dependencies.create_async_engine") as mock_create:
            get_or_create_engine(settings)
            kw = mock_create.call_args[1]
            assert "pool_size" not in kw
            assert "max_overflow" not in kw
            assert "pool_recycle" not in kw
            assert "pool_timeout" not in kw
            assert kw["pool_pre_ping"] is True

    def test_engine_connect_args_includes_timeout_for_all_backends(self):
        """All backends get connect_args with timeout=10."""
        from modulo.api.dependencies import get_or_create_engine

        settings = MagicMock()
        settings.modulo_db = "sqlite"
        settings.database_url = "sqlite+aiosqlite:///./test.db"

        with patch("modulo.api.dependencies.create_async_engine") as mock_create:
            get_or_create_engine(settings)
            kw = mock_create.call_args[1]
            assert kw["connect_args"]["timeout"] == 10

    def test_engine_is_singleton(self):
        """get_or_create_engine returns the same engine on second call."""
        import modulo.api.dependencies as deps
        from modulo.api.dependencies import get_or_create_engine

        deps._engine = None

        settings = MagicMock()
        settings.modulo_db = "postgres"
        settings.database_url = "postgresql+asyncpg://u:p@localhost/db"

        with patch("modulo.api.dependencies.create_async_engine") as mock_create:
            engine1 = get_or_create_engine(settings)
            engine2 = get_or_create_engine(settings)
            mock_create.assert_called_once()
            assert engine1 is engine2


class TestGetDbSession:
    """Test get_db_session async generator."""

    @pytest.mark.asyncio
    async def test_yields_session_on_success(self):
        """Happy path: session is yielded and usable."""
        from modulo.api.dependencies import get_db_session

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        gen = get_db_session(factory=mock_factory)
        session = await gen.__anext__()
        assert session is mock_session

        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    @pytest.mark.asyncio
    async def test_programming_error_converted_to_501(self):
        """ProgrammingError raised during session use is converted to HTTPException 501."""
        from fastapi import HTTPException

        from modulo.api.dependencies import get_db_session

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        gen = get_db_session(factory=mock_factory)
        await gen.__anext__()

        with pytest.raises(HTTPException) as exc_info:
            await gen.athrow(ProgrammingError("stmt", "params", "orig"))
        assert exc_info.value.status_code == 501
        assert "Run database migrations" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_non_programming_error_propagates(self):
        """Non-ProgrammingError exceptions propagate through unchanged."""
        from modulo.api.dependencies import get_db_session

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        gen = get_db_session(factory=mock_factory)
        await gen.__anext__()

        with pytest.raises(RuntimeError, match="test error"):
            await gen.athrow(RuntimeError("test error"))


class TestPgConnectionString:
    """Test pg_connection_string URL transformation."""

    def test_strips_asyncpg_prefix(self):
        from modulo.api.dependencies import pg_connection_string

        result = pg_connection_string("postgresql+asyncpg://user:pass@localhost/db")
        assert result.startswith("postgresql://user:pass@localhost/db")

    def test_strips_psycopg_prefix(self):
        from modulo.api.dependencies import pg_connection_string

        result = pg_connection_string("postgresql+psycopg://user:pass@localhost/db")
        assert result == "postgresql://user:pass@localhost/db?sslmode=disable"

    def test_preserves_sslmode(self):
        from modulo.api.dependencies import pg_connection_string

        result = pg_connection_string("postgresql+asyncpg://user:pass@localhost/db?sslmode=require")
        assert result == "postgresql://user:pass@localhost/db?sslmode=require"

    def test_noop_for_plain_postgresql(self):
        from modulo.api.dependencies import pg_connection_string

        result = pg_connection_string("postgresql://user:pass@localhost/db")
        assert result == "postgresql://user:pass@localhost/db?sslmode=disable"
