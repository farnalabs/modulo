"""Redis-backed sliding-window rate limiter.

Uses ZADD + ZREMRANGEBYSCORE on a sorted set per key.
Window duration and max requests are configurable per-route.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

WINDOW_SECONDS = 60


@dataclass
class RateLimitRule:
    path_prefix: str
    max_requests: int
    window_s: int = WINDOW_SECONDS
    key_fn: Callable[[Any], str] | None = None


class TokenBucket:
    """Simple in-memory token bucket fallback when Redis is unavailable."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.last_refill = now
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class RedisSlidingWindowRateLimiter:
    """Sliding window rate limiter backed by Redis.

    Each request is recorded as a member of a sorted set keyed by the
    rate-limit key. The score is the Unix timestamp. Old entries are
    pruned on every check.

    Requires a `redis.asyncio.Redis` or compatible client with
    `zadd`, `zremrangebyscore`, `zcard` methods.
    """

    def __init__(self, redis_client: Any, prefix: str = "ratelimit:") -> None:
        self._redis = redis_client
        self._prefix = prefix

    async def check(self, key: str, max_requests: int, window_s: int = WINDOW_SECONDS) -> bool:
        """Record a request and return True if within limit, False if exceeded."""
        now = time.time()
        redis_key = f"{self._prefix}{key}"
        cutoff = now - window_s

        pipe = self._redis.pipeline(transaction=True)
        pipe.zremrangebyscore(redis_key, 0, cutoff)
        pipe.zadd(redis_key, {str(now): now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window_s * 2)
        _, _, count, _ = await pipe.execute()

        return count <= max_requests


class RateLimiterRegistry:
    """Holds route-specific limit rules and dispatches to the active backend.

    Falls back to in-memory TokenBucket when Redis is not configured.
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._sliding: RedisSlidingWindowRateLimiter | None = (
            RedisSlidingWindowRateLimiter(redis_client) if redis_client is not None else None
        )
        self._buckets: dict[str, TokenBucket] = defaultdict(lambda: TokenBucket(rate=10.0, burst=20))

    @property
    def has_redis(self) -> bool:
        return self._redis is not None

    async def check(self, key: str, max_requests: int, window_s: int = WINDOW_SECONDS) -> bool:
        if self._sliding is not None:
            return await self._sliding.check(key, max_requests, window_s)
        bucket = self._buckets[key]
        return await bucket.consume()
