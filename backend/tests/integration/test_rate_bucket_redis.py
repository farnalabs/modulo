"""FAR-439: real-Redis integration tests for the shared per-destination limiter.

The unit suite (``tests/unit/connectors/test_rest_observability.py``) exercises
the bucket semantics through an in-process ``_FakeRedis`` that re-implements the
token-bucket Lua in Python — so a defect in the *real* Lua script
(``_CONSUME_LUA`` in ``modulo.connectors._rate_bucket``) can never fail a unit
test. The atomicity / quota guarantee of the shared limiter lives entirely in
that script (server-side ``TIME`` refill, atomic check-and-decrement, PEXPIRE
reclaim), so this module drives it against a real ``redis.asyncio`` server.

This closes the "prove-the-fix" gap: a class of bug that previously shipped here
(worker ``now`` misread as ``ARGV[4]`` / ttl_ms, yielding a ~20-day PEXPIRE)
cannot be caught by the fake, because the fake asserts the TTL the Python side
sends — not what the script actually applies. On a real server we read the key's
actual PEXPIRE and assert it is the computed reclaim TTL, never a stale epoch.

CI provisions a real Redis (redis:7-alpine) for the integration-changed job, so
these tests run there. Locally (or anywhere without Redis) they skip cleanly.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import redis.asyncio as aioredis

from modulo.connectors._rate_bucket import (
    PerDestinationRateLimiter,
    RedisTokenBucket,
    SharedBudgetUnavailableError,
)

REDIS_URL = os.environ.get("RATE_LIMIT_REDIS_URL", "redis://localhost:6379")


def _expected_ttl_ms(rate: float, burst: int) -> int:
    return int(max(60.0, (burst / rate if rate > 0 else 60.0) * 2) * 1000)


@pytest.fixture
async def redis_client() -> aioredis.Redis:
    """A real Redis client; the test is skipped if Redis is unreachable."""
    client = aioredis.from_url(REDIS_URL, decode_responses=False)
    try:
        await client.ping()
    except Exception as exc:  # connectivity probe, not test logic
        await client.aclose()
        pytest.skip(f"real Redis not available at {REDIS_URL}: {exc}")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def test_real_lua_no_lost_token_race_under_concurrency(
    redis_client: aioredis.Redis,
) -> None:
    """Fifty concurrent consumes against the REAL Lua script grant exactly burst.

    The atomic server-side check-and-decrement is what prevents concurrent workers
    from both spending the same token. If the Lua were broken (e.g. non-atomic),
    we would over-grant; the real script must cap at ``burst``.
    """
    bucket = RedisTokenBucket(redis_client, rate=0.0001, burst=5, key_prefix="itest:")
    grants = sum(await asyncio.gather(*[bucket.consume("k", tokens=1.0) for _ in range(50)]))
    assert grants == 5
    # The real script applied PEXPIRE; the key must exist with a bounded reclaim
    # TTL — NOT a stale worker ``now`` (~1.75e9 ms -> ~20-day expiry).
    ttl = await redis_client.pttl("itest:k")
    expected = _expected_ttl_ms(0.0001, 5)
    assert 1 <= ttl <= expected


async def test_real_lua_refills_over_server_wall_clock(
    redis_client: aioredis.Redis,
) -> None:
    """The shared bucket refills off the server's ``TIME``, not a worker clock."""
    bucket = RedisTokenBucket(redis_client, rate=2.0, burst=1, key_prefix="itest:")
    assert await bucket.consume("k", tokens=1.0) is True
    # Immediately after spend, no refill -> denied.
    assert await bucket.consume("k", tokens=1.0) is False
    # 2/s rate: ~0.6s refills a token.
    await asyncio.sleep(0.6)
    assert await bucket.consume("k", tokens=1.0) is True
    ttl = await redis_client.pttl("itest:k")
    expected = _expected_ttl_ms(2.0, 1)
    assert 1 <= ttl <= expected


async def test_shared_budget_enforced_across_workers_real_redis(
    redis_client: aioredis.Redis,
) -> None:
    """Two limiter instances (two workers) share ONE real Redis budget."""
    a = PerDestinationRateLimiter(
        rate=0.0001, burst=3, redis_client=redis_client, tenant_id="org-1", key_prefix="itest:"
    )
    b = PerDestinationRateLimiter(
        rate=0.0001, burst=3, redis_client=redis_client, tenant_id="org-1", key_prefix="itest:"
    )
    results: list[bool] = []
    for _ in range(5):
        results.append(await a.consume("api.example.com/x"))
        results.append(await b.consume("api.example.com/x"))
    # 3-token budget shared by both workers -> 3 grants, 7 denies (no refill).
    assert results.count(True) == 3
    assert not a.buckets  # never fell back to a local bucket


async def test_per_tenant_budgets_separated_real_redis(
    redis_client: aioredis.Redis,
) -> None:
    """Different tenants get independent shared budgets for the same destination."""
    tenant_a = PerDestinationRateLimiter(
        rate=0.0001, burst=2, redis_client=redis_client, tenant_id="org-A", key_prefix="itest:"
    )
    tenant_b = PerDestinationRateLimiter(
        rate=0.0001, burst=2, redis_client=redis_client, tenant_id="org-B", key_prefix="itest:"
    )
    for _ in range(2):
        assert await tenant_a.consume("dest") is True
    assert await tenant_a.consume("dest") is False  # org-A exhausted

    # org-B still has its own full budget (no cross-tenant leak).
    for _ in range(2):
        assert await tenant_b.consume("dest") is True
    assert await tenant_b.consume("dest") is False

    assert tenant_a.key("dest") != tenant_b.key("dest")


async def test_fail_closed_over_real_redis_outage() -> None:
    """A configured-but-unreachable real Redis client drives fail-closed.

    This exercises the real ``redis.asyncio`` connection-failure path (not the
    unit-suite fake): a configured shared budget whose Redis is down MUST raise
    :class:`SharedBudgetUnavailableError` and never mint a per-process token.
    """
    broken = aioredis.from_url("redis://localhost:1/", socket_connect_timeout=1, socket_timeout=1)
    limiter = PerDestinationRateLimiter(rate=1.0, burst=2, redis_client=broken, tenant_id="org-1")
    with pytest.raises(SharedBudgetUnavailableError):
        await limiter.consume("dest")
    assert not limiter.buckets  # never fell back to a per-process bucket
    await broken.aclose()
