"""Unit tests for the modulo_system engine selection in modulo.core.saq_worker.

Proves that _get_system_async_engine creates an engine from
MODULO_SYSTEM_DATABASE_URL when set, and logs a WARNING + falls back to the app
engine (modulo_app, NOBYPASSRLS) when it is not set — the silent RLS-zero-rows
data-loss posture.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import modulo.core.saq_worker as sw


def _settings(system_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        modulo_system_database_url=system_url,
        modulo_db="postgres",
        saq_worker_db_pool_size=7,
        saq_worker_concurrency=2,
    )


@pytest.fixture
def reset_system_engine(monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level _SYSTEM_ASYNC_ENGINE singleton before and after."""
    monkeypatch.setattr(sw, "_SYSTEM_ASYNC_ENGINE", None)
    yield
    sw._SYSTEM_ASYNC_ENGINE = None


def test_creates_engine_with_system_url_when_configured(reset_system_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    system_url = "postgresql+asyncpg://modulo_system:s3cret@db.internal:5432/modulo"
    monkeypatch.setattr(sw, "get_settings", lambda: _settings(system_url))
    create_engine = MagicMock(return_value=MagicMock())
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", create_engine)

    engine = sw._get_system_async_engine()

    assert engine is create_engine.return_value
    create_engine.assert_called_once()
    assert create_engine.call_args.args[0] == system_url


def test_logs_warning_and_uses_app_engine_when_unset(reset_system_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    app_engine = MagicMock()
    monkeypatch.setattr(sw, "get_settings", lambda: _settings(system_url=""))
    monkeypatch.setattr(sw, "_get_async_engine", lambda: app_engine)
    warnings: list[tuple[object, object]] = []
    monkeypatch.setattr(sw._log, "warning", lambda msg, extra=None: warnings.append((msg, extra)))

    engine = sw._get_system_async_engine()

    assert engine is app_engine
    assert len(warnings) == 1
    msg, extra = warnings[0]
    assert msg == "saq_worker.system_engine_fallback"
    assert "MODULO_SYSTEM_DATABASE_URL not set" in extra["reason"]
