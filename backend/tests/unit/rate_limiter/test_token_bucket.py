"""Unit tests for the in-memory TokenBucket and TokenBucketRegistry."""

import asyncio

import pytest

from modulo.core.rate_limiter import TokenBucket, TokenBucketRegistry


class FakeClock:
    """Deterministic ``time.monotonic`` replacement: time only advances when
    the test explicitly calls ``advance``, so refill math is exact and the
    test never depends on real wall-clock sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class TestTokenBucket:
    async def test_full_bucket_allows_burst(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=60)
        for _ in range(60):
            assert await bucket.consume() is True
        assert await bucket.consume() is False

    async def test_exact_burst_boundary(self) -> None:
        bucket = TokenBucket(rate=0.5, burst=3)
        assert await bucket.consume() is True
        assert await bucket.consume() is True
        assert await bucket.consume() is True
        assert await bucket.consume() is False

    async def test_refills_over_time(self) -> None:
        clock = FakeClock()
        bucket = TokenBucket(rate=2.0, burst=10, clock=clock)
        for _ in range(10):
            assert await bucket.consume() is True
        assert await bucket.consume() is False

        clock.advance(0.5 + 1 / 2.0)  # enough time for 1+ tokens at rate 2/s
        assert await bucket.consume() is True

    async def test_never_exceeds_burst_ceiling(self) -> None:
        clock = FakeClock()
        bucket = TokenBucket(rate=100.0, burst=5, clock=clock)
        assert await bucket.consume() is True
        clock.advance(0.05)
        bucket.reset()
        for _ in range(5):
            assert await bucket.consume() is True
        assert await bucket.consume() is False

    async def test_consume_zero_tokens_rejected(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=1)
        with pytest.raises(ValueError, match="tokens must be > 0"):
            await bucket.consume(0)

    def test_constructor_validates_rate(self) -> None:
        with pytest.raises(ValueError, match="rate must be > 0"):
            TokenBucket(rate=0, burst=1)
        with pytest.raises(ValueError, match="rate must be > 0"):
            TokenBucket(rate=-1, burst=1)

    def test_constructor_validates_burst(self) -> None:
        with pytest.raises(ValueError, match="burst must be > 0"):
            TokenBucket(rate=1.0, burst=0)
        with pytest.raises(ValueError, match="burst must be > 0"):
            TokenBucket(rate=1.0, burst=-5)

    async def test_concurrent_consume_never_overdraws(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=10)
        results = await asyncio.gather(*[bucket.consume() for _ in range(50)])
        assert sum(results) == 10
        assert await bucket.consume() is False

    async def test_reset_restores_full_capacity(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=4)
        assert await bucket.consume() is True
        assert bucket._tokens < bucket.burst
        bucket.reset()
        assert bucket._tokens == bucket.burst


class TestTokenBucketRegistry:
    async def test_lazy_bucket_creation_per_key(self) -> None:
        registry = TokenBucketRegistry(rate=1.0, burst=2)
        assert await registry.consume("client-a") is True
        assert await registry.consume("client-a") is True
        assert await registry.consume("client-a") is False
        assert await registry.consume("client-b") is True

    async def test_clients_are_isolated(self) -> None:
        registry = TokenBucketRegistry(rate=1.0, burst=1)
        assert await registry.consume("client-a") is True
        assert await registry.consume("client-a") is False
        assert await registry.consume("client-b") is True

    async def test_reset_clears_buckets(self) -> None:
        registry = TokenBucketRegistry(rate=1.0, burst=1)
        assert await registry.consume("client-a") is True
        registry.reset()
        assert await registry.consume("client-a") is True

    async def test_concurrent_consume_registry(self) -> None:
        registry = TokenBucketRegistry(rate=1.0, burst=5)
        results = await asyncio.gather(*[registry.consume("k") for _ in range(20)])
        assert sum(results) == 5
