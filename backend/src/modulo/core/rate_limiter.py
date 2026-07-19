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

    def __init__(self, max_requests: int, window_s: int) -> None:
        self.rate = max_requests / window_s
        self.burst = max_requests
        self.tokens = float(max_requests)
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

        return bool(count <= max_requests)


class RateLimiterRegistry:
    """Holds route-specific limit rules and dispatches to the active backend.

    Falls back to in-memory TokenBucket when Redis is not configured.
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._sliding: RedisSlidingWindowRateLimiter | None = (
            RedisSlidingWindowRateLimiter(redis_client) if redis_client is not None else None
        )
        self._buckets: dict[str, TokenBucket] = {}

    @property
    def has_redis(self) -> bool:
        return self._redis is not None

    async def check(self, key: str, max_requests: int, window_s: int = WINDOW_SECONDS) -> bool:
        if self._sliding is not None:
            return await self._sliding.check(key, max_requests, window_s)
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(max_requests=max_requests, window_s=window_s)
        bucket = self._buckets[key]
        return await bucket.consume()


class AuthRateLimiter:
    """Tracks failed login attempts per IP with exponential backoff.

    Uses Redis sorted sets when available; falls back to in-memory dicts.
    Two key namespaces:
      - auth_ratelimit:<ip>     — sorted set of failure timestamps
      - auth_ratelimit:lockout:<ip> — TTL key holding lockout expiry

    Backoff formula (only applies when failures >= max_attempts):
      tier = floor(failures / max_attempts)
      backoff = min(2^(tier - 1) * 60, 3600) seconds
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        prefix: str = "auth_ratelimit:",
        max_attempts: int = 10,
        window_s: int = 60,
    ) -> None:
        self._redis = redis_client
        self._prefix = prefix
        self._max_attempts = max_attempts
        self._window_s = window_s
        self._mem_failures: dict[str, list[float]] = defaultdict(list)
        self._mem_lockouts: dict[str, float] = {}

    async def check_login(self, ip: str) -> tuple[bool, int]:
        """Check if login from *ip* is allowed.

        Returns (allowed, retry_after_seconds).
        """
        if self._redis is not None:
            return await self._check_login_redis(ip)
        return self._check_login_memory(ip)

    async def _check_login_redis(self, ip: str) -> tuple[bool, int]:
        assert self._redis is not None  # nosec
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

    def _check_login_memory(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - self._window_s

        lockout_until = self._mem_lockouts.get(ip, 0.0)
        if lockout_until > now:
            return (False, int(lockout_until - now))

        failures = [t for t in self._mem_failures.get(ip, []) if t > cutoff]
        self._mem_failures[ip] = failures
        count = len(failures)

        if count >= self._max_attempts:
            backoff = self._compute_backoff(count)
            self._mem_lockouts[ip] = now + backoff
            return (False, backoff)

        return (True, 0)

    async def record_failure(self, ip: str) -> None:
        """Record a failed login attempt for *ip*."""
        if self._redis is not None:
            await self._record_failure_redis(ip)
        else:
            self._record_failure_memory(ip)

    async def _record_failure_redis(self, ip: str) -> None:
        assert self._redis is not None  # nosec
        now = time.time()
        key = f"{self._prefix}{ip}"
        await self._redis.zadd(key, {str(now): now})
        await self._redis.expire(key, self._window_s * 2)

    def _record_failure_memory(self, ip: str) -> None:
        now = time.time()
        cutoff = now - self._window_s
        failures = self._mem_failures[ip]
        failures.append(now)
        self._mem_failures[ip] = [t for t in failures if t > cutoff]

    async def record_success(self, ip: str) -> None:
        """Reset all failure counters for *ip* on successful login."""
        if self._redis is not None:
            await self._record_success_redis(ip)
        else:
            self._record_success_memory(ip)

    async def _record_success_redis(self, ip: str) -> None:
        assert self._redis is not None  # nosec
        key = f"{self._prefix}{ip}"
        lockout_key = f"{self._prefix}lockout:{ip}"
        pipe = self._redis.pipeline(transaction=True)
        pipe.delete(key)
        pipe.delete(lockout_key)
        await pipe.execute()

    def _record_success_memory(self, ip: str) -> None:
        self._mem_failures.pop(ip, None)
        self._mem_lockouts.pop(ip, None)

    @staticmethod
    def _compute_backoff(count: int) -> int:
        tier = count // 10
        return min(int(pow(2, tier - 1) * 60), 3600)
