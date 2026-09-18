"""Unit tests for rate limiter registry construction and graceful fallbacks.

Covers:
  - _create_registry: sqlite -> no-op, redis_url -> Redis registry,
    Redis connect failure -> no-op, no redis_url -> no-op
  - _NoopRateLimiter allows every request
  - _redis_clients tracking for shutdown
  - asyncio.CancelledError propagation (never swallowed)
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from tests.unit.rate_limiter.helpers import make_settings

from modulo.api.middleware import rate_limiter as rl_mod
from modulo.api.middleware.rate_limiter import _create_registry, _NoopRateLimiter
from modulo.core.rate_limiter import RateLimiterRegistry


class TestCreateRegistry:
    def test_sqlite_disables_rate_limiting(self):
        registry = _create_registry(make_settings(modulo_db="sqlite"))
        assert isinstance(registry, _NoopRateLimiter)
        assert rl_mod.redis_available is False

    def test_no_redis_url_returns_noop(self):
        """With an empty redis_url, _create_registry must never attempt a connection."""
        registry = _create_registry(make_settings(redis_url=""))
        assert isinstance(registry, _NoopRateLimiter)
        assert rl_mod.redis_available is False

    def test_redis_url_returns_registry(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda url, **kwargs: client)
        registry = _create_registry(make_settings(redis_url="redis://redis:6379/0"))
        assert isinstance(registry, RateLimiterRegistry)
        assert rl_mod.redis_available is True
        assert client in rl_mod._redis_clients

    @pytest.mark.parametrize("exc", [ConnectionError("boom"), RuntimeError("conn refused")])
    def test_redis_connect_failure_falls_back_to_noop(self, monkeypatch, exc):
        monkeypatch.setattr("redis.asyncio.Redis.from_url", MagicMock(side_effect=exc))
        registry = _create_registry(make_settings(redis_url="redis://redis:6379/0"))
        assert isinstance(registry, _NoopRateLimiter)
        assert rl_mod.redis_available is False

    def test_cancelled_error_is_never_swallowed(self, monkeypatch):
        monkeypatch.setattr(
            "redis.asyncio.Redis.from_url",
            MagicMock(side_effect=asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            _create_registry(make_settings(redis_url="redis://redis:6379/0"))


class TestNoopRateLimiter:
    async def test_noop_allows_every_request(self):
        limiter = _NoopRateLimiter()
        assert await limiter.check("key", max_requests=1) is True
        assert await limiter.check("key", max_requests=0) is True
