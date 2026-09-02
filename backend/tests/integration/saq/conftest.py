"""Shared fixtures for SAQ integration tests (real Redis + testcontainers Postgres)."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from testcontainers.community.redis import RedisContainer


@pytest.fixture(scope="session")
def redis_container() -> Generator[RedisContainer, None, None]:
    with RedisContainer("redis:7-alpine") as rc:
        yield rc


@pytest.fixture(scope="session")
def saq_redis_url(redis_container: RedisContainer) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture
def saq_settings_env(saq_redis_url: str, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point settings at the testcontainers Postgres + Redis for one test."""
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("REDIS_URL", saq_redis_url)
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)
    monkeypatch.setenv("MODULO_ADMIN_PASSWORD", "test")

    from modulo.settings import get_settings

    get_settings.cache_clear()

    # Force the module-level engines to rebuild against the test DB/Redis.
    import modulo.core.cron_helpers as ch
    import modulo.core.dispatch as dispatch

    ch._ENGINE = None
    dispatch._ENGINE = None

    yield saq_redis_url

    ch._ENGINE = None
    dispatch._ENGINE = None
    get_settings.cache_clear()
