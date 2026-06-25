"""Unit tests for RedisSlidingWindowRateLimiter and RateLimiterRegistry."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.rate_limiter import RateLimiterRegistry, RedisSlidingWindowRateLimiter, TokenBucket


class TestTokenBucket:
    async def test_consume_allows_when_tokens_available(self):
        bucket = TokenBucket(rate=10.0, burst=5)
        for _ in range(5):
            assert await bucket.consume() is True

    async def test_consume_blocks_when_empty(self):
        bucket = TokenBucket(rate=0.1, burst=2)
        assert await bucket.consume() is True
        assert await bucket.consume() is True
        assert await bucket.consume() is False

    async def test_refills_over_time(self):
        bucket = TokenBucket(rate=10.0, burst=2)
        times = iter([0.0, 0.0, 0.0, 0.2, 0.2])
        with patch.object(time, "monotonic", side_effect=times):
            await bucket.consume()
            await bucket.consume()
            assert await bucket.consume() is False
            assert await bucket.consume() is True

    async def test_burst_limits_max_tokens(self):
        bucket = TokenBucket(rate=1.0, burst=3)
        for _ in range(3):
            assert await bucket.consume() is True
        assert await bucket.consume() is False


class TestRedisSlidingWindowRateLimiter:
    @pytest.fixture
    def mock_redis(self):
        client = MagicMock()
        pipe = MagicMock()
        pipe.zremrangebyscore = MagicMock(return_value=pipe)
        pipe.zadd = MagicMock(return_value=pipe)
        pipe.zcard = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(None, None, 1, True))
        client.pipeline = MagicMock(return_value=pipe)
        return client

    async def test_check_allows_within_limit(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        assert await limiter.check("test-key", max_requests=5) is True

    async def test_check_blocks_when_over_limit(self, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.zcard = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(None, None, 6, True))
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        assert await limiter.check("test-key", max_requests=5) is False

    async def test_check_exact_limit(self, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute = AsyncMock(return_value=(None, None, 5, True))
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        assert await limiter.check("test-key", max_requests=5) is True

    async def test_uses_correct_redis_key(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis, prefix="rl:")
        await limiter.check("mykey", max_requests=10, window_s=30)
        pipe = mock_redis.pipeline.return_value
        call_args = pipe.zadd.call_args
        assert call_args is not None
        redis_key = call_args[0][0]
        assert redis_key == "rl:mykey"

    async def test_uses_custom_window(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5, window_s=120)
        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyscore.assert_called_once()
        args = pipe.zremrangebyscore.call_args[0]
        assert args[0] is not None


class TestRateLimiterRegistry:
    async def test_in_memory_fallback_by_default(self):
        registry = RateLimiterRegistry()
        assert registry.has_redis is False
        assert await registry.check("k", max_requests=100) is True

    async def test_uses_redis_when_available(self):
        mock_redis = MagicMock()
        pipe = MagicMock()
        pipe.zremrangebyscore = MagicMock(return_value=pipe)
        pipe.zadd = MagicMock(return_value=pipe)
        pipe.zcard = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(None, None, 1, True))
        mock_redis.pipeline = MagicMock(return_value=pipe)
        registry = RateLimiterRegistry(redis_client=mock_redis)
        assert registry.has_redis is True
        assert await registry.check("k", max_requests=5) is True
        pipe.execute.assert_awaited_once()

    async def test_redis_blocks_over_limit(self):
        mock_redis = MagicMock()
        pipe = MagicMock()
        pipe.zremrangebyscore = MagicMock(return_value=pipe)
        pipe.zadd = MagicMock(return_value=pipe)
        pipe.zcard = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(None, None, 6, True))
        mock_redis.pipeline = MagicMock(return_value=pipe)
        registry = RateLimiterRegistry(redis_client=mock_redis)
        assert await registry.check("k", max_requests=5) is False
