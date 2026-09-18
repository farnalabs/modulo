"""Unit tests for RedisSlidingWindowRateLimiter and RateLimiterRegistry."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.rate_limiter.helpers import make_redis_client

from modulo.core.rate_limiter import (
    WINDOW_SECONDS,
    RateLimiterRegistry,
    RateLimitRule,
    RedisSlidingWindowRateLimiter,
)


@pytest.fixture
def mock_redis():
    """Sliding-window flavoured Redis mock (module-level shadows the conftest one)."""
    return make_redis_client()


class TestRedisSlidingWindowRateLimiter:
    @pytest.mark.parametrize(("zcard_value", "expected"), [(0, True), (1, True), (5, True), (6, False)])
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
        pipe.zcard.assert_called_once()
        assert pipe.zcard.call_args[0][0] == "rl:mykey"
        pipe.expire.assert_called_once()
        assert pipe.expire.call_args[0][0] == "rl:mykey"

    async def test_pipeline_created_transactional(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5)
        mock_redis.pipeline.assert_called_once_with(transaction=True)

    async def test_prunes_entries_older_than_window(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5, window_s=120)
        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyscore.assert_called_once()
        _, lower_bound, cutoff = pipe.zremrangebyscore.call_args[0]
        assert lower_bound == 0
        now = time.time()
        assert abs(cutoff - (now - 120)) < 2

    async def test_uses_default_window(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5)
        pipe = mock_redis.pipeline.return_value
        _, _, cutoff = pipe.zremrangebyscore.call_args[0]
        now = time.time()
        assert abs(cutoff - (now - WINDOW_SECONDS)) < 2

    async def test_expires_key_at_double_window(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5, window_s=120)
        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once()
        assert pipe.expire.call_args[0][0] == "ratelimit:k"
        assert pipe.expire.call_args[0][1] == 240

    async def test_expires_key_at_default_double_window(self, mock_redis):
        """With the default window, the TTL must be 2 * WINDOW_SECONDS."""
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5)
        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once()
        assert pipe.expire.call_args[0][1] == WINDOW_SECONDS * 2

    async def test_records_timestamp_member(self, mock_redis):
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        await limiter.check("k", max_requests=5)
        pipe = mock_redis.pipeline.return_value
        pipe.zadd.assert_called_once()
        _, member = pipe.zadd.call_args[0]
        ((ts, score),) = member.items()
        now = time.time()
        assert abs(score - now) < 2
        ts_str, sep, suffix = ts.partition(":")
        assert sep == ":"
        assert ts_str == str(score)
        assert len(suffix) == 32

    async def test_blocks_every_request_when_max_requests_zero(self, mock_redis):
        """max_requests=0 must block: the current request is recorded before the check."""
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, None, 1, True))
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        assert await limiter.check("k", max_requests=0) is False

    async def test_window_zero_sets_cutoff_to_now(self, mock_redis):
        """window_s=0 prunes everything strictly before now but still allows the current request."""
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, None, 1, True))
        limiter = RedisSlidingWindowRateLimiter(mock_redis)
        assert await limiter.check("k", max_requests=1, window_s=0) is True
        pipe = mock_redis.pipeline.return_value
        _, _, cutoff = pipe.zremrangebyscore.call_args[0]
        now = time.time()
        assert abs(cutoff - now) < 2


class TestRateLimitRule:
    def test_default_window_and_no_key_fn(self):
        rule = RateLimitRule(path_prefix="/api/v1/runs", max_requests=60)
        assert rule.window_s == WINDOW_SECONDS
        assert rule.key_fn is None

    def test_custom_window_and_key_fn(self):
        def key_fn(request):
            return f"tenant:{request.get('tenant_id')}"

        rule = RateLimitRule(
            path_prefix="/api/v1/runs",
            max_requests=30,
            window_s=15,
            key_fn=key_fn,
        )
        assert rule.window_s == 15
        assert rule.key_fn({"tenant_id": "acme"}) == "tenant:acme"


class TestRateLimiterRegistry:
    @pytest.mark.parametrize(
        ("redis_result", "expected"),
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

    async def test_registry_forwards_window(self, mock_redis):
        registry = RateLimiterRegistry(redis_client=mock_redis)
        await registry.check("k", max_requests=5, window_s=90)
        pipe = mock_redis.pipeline.return_value
        _, _, cutoff = pipe.zremrangebyscore.call_args[0]
        now = time.time()
        assert abs(cutoff - (now - 90)) < 2

    def test_requires_redis_client(self):
        with pytest.raises(ValueError, match="requires a Redis client"):
            RateLimiterRegistry(redis_client=None)
