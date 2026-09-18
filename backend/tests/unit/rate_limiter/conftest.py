"""Shared fixtures for the rate_limiter test package.

Keep builders that tests call directly in ``helpers.py``; anything that must be
auto-discovered by pytest (fixtures) lives here.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from tests.unit.rate_limiter.helpers import make_redis_client

from modulo.api.middleware import rate_limiter as rl_mod
from modulo.api.middleware.rate_limiter import RateLimitMiddleware


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
