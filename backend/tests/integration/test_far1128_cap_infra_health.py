"""FAR-1128 cap-infra-health: health dependency checks against real infra.

Two real-world modes, no mocks:

* a real reachable Postgres (the Testcontainers ``postgres_container``
  integration fixture) — the database health check reports ``ok``;
* a dependency pointing at a genuinely closed loopback port (no Redis
  listener) — the Redis dependency check performs a real socket connect that
  is refused and reports ``degraded`` with the real "unreachable" state.

The first test is Docker-dependent and gated by the suite's integration gate;
the Redis test needs no container but lives here as a transport-level
integration check.
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from modulo.api.routes.health import _check_database, _check_redis
from modulo.settings import get_settings

pytestmark = pytest.mark.integration


def _unlistened_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestCapInfraHealth:
    async def test_database_health_ok_against_real_postgres(self, migrated_db_url: str) -> None:
        engine = create_async_engine(migrated_db_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                row = result.fetchone()
            assert row is not None
        finally:
            await engine.dispose()

        get_settings.cache_clear()
        result = await _check_database()
        assert result.status == "ok"
        assert result.detail == "database reachable"

    async def test_unreachable_redis_dependency_reports_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        port = _unlistened_loopback_port()
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/modulo_integration")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        monkeypatch.setenv("MODULO_ADMIN_PASSWORD", "test")
        monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
        monkeypatch.setenv("MODULO_HEALTH_REDIS_TIMEOUT_SECONDS", "2")
        get_settings.cache_clear()

        result = await _check_redis()

        assert result.status == "degraded"
        assert result.detail == "redis unreachable"
