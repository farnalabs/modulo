"""Unit tests for RedisSlidingWindowRateLimiter and RateLimiterRegistry."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.rate_limiter import RateLimiterRegistry, RedisSlidingWindowRateLimiter


@pytest.fixture
def mock_redis():
    client = MagicMock()
    pipe = MagicMock()
    pipe.zremrangebyscore = MagicMock(return_value=pipe)
    pipe.zadd = MagicMock(return_value=pipe)
    pipe.zcard = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=(None, None, 1, True))
    client.pipeline = MagicMock(return_value=pipe)
    return client


class TestRedisSlidingWindowRateLimiter:
    @pytest.mark.parametrize("zcard_value,expected", [(1, True), (5, True), (6, False)])
    async def test_check_limit(self, mock_redis, zcard_value, expected):
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, None, zcard_value, True))
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        assert await limiter.check("test-key", max_requests=5) is expected

    async def test_uses_correct_redis_key(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis, prefix="rl:")
        await limiter.check("mykey", max_requests=10, window_s=30)
        pipe = mock_redis.pipeline.return_value
        call_args = pipe.zadd.call_args
        assert call_args is not None
        redis_key = call_args[0][0]
        assert redis_key == "rl:mykey"
        pipe.zremrangebyscore.assert_called_once()
        assert pipe.zremrangebyscore.call_args[0][0] == "rl:mykey"

    async def test_uses_custom_window(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5, window_s=120)
        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyscore.assert_called_once()
        _, _, cutoff = pipe.zremrangebyscore.call_args[0]
        now = time.time()
        assert abs(cutoff - (now - 120)) < 2

    async def test_expires_key_at_double_window(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5, window_s=120)
        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once()
        assert pipe.expire.call_args[0][0] == "ratelimit:k"
        assert pipe.expire.call_args[0][1] == 240

    async def test_records_timestamp_member(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5)
        pipe = mock_redis.pipeline.return_value
        pipe.zadd.assert_called_once()
        _, member = pipe.zadd.call_args[0]
        ((ts, score),) = member.items()
        now = time.time()
        assert abs(score - now) < 2
        assert ts == str(score)


class TestRateLimiterRegistry:
    @pytest.mark.parametrize(
        "redis_result,expected",
        [
            ((None, None, 1, True), True),
            ((None, None, 6, True), False),
        ],
    )
    async def test_registry(self, redis_result, expected):
        mock_redis = MagicMock()
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=redis_result)
        registry = RateLimiterRegistry(redis_client=mock_redis)
        assert await registry.check("k", max_requests=5) is expected

    def test_requires_redis_client(self):
        with pytest.raises(ValueError, match="requires a Redis client"):
            RateLimiterRegistry(redis_client=None)
