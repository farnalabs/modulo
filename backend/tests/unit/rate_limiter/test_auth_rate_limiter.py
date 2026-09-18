"""Unit tests for AuthRateLimiter and AuthRateLimitMiddleware.

Covers:
  - get_auth_rate_limiter returns None when modulo_auth_rate_limit_enabled=False
  - get_auth_rate_limiter singleton behavior
  - AuthRateLimitMiddleware skips rate limiting when _rate_limiter is None
  - _client_key None-host edge case
  - AuthRateLimiter check_login/record_failure/record_success/backoff paths
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.rate_limiter.helpers import make_mock_request, make_settings

from modulo.api.middleware.rate_limiter import (
    RATELIMIT_BYPASS_HEADER,
    AuthRateLimitMiddleware,
    get_auth_rate_limiter,
    shutdown_rate_limiters,
)
from modulo.core.rate_limiter import AuthRateLimiter as AuthRateLimiterCls
from modulo.settings import Settings


def _make_app(
    settings: Settings | None = None,
    rate_limiter: AuthRateLimiterCls | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/auth/login")
    async def login():
        return {"token": "dummy"}

    resolved = settings or make_settings()
    app.add_middleware(
        AuthRateLimitMiddleware,
        settings=resolved,
        rate_limiter=rate_limiter,
    )
    return app


class TestGetAuthRateLimiter:
    def test_returns_none_when_disabled(self):
        settings = make_settings(modulo_auth_rate_limit_enabled=False)
        limiter = get_auth_rate_limiter(settings)
        assert limiter is None

    def test_returns_limiter_when_enabled(self, monkeypatch):
        monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda url, **kwargs: MagicMock())
        settings = make_settings(modulo_auth_rate_limit_enabled=True)
        limiter = get_auth_rate_limiter(settings)
        assert limiter is not None
        assert isinstance(limiter, AuthRateLimiterCls)

    def test_returns_none_when_no_redis(self):
        """get_auth_rate_limiter returns None when REDIS_URL is empty (graceful fallback)."""
        settings = make_settings(modulo_auth_rate_limit_enabled=True, redis_url="")
        limiter = get_auth_rate_limiter(settings)
        assert limiter is None

    def test_singleton_returns_same_instance(self, monkeypatch):
        monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda url, **kwargs: MagicMock())
        settings = make_settings(modulo_auth_rate_limit_enabled=True)
        first = get_auth_rate_limiter(settings)
        second = get_auth_rate_limiter(settings)
        assert first is second

    def test_redis_connect_failure_returns_none(self, monkeypatch):
        """A Redis connect error must degrade to None (rate limiting skipped), not raise."""
        from modulo.api.middleware import rate_limiter as rl_mod

        before_clients = set(rl_mod._redis_clients)
        monkeypatch.setattr(
            "redis.asyncio.Redis.from_url",
            MagicMock(side_effect=ConnectionError("boom")),
        )
        limiter = get_auth_rate_limiter(make_settings(modulo_auth_rate_limit_enabled=True))
        assert limiter is None
        assert rl_mod._auth_rate_limiter is None
        assert rl_mod._redis_clients == before_clients

    def test_redis_cancelled_error_propagates(self, monkeypatch):
        """asyncio.CancelledError must never be swallowed by get_auth_rate_limiter."""
        from modulo.api.middleware import rate_limiter as rl_mod

        before_clients = set(rl_mod._redis_clients)
        monkeypatch.setattr(
            "redis.asyncio.Redis.from_url",
            MagicMock(side_effect=asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            get_auth_rate_limiter(make_settings(modulo_auth_rate_limit_enabled=True))
        assert rl_mod._redis_clients == before_clients


class TestAuthRateLimitMiddlewareDisabled:
    def test_skips_rate_limiting_when_limiter_is_none(self):
        """When modulo_auth_rate_limit_enabled=False, middleware passes through."""
        app = _make_app(settings=make_settings(modulo_auth_rate_limit_enabled=False))

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
            settings=make_settings(modulo_auth_rate_limit_enabled=True),
            rate_limiter=limiter,
        )

        with TestClient(app) as client:
            resp = client.get("/api/v1/auth/login")

        assert resp.status_code == 200

    def test_non_auth_path_not_rate_limited(self, mock_redis):
        """POSTs to non-auth paths should pass through untouched."""
        app = FastAPI()

        @app.post("/api/v1/other")
        async def other():
            return {"ok": True}

        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=0,
            window_s=60,
        )

        app.add_middleware(
            AuthRateLimitMiddleware,
            settings=make_settings(modulo_auth_rate_limit_enabled=True),
            rate_limiter=limiter,
        )

        with TestClient(app) as client:
            resp = client.post("/api/v1/other")

        assert resp.status_code == 200

    def test_valid_bypass_token_skips_rate_limit(self, mock_redis):
        """A valid MODULO_RATELIMIT_BYPASS_TOKEN should skip the auth rate limit."""
        mock_redis.ttl = AsyncMock(return_value=30)
        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=0,
            window_s=60,
        )
        settings = make_settings(modulo_auth_rate_limit_enabled=True)
        settings.modulo_ratelimit_bypass_token = "bypass-secret"
        app = _make_app(settings=settings, rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/auth/login",
                headers={RATELIMIT_BYPASS_HEADER: "bypass-secret"},
            )

        assert resp.status_code == 200
        mock_redis.ttl.assert_not_awaited()

    def test_wrong_bypass_token_still_rate_limited(self, mock_redis):
        """An invalid bypass token should still be rate limited."""
        mock_redis.ttl = AsyncMock(return_value=30)
        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=0,
            window_s=60,
        )
        settings = make_settings(modulo_auth_rate_limit_enabled=True)
        settings.modulo_ratelimit_bypass_token = "bypass-secret"
        app = _make_app(settings=settings, rate_limiter=limiter)

        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/auth/login",
                headers={RATELIMIT_BYPASS_HEADER: "wrong-token"},
            )

        assert resp.status_code == 429

    async def test_dispatch_propagates_cancelled_error(self, mock_redis):
        """asyncio.CancelledError from check_login must propagate through dispatch."""
        mock_redis.ttl = AsyncMock(side_effect=asyncio.CancelledError())
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        mw = AuthRateLimitMiddleware(
            app=FastAPI(),
            settings=make_settings(modulo_auth_rate_limit_enabled=True),
            rate_limiter=limiter,
        )
        request = make_mock_request(path="/api/v1/auth/login", headers={}, client=None)
        call_next = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await mw.dispatch(request, call_next)
        call_next.assert_not_awaited()


class TestClientKeyEdgeCases:
    def test_client_host_preferred_over_x_forwarded_for(self):
        """_client_ip should prefer request.client.host over X-Forwarded-For."""
        request = make_mock_request(
            path="/api/v1/auth/login",
            headers={"X-Forwarded-For": "203.0.113.42"},
            client=MagicMock(host="192.0.2.1"),
        )
        ip = AuthRateLimitMiddleware._client_ip(request)
        assert ip == "192.0.2.1"

    def test_x_forwarded_for_uses_first_hop_ip(self):
        """_client_ip should take the first entry of a comma-separated XFF list."""
        request = make_mock_request(
            path="/api/v1/auth/login",
            headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1, 172.16.0.9"},
        )
        ip = AuthRateLimitMiddleware._client_ip(request)
        assert ip == "198.51.100.7"

    def test_client_host_falls_back_when_no_xff(self):
        """_client_ip should use request.client.host when no XFF header is present."""
        request = make_mock_request(path="/api/v1/auth/login", client=MagicMock(host="192.0.2.1"))
        ip = AuthRateLimitMiddleware._client_ip(request)
        assert ip == "192.0.2.1"

    def test_no_client_host_falls_back_to_unknown(self):
        """_client_ip should handle request.client being truthy but host being None."""
        client = MagicMock()
        client.host = None
        request = make_mock_request(path="/api/v1/auth/login", client=client)
        ip = AuthRateLimitMiddleware._client_ip(request)
        assert ip == "unknown"

    def test_no_client_falls_back_to_unknown(self):
        request = make_mock_request(path="/api/v1/auth/login", client=None)
        ip = AuthRateLimitMiddleware._client_ip(request)
        assert ip == "unknown"

    def test_x_forwarded_for_first_hop_is_stripped(self):
        """The first XFF hop must have surrounding whitespace stripped."""
        request = make_mock_request(
            path="/api/v1/auth/login",
            headers={"X-Forwarded-For": " 198.51.100.7 , 10.0.0.1"},
        )
        ip = AuthRateLimitMiddleware._client_ip(request)
        assert ip == "198.51.100.7"


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

    async def test_check_login_prunes_old_failures(self, mock_redis):
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=90)
        await limiter.check_login("203.0.113.5")
        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyscore.assert_called_once()
        _, lower_bound, cutoff = pipe.zremrangebyscore.call_args[0]
        assert lower_bound == 0
        now = time.time()
        assert abs(cutoff - (now - 90)) < 2

    async def test_check_login_within_max_does_not_set_lockout(self, mock_redis):
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, 3))
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        allowed, retry_after = await limiter.check_login("203.0.113.5")
        assert allowed is True
        assert retry_after == 0
        mock_redis.setex.assert_not_awaited()

    @pytest.mark.parametrize("ttl_value", [0, -1, -2])
    async def test_lockout_not_active_does_not_block(self, mock_redis, ttl_value):
        """ttl of 0 (expired), -1 (no expiry) or -2 (missing key) must not block."""
        mock_redis.ttl = AsyncMock(return_value=ttl_value)
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, 3))
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        allowed, retry_after = await limiter.check_login("203.0.113.5")
        assert allowed is True
        assert retry_after == 0

    async def test_check_login_backoff_scales_with_tier(self, mock_redis):
        """Past the max_attempts boundary, backoff doubles per tier through check_login."""
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, 20))
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        allowed, retry_after = await limiter.check_login("203.0.113.5")
        assert allowed is False
        assert retry_after == 120
        mock_redis.setex.assert_awaited_once()
        _, backoff, _ = mock_redis.setex.await_args.args
        assert backoff == 120

    async def test_record_failure_then_check_login_blocks_at_max(self, mock_redis):
        mock_redis.pipeline.return_value.execute = AsyncMock(return_value=(None, 10))
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        await limiter.record_failure("203.0.113.5")
        allowed, retry_after = await limiter.check_login("203.0.113.5")
        assert allowed is False
        assert retry_after == 60
        mock_redis.setex.assert_awaited_once()

    async def test_record_failure_adds_timestamp_and_expiry(self, mock_redis):
        limiter = AuthRateLimiterCls(redis_client=mock_redis, max_attempts=10, window_s=60)
        await limiter.record_failure("203.0.113.5")
        mock_redis.zadd.assert_awaited_once()
        redis_key, member = mock_redis.zadd.await_args.args
        assert redis_key == "auth_ratelimit:203.0.113.5"
        ((ts, score),) = member.items()
        now = time.time()
        assert abs(score - now) < 2
        ts_str, sep, suffix = ts.partition(":")
        assert sep == ":"
        assert ts_str == str(score)
        assert len(suffix) == 32
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

    def test_requires_redis_client(self):
        with pytest.raises(ValueError, match="requires a Redis client"):
            AuthRateLimiterCls(redis_client=None)

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (10, 60),
            (20, 120),
            (30, 240),
            (100, 3600),
        ],
    )
    def test_compute_backoff_caps_at_3600(self, count, expected):
        limiter = AuthRateLimiterCls(redis_client=MagicMock(), max_attempts=10, window_s=60)
        assert limiter._compute_backoff(count) == expected

    def test_compute_backoff_tier_boundaries(self):
        limiter = AuthRateLimiterCls(redis_client=MagicMock(), max_attempts=10, window_s=60)
        assert limiter._compute_backoff(10) == 60
        assert limiter._compute_backoff(11) == 60
        assert limiter._compute_backoff(19) == 60

    def test_compute_backoff_respects_configured_max_attempts(self):
        """Backoff tiers must be derived from max_attempts, not a hardcoded 10."""
        limiter = AuthRateLimiterCls(redis_client=MagicMock(), max_attempts=5, window_s=60)
        assert limiter._compute_backoff(5) == 60
        assert limiter._compute_backoff(9) == 60
        assert limiter._compute_backoff(10) == 120

    def test_compute_backoff_does_not_divide_by_zero(self):
        """_compute_backoff must not ZeroDivisionError when max_attempts is 0."""
        limiter = AuthRateLimiterCls(redis_client=MagicMock(), max_attempts=0, window_s=60)
        assert limiter._compute_backoff(0) == 30
        assert limiter._compute_backoff(1) == 60
        assert limiter._compute_backoff(10) == 3600


class TestShutdownRateLimiters:
    async def test_shutdown_closes_all_clients(self):
        from modulo.api.middleware import rate_limiter as rl_mod

        client1 = AsyncMock()
        client2 = AsyncMock()
        rl_mod._redis_clients.update({client1, client2})
        await shutdown_rate_limiters()
        client1.aclose.assert_awaited_once()
        client2.aclose.assert_awaited_once()
        assert not rl_mod._redis_clients

    async def test_shutdown_survives_close_errors(self):
        from modulo.api.middleware import rate_limiter as rl_mod

        failing = MagicMock()
        failing.aclose = AsyncMock(side_effect=ConnectionError("boom"))
        ok = AsyncMock()
        rl_mod._redis_clients.update({failing, ok})
        await shutdown_rate_limiters()
        failing.aclose.assert_awaited_once()
        ok.aclose.assert_awaited_once()
        assert not rl_mod._redis_clients

    async def test_shutdown_propagates_cancelled_error(self):
        """asyncio.CancelledError from aclose must propagate, not be logged."""
        from modulo.api.middleware import rate_limiter as rl_mod

        failing = MagicMock()
        failing.aclose = AsyncMock(side_effect=asyncio.CancelledError())
        rl_mod._redis_clients.update({failing})
        try:
            with pytest.raises(asyncio.CancelledError):
                await shutdown_rate_limiters()
        finally:
            rl_mod._redis_clients.clear()
