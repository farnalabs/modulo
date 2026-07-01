"""Unit tests for AuthRateLimiter and AuthRateLimitMiddleware."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware
from modulo.core.rate_limiter import AuthRateLimiter
from modulo.settings import Settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_ratelimit_bypass_token="test-bypass",
    )


class TestComputeBackoff:
    @pytest.mark.parametrize(
        ("failures", "expected"),
        [
            (10, 60),
            (20, 120),
            (30, 240),
            (40, 480),
            (50, 960),
            (100, 3600),
            (200, 3600),
        ],
    )
    def test_backoff_values(self, failures: int, expected: int) -> None:
        assert AuthRateLimiter._compute_backoff(failures) == expected


class TestAuthRateLimiterInMemory:
    @pytest.fixture
    def limiter(self) -> AuthRateLimiter:
        return AuthRateLimiter(redis_client=None, max_attempts=10, window_s=60)

    async def test_check_login_allows_when_under_limit(self, limiter: AuthRateLimiter) -> None:
        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is True
        assert retry_after == 0

    async def test_check_login_allows_under_max_attempts(self, limiter: AuthRateLimiter) -> None:
        for _ in range(9):
            await limiter.record_failure("1.2.3.4")
        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is True
        assert retry_after == 0

    async def test_check_login_blocks_when_over_limit(self, limiter: AuthRateLimiter) -> None:
        for _ in range(10):
            await limiter.record_failure("1.2.3.4")
        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is False
        assert retry_after == 60

    async def test_record_failure_increments_counter(self, limiter: AuthRateLimiter) -> None:
        await limiter.record_failure("1.2.3.4")
        await limiter.record_failure("1.2.3.4")
        allowed, _ = await limiter.check_login("1.2.3.4")
        assert allowed is True
        for _ in range(8):
            await limiter.record_failure("1.2.3.4")
        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is False
        assert retry_after == 60

    async def test_record_success_resets_counter(self, limiter: AuthRateLimiter) -> None:
        for _ in range(10):
            await limiter.record_failure("1.2.3.4")
        allowed, _ = await limiter.check_login("1.2.3.4")
        assert allowed is False

        await limiter.record_success("1.2.3.4")
        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is True
        assert retry_after == 0

    async def test_ips_are_independent(self, limiter: AuthRateLimiter) -> None:
        for _ in range(10):
            await limiter.record_failure("1.2.3.4")
        blocked, _ = await limiter.check_login("1.2.3.4")
        assert blocked is False

        allowed, _ = await limiter.check_login("5.6.7.8")
        assert allowed is True

    async def test_old_failures_pruned_outside_window(self, limiter: AuthRateLimiter) -> None:
        now = time.time()

        with patch.object(time, "time", return_value=now):
            for _ in range(10):
                await limiter.record_failure("1.2.3.4")
            blocked, _ = await limiter.check_login("1.2.3.4")
            assert blocked is False

        with patch.object(time, "time", return_value=now + 120):
            allowed, retry_after = await limiter.check_login("1.2.3.4")
            assert allowed is True
            assert retry_after == 0

    async def test_lockout_persists_across_checks(self, limiter: AuthRateLimiter) -> None:
        for _ in range(10):
            await limiter.record_failure("1.2.3.4")
        blocked1, _retry1 = await limiter.check_login("1.2.3.4")
        assert blocked1 is False
        blocked2, retry2 = await limiter.check_login("1.2.3.4")
        assert blocked2 is False
        assert retry2 > 0


class TestAuthRateLimiterRedis:
    @pytest.fixture
    def mock_redis(self):
        return MagicMock()

    @pytest.fixture
    def limiter(self, mock_redis):
        return AuthRateLimiter(redis_client=mock_redis, max_attempts=10, window_s=60)

    async def test_check_login_allows_when_under_limit(self, limiter, mock_redis) -> None:
        mock_redis.ttl = AsyncMock(return_value=-2)
        pipe = MagicMock()
        pipe.zremrangebyscore = MagicMock(return_value=pipe)
        pipe.zcard = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(None, 5))
        mock_redis.pipeline = MagicMock(return_value=pipe)

        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is True
        assert retry_after == 0

    async def test_check_login_blocks_when_over_limit(self, limiter, mock_redis) -> None:
        mock_redis.ttl = AsyncMock(return_value=-2)
        mock_redis.setex = AsyncMock()
        pipe = MagicMock()
        pipe.zremrangebyscore = MagicMock(return_value=pipe)
        pipe.zcard = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(None, 10))
        mock_redis.pipeline = MagicMock(return_value=pipe)

        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is False
        assert retry_after == 60

    async def test_active_lockout_returns_ttl(self, limiter, mock_redis) -> None:
        mock_redis.ttl = AsyncMock(return_value=45)

        allowed, retry_after = await limiter.check_login("1.2.3.4")
        assert allowed is False
        assert retry_after == 45

    async def test_record_failure_adds_to_sorted_set(self, limiter, mock_redis) -> None:
        mock_redis.zadd = AsyncMock()
        mock_redis.expire = AsyncMock()

        await limiter.record_failure("1.2.3.4")

        mock_redis.zadd.assert_awaited_once()
        mock_redis.expire.assert_awaited_once()

    async def test_record_success_deletes_keys(self, limiter, mock_redis) -> None:
        pipe = MagicMock()
        pipe.delete = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(True, True))
        mock_redis.pipeline = MagicMock(return_value=pipe)

        await limiter.record_success("1.2.3.4")

        pipe.delete.assert_any_call("auth_ratelimit:1.2.3.4")
        pipe.delete.assert_any_call("auth_ratelimit:lockout:1.2.3.4")
        pipe.execute.assert_awaited_once()


def _make_app(rate_limiter: AuthRateLimiter | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    async def login():
        return {"access_token": "mock", "refresh_token": "mock"}

    app.add_middleware(
        AuthRateLimitMiddleware,
        settings=_make_settings(),
        rate_limiter=rate_limiter,
    )
    return app


class TestAuthRateLimitMiddleware:
    def test_get_request_not_rate_limited(self):
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_login_allowed_when_under_limit(self):
        mock_limiter = MagicMock(spec=AuthRateLimiter)
        mock_limiter.check_login = AsyncMock(return_value=(True, 0))
        app = _make_app(rate_limiter=mock_limiter)
        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")
        assert resp.status_code == 200

    def test_login_returns_429_when_rate_limited(self):
        mock_limiter = MagicMock(spec=AuthRateLimiter)
        mock_limiter.check_login = AsyncMock(return_value=(False, 120))
        app = _make_app(rate_limiter=mock_limiter)
        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")
        assert resp.status_code == 429
        body = resp.json()
        assert body["error_code"] == "rate_limit_exceeded"

    def test_retry_after_header_present_on_429(self):
        mock_limiter = MagicMock(spec=AuthRateLimiter)
        mock_limiter.check_login = AsyncMock(return_value=(False, 60))
        app = _make_app(rate_limiter=mock_limiter)
        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") == "60"

    def test_bypass_token_skips_rate_limit(self):
        mock_limiter = MagicMock(spec=AuthRateLimiter)
        mock_limiter.check_login = AsyncMock(return_value=(False, 120))
        app = _make_app(rate_limiter=mock_limiter)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/auth/login",
                headers={"MODULO_RATELIMIT_BYPASS_TOKEN": "test-bypass"},
            )
        assert resp.status_code == 200
        mock_limiter.check_login.assert_not_called()

    def test_wrong_bypass_token_does_not_skip(self):
        mock_limiter = MagicMock(spec=AuthRateLimiter)
        mock_limiter.check_login = AsyncMock(return_value=(False, 120))
        app = _make_app(rate_limiter=mock_limiter)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/auth/login",
                headers={"MODULO_RATELIMIT_BYPASS_TOKEN": "wrong-token"},
            )
        assert resp.status_code == 429

    def test_non_auth_path_not_rate_limited(self):
        mock_limiter = MagicMock(spec=AuthRateLimiter)
        mock_limiter.check_login = AsyncMock(return_value=(False, 120))
        app = _make_app(rate_limiter=mock_limiter)
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        mock_limiter.check_login.assert_not_called()
