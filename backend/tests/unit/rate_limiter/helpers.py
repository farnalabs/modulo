"""Shared fixtures and builders for the rate_limiter test package.

Each test module previously re-implemented the Settings builder and the
module-level state isolation fixture with slightly different shapes. Keeping
them here means a change to the Settings contract or to how the middleware
tracks its module globals only has to be made once.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.api.middleware import rate_limiter as rl_mod
from modulo.api.middleware.rate_limiter import RateLimitMiddleware
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


@pytest.fixture
def mock_redis() -> MagicMock:
    """Auth-flavoured Redis mock used by AuthRateLimiter/middleware tests."""
    return make_redis_client(auth=True)


@pytest.fixture(autouse=True)
def _isolate_module_state() -> Generator[None, None, None]:
    """Snapshot and restore the rate-limiter module globals between tests.

    Resets ``_redis_clients``, ``redis_available``, the ``_auth_rate_limiter``
    singleton and the class-level ``RateLimitMiddleware.RULES`` so a mutation in
    one test (e.g. ``set_rules`` or a shutdown that clears the client set) can
    never leak into a later test.
    """
    saved_clients = set(rl_mod._redis_clients)
    saved_available = rl_mod.redis_available
    saved_limiter = rl_mod._auth_rate_limiter
    saved_rules = list(RateLimitMiddleware.RULES)
    yield
    rl_mod._redis_clients.clear()
    rl_mod._redis_clients.update(saved_clients)
    rl_mod.redis_available = saved_available
    rl_mod._auth_rate_limiter = saved_limiter
    RateLimitMiddleware.RULES = saved_rules
