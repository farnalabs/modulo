"""Unit tests for AuthRateLimiter and AuthRateLimitMiddleware.

Covers:
  - get_auth_rate_limiter returns None when modulo_auth_rate_limit_enabled=False
  - get_auth_rate_limiter singleton behavior
  - AuthRateLimitMiddleware skips rate limiting when _rate_limiter is None
  - _client_key None-host edge case
  - AuthRateLimiter check_login/record_failure/record_success/backoff paths
"""

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import (
    AuthRateLimitMiddleware,
    get_auth_rate_limiter,
)
from modulo.core.rate_limiter import AuthRateLimiter as AuthRateLimiterCls
from modulo.settings import Settings


@pytest.fixture
def mock_redis():
    client = MagicMock()
    client.ttl = AsyncMock(return_value=0)
    pipe = MagicMock()
    pipe.zremrangebyscore = MagicMock(return_value=pipe)
    pipe.zcard = MagicMock(return_value=pipe)
    pipe.delete = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=(None, 0))
    client.pipeline = MagicMock(return_value=pipe)
    client.zadd = AsyncMock()
    client.expire = AsyncMock()
    client.delete = AsyncMock()
    client.setex = AsyncMock()
    return client


@pytest.fixture(autouse=True)
def _reset_singleton() -> Generator[None, None, None]:
    """Reset the module-level _auth_rate_limiter before and after each test."""
    from modulo.api.middleware import rate_limiter as rl_mod

    saved = rl_mod._auth_rate_limiter
    rl_mod._auth_rate_limiter = None
    yield
    rl_mod._auth_rate_limiter = saved


def _make_settings(
    enabled: bool = True,
    max_attempts: int = 10,
    window_s: int = 60,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_auth_rate_limit_enabled=enabled,
        modulo_auth_max_attempts=max_attempts,
        modulo_auth_window_seconds=window_s,
        redis_url="redis://localhost:6379/0",
    )


def _make_app(
    settings: Settings | None = None,
    rate_limiter: AuthRateLimiterCls | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/auth/login")
    async def login():
        return {"token": "dummy"}

    resolved = settings or _make_settings()
    app.add_middleware(
        AuthRateLimitMiddleware,
        settings=resolved,
        rate_limiter=rate_limiter,
    )
    return app


class TestGetAuthRateLimiter:
    def test_returns_none_when_disabled(self):
        settings = _make_settings(enabled=False)
        limiter = get_auth_rate_limiter(settings)
        assert limiter is None

    def test_returns_limiter_when_enabled(self):
        settings = _make_settings(enabled=True)
        limiter = get_auth_rate_limiter(settings)
        assert limiter is not None
        assert isinstance(limiter, AuthRateLimiterCls)

    def test_returns_none_when_no_redis(self):
        """get_auth_rate_limiter returns None when REDIS_URL is empty (graceful fallback)."""
        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
            modulo_auth_rate_limit_enabled=True,
            redis_url="",
        )
        limiter = get_auth_rate_limiter(settings)
        assert limiter is None

    def test_singleton_returns_same_instance(self):
        settings = _make_settings(enabled=True)
        first = get_auth_rate_limiter(settings)
        second = get_auth_rate_limiter(settings)
        assert first is second


class TestAuthRateLimitMiddlewareDisabled:
    def test_skips_rate_limiting_when_limiter_is_none(self):
        """When modulo_auth_rate_limit_enabled=False, middleware passes through."""
        app = _make_app(settings=_make_settings(enabled=False))

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 200


class TestAuthRateLimitMiddlewareEnabled:
    def test_allows_within_limit(self, mock_redis):
        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=10,
            window_s=60,
        )
        app = _make_app(rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 200

    def test_blocks_when_exceeded(self, mock_redis):
        """When a lockout is already in place, the middleware blocks."""
        mock_redis.ttl = AsyncMock(return_value=30)
        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=1,
            window_s=60,
        )
        app = _make_app(rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 429

    def test_blocks_when_failure_count_exceeds_max(self, mock_redis):
        """When failures >= max_attempts, the middleware blocks with backoff."""
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, 10))
        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=10,
            window_s=60,
        )
        app = _make_app(rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_429_has_retry_after_header(self, mock_redis):
        mock_redis.ttl = AsyncMock(return_value=30)
        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=0,
            window_s=60,
        )
        app = _make_app(rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_get_not_rate_limited(self, mock_redis):
        """GET requests to auth paths should not be rate limited."""
        app = FastAPI()

        @app.get("/api/v1/auth/login")
        async def login_get():
            return {"token": "dummy"}

        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=0,
            window_s=60,
        )

        app.add_middleware(
            AuthRateLimitMiddleware,
            settings=_make_settings(enabled=True),
            rate_limiter=limiter,
        )

        with TestClient(app) as client:
            resp = client.get("/api/v1/auth/login")

        assert resp.status_code == 200


class TestClientKeyEdgeCases:
    def test_x_forwarded_for_is_used_when_present(self):
        """_client_ip should prefer X-Forwarded-For header when present."""
        from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/api/v1/auth/login"
        mock_request.headers.get = MagicMock(return_value="203.0.113.42")
        mock_request.client = None

        ip = AuthRateLimitMiddleware._client_ip(mock_request)
        assert ip == "203.0.113.42"

    def test_no_client_host_falls_back_to_unknown(self):
        """_client_ip should handle request.client being truthy but host being None."""
        from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/api/v1/auth/login"
        mock_request.headers.get = MagicMock(return_value="")
        mock_request.client = MagicMock()
        mock_request.client.host = None

        ip = AuthRateLimitMiddleware._client_ip(mock_request)
        assert ip == "unknown"

    def test_no_client_falls_back_to_unknown(self):
        from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/api/v1/auth/login"
        mock_request.headers.get = MagicMock(return_value="")
        mock_request.client = None

        ip = AuthRateLimitMiddleware._client_ip(mock_request)
        assert ip == "unknown"


class TestAuthRateLimiterCore:
    """Direct unit tests for AuthRateLimiter (no middleware/HTTP involved)."""

    async def test_check_login_allowed_under_max(self, mock_redis):
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        allowed, retry_after = await limiter.check_login("203.0.113.5")
        assert allowed is True
        assert retry_after == 0

    async def test_check_login_blocks_on_active_lockout(self, mock_redis):
        mock_redis.ttl = AsyncMock(return_value=45)
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        allowed, retry_after = await limiter.check_login("203.0.113.5")
        assert allowed is False
        assert retry_after == 45

    async def test_check_login_sets_lockout_when_at_max(self, mock_redis):
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, 10))
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        allowed, retry_after = await limiter.check_login("203.0.113.5")
        assert allowed is False
        assert retry_after == 60
        mock_redis.setex.assert_awaited_once()
        lockout_key, backoff, _ = mock_redis.setex.await_args.args
        assert lockout_key == "auth_ratelimit:lockout:203.0.113.5"
        assert backoff == 60

    async def test_check_login_uses_configured_window(self, mock_redis):
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=90)
        await limiter.check_login("203.0.113.5")
        pipe = mock_redis.pipeline.return_value
        _, _, cutoff = pipe.zremrangebyscore.call_args[0]
        now = time.time()
        assert abs(cutoff - (now - 90)) < 2

    async def test_record_failure_adds_timestamp_and_expiry(self, mock_redis):
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        await limiter.record_failure("203.0.113.5")
        mock_redis.zadd.assert_awaited_once()
        redis_key, member = mock_redis.zadd.await_args.args
        assert redis_key == "auth_ratelimit:203.0.113.5"
        ((ts, score),) = member.items()
        now = time.time()
        assert abs(score - now) < 2
        assert ts == str(score)
        mock_redis.expire.assert_awaited_once_with("auth_ratelimit:203.0.113.5", 120)

    async def test_record_success_resets_failure_and_lockout(self, mock_redis):
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        await limiter.record_success("203.0.113.5")
        pipe = mock_redis.pipeline.return_value
        assert pipe.delete.call_args_list == [
            [("auth_ratelimit:203.0.113.5",)],
            [("auth_ratelimit:lockout:203.0.113.5",)],
        ]
        pipe.execute.assert_awaited_once()

    @pytest.mark.parametrize(
        "count,expected",
        [
            (10, 60),
            (20, 120),
            (30, 240),
            (100, 3600),
        ],
    )
    def test_compute_backoff_caps_at_3600(self, count, expected):
        assert AuthRateLimiterCls._compute_backoff(count) == expected
