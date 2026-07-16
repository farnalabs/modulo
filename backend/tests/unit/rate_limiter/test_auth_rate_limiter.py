"""Unit tests for AuthRateLimiter and AuthRateLimitMiddleware.

Covers:
  - get_auth_rate_limiter returns None when modulo_auth_rate_limit_enabled=False
  - get_auth_rate_limiter singleton behavior
  - AuthRateLimitMiddleware skips rate limiting when _rate_limiter is None
  - _client_key None-host edge case
"""

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import (
    AuthRateLimitMiddleware,
    get_auth_rate_limiter,
)
from modulo.core.rate_limiter import AuthRateLimiter as AuthRateLimiterCls
from modulo.settings import Settings


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
    no_redis: bool = False,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_auth_rate_limit_enabled=enabled,
        modulo_auth_max_attempts=max_attempts,
        modulo_auth_window_seconds=window_s,
        redis_url="" if no_redis else "redis://localhost:6379/0",
    )


def _make_app(
    settings: Settings | None = None,
    rate_limiter: AuthRateLimiterCls | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/auth/login")
    async def login():
        return {"token": "dummy"}

    resolved = settings or _make_settings(no_redis=True)
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

    def test_returns_in_memory_when_no_redis(self):
        settings = _make_settings(enabled=True, no_redis=True)
        limiter = get_auth_rate_limiter(settings)
        assert limiter is not None
        assert limiter._redis is None

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
    def test_allows_within_limit(self):
        limiter = AuthRateLimiterCls(
            redis_client=None,
            max_attempts=10,
            window_s=60,
        )
        app = _make_app(rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 200

    def test_blocks_when_exceeded(self):
        """Pre-populate failures, then verify the middleware blocks."""
        limiter = AuthRateLimiterCls(
            redis_client=None,
            max_attempts=1,
            window_s=60,
        )
        limiter._record_failure_memory("testclient")
        app = _make_app(rate_limiter=limiter, settings=_make_settings(no_redis=True))

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 429

    def test_429_has_retry_after_header(self):
        limiter = AuthRateLimiterCls(
            redis_client=None,
            max_attempts=0,
            window_s=60,
        )
        app = _make_app(rate_limiter=limiter, settings=_make_settings(no_redis=True))

        with TestClient(app) as client:
            resp = client.post("/api/v1/auth/login")

        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_get_not_rate_limited(self):
        """GET requests to auth paths should not be rate limited."""
        app = FastAPI()

        @app.get("/api/v1/auth/login")
        async def login_get():
            return {"token": "dummy"}

        limiter = AuthRateLimiterCls(
            redis_client=None,
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
