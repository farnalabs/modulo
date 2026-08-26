"""Connector-local per-destination rate limiting buckets (FAR-411 / FAR-439).

The REST connector's per-destination outbound rate limit lives here rather than
reaching into ``modulo.core`` — the import-linter contract forbids connectors
importing core, so ``modulo.core.rate_limiter.TokenBucket`` cannot be reused.
Semantics mirror the core bucket: continuous refill up to ``burst``, guarded by
an ``asyncio.Lock`` so concurrent tasks never go below zero tokens.

FAR-439 adds a SHARED, Redis-backed limiter so a fleet of uvicorn/SAQ workers
enforces ONE budget per destination instead of ``N`` independent per-process
budgets. A destination is keyed per-tenant (``<tenant_id>:<destination>``) where
the destination is the resolved host + path, so different tenants never share a
budget. A SHARED limiter requires a real (non-empty) ``tenant_id`` — passing a
``redis_client`` without one raises, because silently coercing to ``"default"``
would funnel every organisation into a single cross-tenant Redis budget. The
shared limiter composes with the local bucket:

* **Redis available** — every worker acquires a token through a single atomic
  Redis token bucket (a Lua script), so concurrent workers across the fleet can
  never over-spend the shared budget (no lost-token race).
* **Redis NOT configured** (``redis_client=None``) — the connector-local
  :class:`TokenBucket` is authoritative. This is the single-worker-dev /
  no-fleet case, where there is no shared budget to multiply, so a per-process
  bucket is correct.

REMOVED FAIL-OPEN FALLBACK (FAR-439 follow-up):

When a ``redis_client`` **is** configured (a multi-worker fleet sharing one
budget is the *point*), a Redis failure must NOT fall back to each worker's own
per-process bucket — that would silently multiply the effective cap by the
worker count (``N x burst``), defeating the single-budget guarantee. The limiter
is therefore **fail-closed** when Redis is configured but unavailable:

* a single transient failure is **retried once** (the shared charge is re-issued
  against the SAME Redis budget — never a second, uncounted budget);
* if the retry also fails, :class:`SharedBudgetUnavailableError` is raised and a
  prominent ``rest.rate_limit.degraded`` warning is emitted, so the request is
  NOT sent on a budget we cannot account for and an operator sees the degrade
  window. We deliberately do NOT mint from a per-process bucket here — the whole
  point of a quota-correctness limiter is one shared budget.

The ``rest.rate_limit.degraded`` signal is the honest aggregate-cap alert: it
fires exactly once per outage episode where consumption crosses from the shared
Redis budget into fail-closed, making the window visible without flooding per
request.

Saturation is surfaced as a monotonically increasing counter plus a structured
log. To avoid alert flooding, the ``rest.rate_limit.saturated`` WARNING is
emitted only on a deny-after-grant transition (or the first deny for a fresh
destination); subsequent back-to-back denies still increment the counters but do
not re-emit the level — the counters are the metric, the log is the alarm.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from redis.exceptions import RedisError

_log = logging.getLogger(__name__)


class SharedBudgetUnavailableError(RuntimeError):
    """A configured shared (Redis) budget could not be charged (FAR-439).

    Raised when a ``redis_client`` is configured but the shared Redis bucket
    cannot be updated. The caller MUST treat this as fail-closed: the request is
    never sent, because a token could not be charged against the authoritative
    shared budget and we refuse to fall back to an uncounted per-process budget
    (which would let ``N`` workers each overspend ``burst``).
    """


# Transport/command failures that mean the shared bucket could not be charged.
# ``RedisError`` covers redis-py's connection/timeout/response errors;
# the builtin ``OSError``/``ConnectionError``/``TimeoutError`` cover non-redis
# socket-level failures surfaced unwrapped.
_REDIS_FAILURES = (RedisError, OSError, TimeoutError, ConnectionError)


class TokenBucket:
    """In-memory, per-process, async-safe token bucket."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate = float(rate)
        self.burst = int(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()
        self._lock = asyncio.Lock()

    async def consume(self, tokens: float = 1.0) -> bool:
        """Consume ``tokens`` (default one). Returns False when the bucket is low."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        async with self._lock:
            now = self._clock()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True


# Lua script executed atomically on the Redis server. Reads the per-key token
# bucket (tokens + last-refill timestamp), applies continuous refill, and either
# consumes ``cost`` tokens (returning 1) or persists the refilled state and
# returns 0. Atomicity on the server is what prevents concurrent workers from
# reading the same token count and both spending it (no lost-token race). A
# PEXPIRE on every call reclaims an abandoned bucket rather than accumulating
# forever, while comfortably outlasting a burst-worth of refill.
#
# The refill base uses ``redis.call('TIME')`` SERVER-side (not the worker-supplied
# clock argument) so every worker across the fleet agrees on "now" even with
# clock skew — a shared bucket must never rely on a single worker's wall clock.
# A corrupt stored value (a hash key present but unparseable) returns -1 instead
# of silently re-bursting to full capacity, so the caller can warn + fail closed.
_CONSUME_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local tt = redis.call('TIME')
local now = tt[1] + tt[2] / 1e6

local st = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(st[1])
local ts = tonumber(st[2])
if tokens == nil and st[1] ~= nil then
    return -1
end
if tokens == nil then
    tokens = burst
    ts = now
end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = tokens + elapsed * rate
if tokens > burst then tokens = burst end
ts = now
if tokens >= cost then
    redis.call('HSET', key, 'tokens', tokens - cost, 'ts', ts)
    redis.call('PEXPIRE', key, ttl_ms)
    return 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', ts)
redis.call('PEXPIRE', key, ttl_ms)
return 0
"""


class RedisTokenBucket:
    """Redis-backed, atomic token bucket shared across workers (FAR-439).

    Unlike :class:`TokenBucket` (per-process, using ``time.monotonic``), a shared
    bucket must use a **wall clock** (``time.time``) so every worker agrees on
    "now". The Lua script runs atomically on the Redis server — and reads the
    refill base from ``redis.call('TIME')`` server-side — so concurrent workers
    never double-spend a token and never trust a single worker's clock.
    """

    def __init__(
        self,
        redis_client: Any,
        rate: float,
        burst: int,
        key_prefix: str = "rest_rate_limit:",
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be >= 1")
        self.rate = float(rate)
        self.burst = int(burst)
        self._key_prefix = key_prefix
        self._script = redis_client.register_script(_CONSUME_LUA)

    def _key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    async def consume(self, key: str, tokens: float = 1.0, now: float | None = None) -> bool:
        """Consume ``tokens`` from the shared bucket at ``key``; False when low.

        ``now`` is retained for backward compatibility with callers and as a
        historical test seam, but it is deliberately NOT forwarded to the Redis
        script: the production Lua reads the refill base from
        ``redis.call('TIME')`` server-side, so worker clock skew never corrupts a
        shared bucket. Redis errors and an unchargeable (corrupt) stored value
        propagate to the caller, which FAILS CLOSED rather than minting a token
        from an uncounted per-process budget.
        """
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        # TTL must comfortably outlive a burst-worth of refill so the bucket is
        # reclaimed only when truly idle (never mid-window).
        ttl_ms = int(max(60.0, (self.burst / self.rate if self.rate > 0 else 60.0) * 2) * 1000)
        key_ = self._key(key)
        keys = [key_]
        # ARGV is exactly [rate, burst, cost, ttl_ms] as the Lua reads it. The
        # worker's ``now`` is intentionally NOT sent: a stale/epoch ``now``
        # (≈1.75e9) was previously misread as ARGV[4] (ttl_ms) by PEXPIRE, giving
        # every Redis key a ~20-day expiry instead of the intended reclaim TTL.
        args = [self.rate, self.burst, tokens, ttl_ms]
        result = await self._script(keys=keys, args=args)
        if result is None or result < 0:
            _log.warning(
                "rest.rate_limit.corrupt_bucket",
                extra={"bucket": key_, "result": result},
            )
            raise SharedBudgetUnavailableError(
                f"shared rate-limit bucket {key_!r} was corrupt; refusing to re-burst (fail-closed)"
            )
        return bool(result)


class PerDestinationRateLimiter:
    """Per-destination rate limiter: shared Redis budget with fail-closed outage.

    Holds one bucket per destination (host + path), keyed per-tenant so tenants
    never share a budget. When a ``redis_client`` is supplied the shared Redis
    bucket is authoritative; any Redis failure is retried once and then raises
    :class:`SharedBudgetUnavailableError` (fail-closed) — the limiter never mints
    from a per-process budget when a shared one is configured, because that
    multiplies the effective cap by the worker count. Without a ``redis_client``
    it is purely per-process — identical to the pre-FAR-439 single-worker
    behaviour, where no shared budget exists to multiply.

    Saturation is surfaced as a monotonically increasing counter plus a
    structured log; the WARNING is emitted only on a deny-after-grant transition
    (the counters always increment per deny) so an operator can alert on a
    destination that is newly throttling without a per-deny flood.
    """

    def __init__(
        self,
        *,
        rate: float,
        burst: int,
        redis_client: Any = None,
        tenant_id: str | None = None,
        key_prefix: str = "rest_rate_limit:",
        buckets: dict[str, TokenBucket] | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be >= 1")
        if redis_client is not None and not tenant_id:
            # A SHARED (Redis) budget keyed by ``<tenant_id>:<destination>`` must
            # never be created without a real tenant: silently coercing a missing
            # tenant to "default" would bucket every caller into ONE Redis budget
            # across distinct orgs (a cross-tenant leak). Require a tenant at the
            # composition root — fail loud rather than share a budget.
            raise ValueError(
                "a shared (Redis) per-destination rate limiter requires a non-empty tenant_id; "
                "refusing to bucket every caller into a shared 'default' budget (cross-tenant leak)"
            )
        self.rate = float(rate)
        self.burst = int(burst)
        self._redis_client = redis_client
        self._tenant_id = tenant_id or "default"
        self._key_prefix = key_prefix
        self.buckets: dict[str, TokenBucket] = buckets if buckets is not None else {}
        self._lock = asyncio.Lock()
        self._redis_buckets: dict[str, RedisTokenBucket] = {}
        self.saturation_count = 0
        self.saturations: dict[str, int] = {}
        # Whether the last successful consume for a destination was a DENY (True =
        # currently in saturation). Used to gate the saturated WARNING to the
        # deny-after-grant transition.
        self._saturated: dict[str, bool] = {}

    def key(self, destination: str) -> str:
        """Per-tenant Redis key for ``destination`` (host + path)."""
        return f"{self._tenant_id}:{destination}"

    async def consume(self, destination: str, tokens: float = 1.0) -> bool:
        """Consume ``tokens`` for ``destination``; True on acquire, False on saturate.

        Redis-first when a client is available: a failure is retried once against
        the SAME shared budget, and if it still fails the limiter FAILS CLOSED
        (raises :class:`SharedBudgetUnavailableError`) rather than minting a token
        from an uncounted per-process budget. Without a ``redis_client`` the local
        :class:`TokenBucket` is authoritative. Returns False when the budget is
        exhausted (and records a saturation signal).
        """
        if self._redis_client is not None:
            try:
                ok = await self._redis_consume(destination, tokens)
            except _REDIS_FAILURES as exc:
                _log.warning(
                    "rest.rate_limit.redis_error",
                    extra={"destination": destination, "tenant": self._tenant_id, "error": str(exc)},
                )
                # A single transient failure may resolve; retry the SAME shared
                # charge once. We never fall back to a per-process bucket.
                try:
                    ok = await self._redis_consume(destination, tokens)
                except _REDIS_FAILURES as exc2:
                    _log.warning(
                        "rest.rate_limit.degraded",
                        extra={
                            "destination": destination,
                            "tenant": self._tenant_id,
                            "error": str(exc2),
                        },
                    )
                    raise SharedBudgetUnavailableError(
                        f"shared rate-limit budget unavailable for {self.key(destination)!r}: {exc2}"
                    ) from exc2
            if ok:
                self._saturated[destination] = False
            else:
                self._record_saturation(destination)
            return ok
        ok = await self._local_consume(destination, tokens)
        if ok:
            self._saturated[destination] = False
        else:
            self._record_saturation(destination)
        return ok

    async def _redis_consume(self, destination: str, tokens: float) -> bool:
        bucket = self._redis_buckets.get(destination)
        if bucket is None:
            bucket = RedisTokenBucket(self._redis_client, self.rate, self.burst, key_prefix=self._key_prefix)
            self._redis_buckets[destination] = bucket
        return await bucket.consume(self.key(destination), tokens=tokens)

    async def _local_consume(self, destination: str, tokens: float) -> bool:
        bucket = self.buckets.get(destination)
        if bucket is None:
            async with self._lock:
                bucket = self.buckets.get(destination)
                if bucket is None:
                    bucket = TokenBucket(rate=self.rate, burst=self.burst)
                    self.buckets[destination] = bucket
        return await bucket.consume(tokens)

    def _record_saturation(self, destination: str) -> None:
        self.saturation_count += 1
        count = self.saturations.get(destination, 0) + 1
        self.saturations[destination] = count
        was_saturated = self._saturated.get(destination, False)
        self._saturated[destination] = True
        # Emit the WARNING only on the deny-after-grant transition so a saturated
        # destination does not flood an alert per deny; the counters (the metric)
        # still increment every deny.
        if not was_saturated:
            _log.warning(
                "rest.rate_limit.saturated",
                extra={
                    "destination": destination,
                    "tenant": self._tenant_id,
                    "saturation_count": count,
                    "saturation_total": self.saturation_count,
                },
            )
