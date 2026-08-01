"""Redis-backed sliding-window rate limiter.

Uses ZADD + ZREMRANGEBYSCORE on a sorted set per key.
Window duration and max requests are configurable per-route.
Redis is required — no in-memory fallback.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

WINDOW_SECONDS = 60


@dataclass
class RateLimitRule:
    path_prefix: str
    max_requests: int
    window_s: int = WINDOW_SECONDS
    key_fn: Callable[[Any], str] | None = None


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

        return bool(count <= max_requests)


class RateLimiterRegistry:
    """Holds route-specific limit rules backed by Redis."""

    def __init__(self, redis_client: Any) -> None:
        if redis_client is None:
            raise ValueError("RateLimiterRegistry requires a Redis client")
        self._redis = redis_client
        self._sliding = RedisSlidingWindowRateLimiter(redis_client)

    async def check(self, key: str, max_requests: int, window_s: int = WINDOW_SECONDS) -> bool:
        return await self._sliding.check(key, max_requests, window_s)


class AuthRateLimiter:
    """Tracks failed login attempts per IP with exponential backoff.

    Two key namespaces:
      - auth_ratelimit:<ip>     — sorted set of failure timestamps
      - auth_ratelimit:lockout:<ip> — TTL key holding lockout expiry

    Backoff formula (only applies when failures >= max_attempts):
      tier = floor(failures / max_attempts)
      backoff = min(2^(tier - 1) * 60, 3600) seconds
    """

    def __init__(
        self,
        redis_client: Any,
        prefix: str = "auth_ratelimit:",
        max_attempts: int = 10,
        window_s: int = 60,
    ) -> None:
        if redis_client is None:
            raise ValueError("AuthRateLimiter requires a Redis client")
        self._redis = redis_client
        self._prefix = prefix
        self._max_attempts = max_attempts
        self._window_s = window_s

    async def check_login(self, ip: str) -> tuple[bool, int]:
        """Check if login from *ip* is allowed.

        Returns (allowed, retry_after_seconds).
        """
        now = time.time()
        key = f"{self._prefix}{ip}"
        lockout_key = f"{self._prefix}lockout:{ip}"
        cutoff = now - self._window_s

        ttl = await self._redis.ttl(lockout_key)
        if ttl > 0:
            return (False, int(ttl))

        pipe = self._redis.pipeline(transaction=True)
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        _, count = await pipe.execute()

        if count >= self._max_attempts:
            backoff = self._compute_backoff(count)
            await self._redis.setex(lockout_key, backoff, "1")
            return (False, backoff)

        return (True, 0)

    async def record_failure(self, ip: str) -> None:
        """Record a failed login attempt for *ip*."""
        now = time.time()
        key = f"{self._prefix}{ip}"
        await self._redis.zadd(key, {str(now): now})
        await self._redis.expire(key, self._window_s * 2)

    async def record_success(self, ip: str) -> None:
        """Reset all failure counters for *ip* on successful login."""
        key = f"{self._prefix}{ip}"
        lockout_key = f"{self._prefix}lockout:{ip}"
        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(key)
        pipe.delete(lockout_key)
        await pipe.execute()

    @staticmethod
    def _compute_backoff(count: int) -> int:
        tier = count // 10
        return min(int(pow(2, tier - 1) * 60), 3600)
