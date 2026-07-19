"""Unit tests for RedisSlidingWindowRateLimiter and RateLimiterRegistry."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.rate_limiter import RateLimiterRegistry, RedisSlidingWindowRateLimiter, TokenBucket


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


class TestTokenBucket:
    @pytest.mark.parametrize(
        "max_requests,window_s,consume_count,expected_results",
        [
            (5, 1, 5, [True] * 5),
            (2, 20, 3, [True, True, False]),
            (2, 1, 4, [True, True, False, True]),
            (3, 3, 4, [True, True, True, False]),
        ],
    )
    async def test_token_bucket(self, max_requests, window_s, consume_count, expected_results):
        if max_requests == 2 and window_s == 1:
            times = iter([0.0, 0.0, 0.0, 0.0, 0.5])
            with patch.object(time, "monotonic", side_effect=times):
                bucket = TokenBucket(max_requests=max_requests, window_s=window_s)
                for expected in expected_results:
                    assert await bucket.consume() is expected
        else:
            bucket = TokenBucket(max_requests=max_requests, window_s=window_s)
            for expected in expected_results:
                assert await bucket.consume() is expected


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

    async def test_uses_custom_window(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5, window_s=120)
        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyscore.assert_called_once()
        args = pipe.zremrangebyscore.call_args[0]
        assert args[0] is not None


class TestRateLimiterRegistry:
    @pytest.mark.parametrize(
        "has_redis,redis_result,expected",
        [
            (False, None, True),
            (True, (None, None, 1, True), True),
            (True, (None, None, 6, True), False),
        ],
    )
    async def test_registry(self, has_redis, redis_result, expected):
        if has_redis:
            mock_redis = MagicMock()
            mock_redis.pipeline.return_value.execute = AsyncMock(return_value=redis_result)
            registry = RateLimiterRegistry(redis_client=mock_redis)
        else:
            registry = RateLimiterRegistry()
        assert await registry.check("k", max_requests=5) is expected
