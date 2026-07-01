"""Tests for engine pool configuration in api/dependencies.py.

These tests verify that get_or_create_engine passes the correct pool
parameters to SQLAlchemy's create_async_engine for each DB backend.
"""

from unittest.mock import patch

import pytest


class TestEnginePoolConfig:
    def test_passes_postgres_pool_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "postgres")

        from modulo.api import dependencies as deps

        deps._engine = None
        with patch.object(deps, "create_async_engine") as mock_create:
            from modulo.settings import Settings

            settings = Settings()
            deps.get_or_create_engine(settings)

        _call_kwargs = mock_create.call_args.kwargs
        assert _call_kwargs["pool_pre_ping"] is True
        assert _call_kwargs["pool_size"] == 10
        assert _call_kwargs["max_overflow"] == 5
        assert _call_kwargs["pool_timeout"] == 10

    def test_skips_pool_size_for_sqlite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "sqlite")

        from modulo.api import dependencies as deps

        deps._engine = None
        with patch.object(deps, "create_async_engine") as mock_create:
            from modulo.settings import Settings

            settings = Settings()
            deps.get_or_create_engine(settings)

        _call_kwargs = mock_create.call_args.kwargs
        assert _call_kwargs["pool_pre_ping"] is True
        assert "pool_size" not in _call_kwargs
        assert "max_overflow" not in _call_kwargs
        assert "pool_timeout" not in _call_kwargs
