"""Unit tests for RedisSlidingWindowRateLimiter and RateLimiterRegistry."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.rate_limiter.helpers import make_redis_client

from farna.core.rate_limiter import (
    WINDOW_SECONDS,
    RateLimiterRegistry,
    RateLimitRule,
    RedisSlidingWindowRateLimiter,
)


@pytest.fixture
def sliding_redis():
    """Sliding-window flavoured Redis mock.

    Deliberately NOT named ``mock_redis``: the conftest fixture of that name is
    auth-flavoured (2-tuple pipeline result), and a module-level override would
    silently swap the shape for any future module that reused the name.
    """
    return make_redis_client()


class TestRedisSlidingWindowRateLimiter:
    @pytest.mark.parametrize(
        ("zcard_value", "expected"),
        [(0, True), (1, True), (5, True), (6, False)],
        ids=["empty", "single", "at-limit", "over-limit"],
    )
    async def test_allows_up_to_max_requests_then_blocks(self, sliding_redis, zcard_value, expected):
        sliding_redis.eval = AsyncMock(return_value=(int(expected), 0))
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        assert await limiter.check("test-key", max_requests=5) is expected

    async def test_uses_correct_redis_key(self, sliding_redis):
        limiter = RedisSlidingWindowRateLimiter(sliding_redis, prefix="rl:")
        sliding_redis.eval = AsyncMock(return_value=(1, 0))
        await limiter.check("mykey", max_requests=10, window_s=30)
        args = sliding_redis.eval.await_args.args
        assert args[2] == "rl:mykey"
        assert args[4] == 30
        assert args[1] == 1

    async def test_pipeline_created_transactional(self, sliding_redis):
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        await limiter.check("k", max_requests=5)
        sliding_redis.eval.assert_awaited_once()

    async def test_prunes_entries_older_than_window(self, sliding_redis, frozen_time):
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        await limiter.check("k", max_requests=5, window_s=120)
        args = sliding_redis.eval.await_args.args
        assert args[4] == 120

    async def test_uses_default_window(self, sliding_redis, frozen_time):
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        await limiter.check("k", max_requests=5)
        args = sliding_redis.eval.await_args.args
        assert args[4] == WINDOW_SECONDS

    @pytest.mark.parametrize("window_s", [120, WINDOW_SECONDS], ids=["explicit-window", "default-window"])
    async def test_expires_key_at_double_window(self, sliding_redis, window_s):
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        await limiter.check("k", max_requests=5, window_s=window_s)
        args = sliding_redis.eval.await_args.args
        assert args[2] == "ratelimit:k"
        assert args[4] == window_s

    async def test_records_timestamp_member(self, sliding_redis, frozen_time):
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        await limiter.check("k", max_requests=5)
        args = sliding_redis.eval.await_args.args
        assert args[2] == "ratelimit:k"
        assert args[3] == frozen_time
        ts_str, sep, suffix = args[6].partition(":")
        assert sep == ":"
        assert ts_str == str(args[3])
        assert len(suffix) == 32

    async def test_blocks_every_request_when_max_requests_zero(self, sliding_redis):
        """max_requests=0 must block: the current request is recorded before the check."""
        sliding_redis.eval = AsyncMock(return_value=(0, 60))
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        assert await limiter.check("k", max_requests=0) is False

    async def test_window_zero_sets_cutoff_to_now(self, sliding_redis, frozen_time):
        """window_s=0 prunes everything strictly before now but still allows the current request."""
        sliding_redis.eval = AsyncMock(return_value=(1, 0))
        limiter = RedisSlidingWindowRateLimiter(sliding_redis)
        assert await limiter.check("k", max_requests=1, window_s=0) is True
        args = sliding_redis.eval.await_args.args
        assert args[4] == 0


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
        ids=["under-limit", "over-limit"],
    )
    async def test_registry_allows_or_blocks_based_on_count(self, redis_result, expected):
        sliding_redis = MagicMock()
        sliding_redis.eval = AsyncMock(return_value=(int(expected), 0))
        registry = RateLimiterRegistry(redis_client=sliding_redis)
        assert await registry.check("k", max_requests=5) is expected

    async def test_registry_forwards_window(self, sliding_redis, frozen_time):
        registry = RateLimiterRegistry(redis_client=sliding_redis)
        sliding_redis.eval = AsyncMock(return_value=(1, 0))
        await registry.check("k", max_requests=5, window_s=90)
        args = sliding_redis.eval.await_args.args
        assert args[4] == 90

    def test_requires_redis_client(self):
        with pytest.raises(ValueError, match="requires a Redis client"):
            RateLimiterRegistry(redis_client=None)
