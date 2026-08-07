"""Shared builders for the rate_limiter test package.

Each test module previously re-implemented the Settings builder with slightly
different shapes. Keeping it here means a change to the Settings contract only
has to be made once. Fixtures (``mock_redis``, ``_isolate_module_state``) live
in ``conftest.py`` — pytest only auto-applies fixtures from conftest files and
test modules, so defining them here was dead code that silently duplicated the
conftest versions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modulo.settings import Settings

BASE_SETTINGS: dict[str, object] = {
    "database_url": "postgresql+asyncpg://localhost/test",
    "secret_key": "a" * 32,
    "fernet_key": "a" * 32,
    "modulo_admin_password": "testpass",
    "redis_url": "redis://localhost:6379/0",
}


def make_settings(**overrides: object) -> Settings:
    """Build a Settings instance, overriding any field via keyword args."""
    base = dict(BASE_SETTINGS)
    base.update(overrides)
    return Settings(**base)


def make_redis_client(*, auth: bool = False) -> MagicMock:
    """Build a MagicMock Redis client with a chainable transactional pipeline.

    ``auth=False`` configures the pipeline for the sliding-window limiter
    (execute returns a 4-tuple: zrem/zadd/zcard/expire); ``auth=True``
    configures it for AuthRateLimiter.check_login (execute returns a 2-tuple).
    """
    client = MagicMock()
    client.ttl = AsyncMock(return_value=0)
    pipe = MagicMock()
    pipe.zremrangebyscore = MagicMock(return_value=pipe)
    pipe.zadd = MagicMock(return_value=pipe)
    pipe.zcard = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.delete = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=(None, None, 1, True) if not auth else (None, 0))
    client.pipeline = MagicMock(return_value=pipe)
    client.zadd = AsyncMock()
    client.expire = AsyncMock()
    client.delete = AsyncMock()
    client.setex = AsyncMock()
    return client
