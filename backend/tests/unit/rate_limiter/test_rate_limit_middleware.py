"""Unit tests for the general RateLimitMiddleware (per-route sliding window).

Covers:
  - _NoopRateLimiter passthrough behaviour
  - _create_registry: sqlite / redis / no-redis / connect-failure paths
  - RateLimitMiddleware.__init__ with default get_settings() path
  - dispatch: allow / block / bypass / method-and-path filtering
  - _should_rate_limit and _rule_for rule resolution
  - _client_key: auth principal, Bearer API key, Bearer JWT, IP fallbacks
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import (
    RATELIMIT_BYPASS_HEADER,
    RateLimitMiddleware,
    _create_registry,
    _NoopRateLimiter,
)
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "testpass",
        "redis_url": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return Settings(**base)


def _make_app(registry=None, settings=None) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/runs")
    async def create_run():
        return {"ok": True}

    @app.get("/api/v1/runs")
    async def list_runs():
        return {"ok": True}

    @app.post("/api/v1/other")
    async def other():
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        settings=settings or _settings(),
        registry=registry or _registry(),
    )
    return app


def _registry(allowed: bool = True) -> MagicMock:
    reg = MagicMock()
    reg.check = AsyncMock(return_value=allowed)
    return reg


@pytest.fixture
def rl_mod():
    import modulo.api.middleware.rate_limiter as m

    return m


@pytest.fixture(autouse=True)
def _clean_redis_state(monkeypatch, rl_mod):
    """Isolate the module-level `_redis_clients` set and `redis_available` flag."""
    monkeypatch.setattr(rl_mod, "_redis_clients", set())
    monkeypatch.setattr(rl_mod, "redis_available", False)


def _mock_request(method="POST", path="/api/v1/runs", headers=None, scope=None, client=None):
    req = MagicMock()
    req.method = method
    req.url.path = path
    req.scope = scope or {}
    req.client = client
    req.headers.get = MagicMock(side_effect=lambda k, d="": (headers or {}).get(k, d))
    return req


class TestNoopRateLimiter:
    async def test_allows_every_request(self):
        limiter = _NoopRateLimiter()
        assert await limiter.check("k", max_requests=5) is True
        assert await limiter.check("k", max_requests=0) is True


class TestCreateRegistry:
    def test_sqlite_returns_noop(self, rl_mod):
        settings = _settings(modulo_db="sqlite", redis_url="redis://localhost:6379/0")
        registry = _create_registry(settings)
        assert isinstance(registry, _NoopRateLimiter)
        assert rl_mod.redis_available is False

    def test_redis_url_creates_registry(self, rl_mod):
        fake_client = MagicMock()
        with patch("redis.asyncio.Redis.from_url", return_value=fake_client) as from_url:
            registry = _create_registry(_settings())
        assert isinstance(registry, RateLimiterRegistry)
        assert rl_mod.redis_available is True
        assert fake_client in rl_mod._redis_clients
        from_url.assert_called_once()

    def test_redis_connect_failure_returns_noop(self, rl_mod):
        with patch("redis.asyncio.Redis.from_url", side_effect=RuntimeError("conn refused")):
            registry = _create_registry(_settings())
        assert isinstance(registry, _NoopRateLimiter)
        assert rl_mod.redis_available is False
        assert rl_mod._redis_clients == set()

    def test_no_redis_url_returns_noop(self, rl_mod):
        registry = _create_registry(_settings(redis_url=""))
        assert isinstance(registry, _NoopRateLimiter)
        assert rl_mod.redis_available is False

    def test_redis_connect_cancelled_error_re_raised(self, rl_mod):
        with (
            patch("redis.asyncio.Redis.from_url", side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            _create_registry(_settings())


class TestGetAuthRateLimiter:
    def test_returns_none_on_redis_connect_failure(self, rl_mod):
        with patch("redis.asyncio.Redis.from_url", side_effect=RuntimeError("conn refused")):
            limiter = rl_mod.get_auth_rate_limiter(_settings())
        assert limiter is None
        assert rl_mod._redis_clients == set()

    def test_re_raises_cancelled_error(self, rl_mod):
        with (
            patch("redis.asyncio.Redis.from_url", side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            rl_mod.get_auth_rate_limiter(_settings())


class TestRateLimitMiddlewareInit:
    def test_init_uses_get_settings_default(self, rl_mod):
        settings = _settings()
        with (
            patch.object(rl_mod, "get_settings", return_value=settings) as get_settings,
            patch.object(rl_mod, "_create_registry", return_value=MagicMock()) as create_registry,
        ):
            RateLimitMiddleware(app=FastAPI())
        get_settings.assert_called_once()
        create_registry.assert_called_once_with(settings)

    def test_init_uses_injected_registry(self, rl_mod):
        settings = _settings()
        registry = _registry()
        mw = RateLimitMiddleware(app=FastAPI(), settings=settings, registry=registry)
        assert mw._registry is registry

    def test_set_rules_updates_class_rules(self):
        RateLimitMiddleware.set_rules([("/custom", 5, 10)])
        assert RateLimitMiddleware.RULES == [("/custom", 5, 10)]
        RateLimitMiddleware.set_rules(
            [
                ("/api/v1/runs", 60, 60),
                ("/api/v1/triggers", 100, 60),
                ("/api/v1/errors/ingest", 10, 60),
                ("/mcp", 200, 60),
            ]
        )


class TestDispatch:
    def test_allows_within_limit(self):
        registry = _registry(allowed=True)
        app = _make_app(registry=registry)
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")
        assert resp.status_code == 200
        registry.check.assert_awaited_once()

    def test_blocks_when_exceeded(self):
        registry = _registry(allowed=False)
        app = _make_app(registry=registry)
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "60"

    def test_passes_rule_params_to_registry(self):
        registry = _registry(allowed=True)
        app = _make_app(registry=registry)
        with TestClient(app) as client:
            client.post("/api/v1/runs")
        args, kwargs = registry.check.await_args
        assert args[0] == "ip:testclient:/api/v1/runs"
        assert kwargs["max_requests"] == 60
        assert kwargs["window_s"] == 60

    def test_get_not_rate_limited(self):
        registry = _registry(allowed=False)
        app = _make_app(registry=registry)
        with TestClient(app) as client:
            resp = client.get("/api/v1/runs")
        assert resp.status_code == 200
        registry.check.assert_not_awaited()

    def test_non_rule_path_not_rate_limited(self):
        registry = _registry(allowed=False)
        app = _make_app(registry=registry)
        with TestClient(app) as client:
            resp = client.post("/api/v1/other")
        assert resp.status_code == 200
        registry.check.assert_not_awaited()

    def test_bypass_token_skips_limit(self):
        registry = _registry(allowed=False)
        settings = _settings(modulo_ratelimit_bypass_token="bypass-secret")
        app = _make_app(registry=registry, settings=settings)
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs", headers={RATELIMIT_BYPASS_HEADER: "bypass-secret"})
        assert resp.status_code == 200
        registry.check.assert_not_awaited()


class TestShouldRateLimit:
    def test_skip_get(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=_settings(), registry=_registry())
        assert mw._should_rate_limit(_mock_request(method="GET")) is False

    def test_skip_when_bypass_header_matches(self):
        settings = _settings(modulo_ratelimit_bypass_token="tok")
        mw = RateLimitMiddleware(app=FastAPI(), settings=settings, registry=_registry())
        req = _mock_request(headers={RATELIMIT_BYPASS_HEADER: "tok"})
        assert mw._should_rate_limit(req) is False

    def test_skip_when_bypass_token_empty(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=_settings(), registry=_registry())
        req = _mock_request(headers={RATELIMIT_BYPASS_HEADER: "tok"})
        assert mw._should_rate_limit(req) is True

    def test_true_for_matching_rule_path(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=_settings(), registry=_registry())
        assert mw._should_rate_limit(_mock_request(path="/api/v1/triggers")) is True

    def test_false_for_non_rule_path(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=_settings(), registry=_registry())
        assert mw._should_rate_limit(_mock_request(path="/api/v1/other")) is False


class TestRuleFor:
    def test_matching_prefix(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=_settings(), registry=_registry())
        assert mw._rule_for(_mock_request(path="/api/v1/runs")) == ("/api/v1/runs", 60, 60)

    def test_no_match(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=_settings(), registry=_registry())
        assert mw._rule_for(_mock_request(path="/api/v1/other")) == ("", 0, 0)


class TestClientKey:
    def test_api_key_principal(self):
        scope = {"auth_principal": {"type": "api_key", "org_id": "org1", "prefix": "mk_abcdefgh"}}
        req = _mock_request(scope=scope)
        assert RateLimitMiddleware._client_key(req) == "ak:org1:mk_abcdefgh:/api/v1/runs"

    def test_user_principal(self):
        scope = {"auth_principal": {"type": "user", "org_id": "org1", "user_id": "u1"}}
        req = _mock_request(scope=scope)
        assert RateLimitMiddleware._client_key(req) == "user:org1:u1:/api/v1/runs"

    def test_bearer_management_api_key(self):
        token = "mk_abcdefgh1234567890"
        req = _mock_request(headers={"Authorization": f"Bearer {token}"})
        assert RateLimitMiddleware._client_key(req) == "ak:none:abcdefgh:/api/v1/runs"

    def test_bearer_jwt_with_org_and_user(self):
        token = jwt.encode({"org_id": "o1", "user_id": "u1"}, "a" * 32, algorithm="HS256")
        req = _mock_request(headers={"Authorization": f"Bearer {token}"})
        assert RateLimitMiddleware._client_key(req) == "user:o1:u1:/api/v1/runs"

    def test_bearer_jwt_falls_back_to_account_id(self):
        token = jwt.encode({"org_id": "o1", "account_id": "a1"}, "a" * 32, algorithm="HS256")
        req = _mock_request(headers={"Authorization": f"Bearer {token}"})
        assert RateLimitMiddleware._client_key(req) == "user:o1:a1:/api/v1/runs"

    def test_bearer_jwt_without_identity_falls_back_to_ip(self):
        token = jwt.encode({"scope": "public"}, "a" * 32, algorithm="HS256")
        req = _mock_request(headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "203.0.113.9"})
        assert RateLimitMiddleware._client_key(req) == "ip:203.0.113.9:/api/v1/runs"

    def test_invalid_bearer_token_falls_back_to_ip(self):
        req = _mock_request(headers={"Authorization": "Bearer garbage.not.a.jwt.x", "X-Forwarded-For": "198.51.100.7"})
        assert RateLimitMiddleware._client_key(req) == "ip:198.51.100.7:/api/v1/runs"

    def test_x_forwarded_for_first_hop(self):
        req = _mock_request(headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"})
        assert RateLimitMiddleware._client_key(req) == "ip:198.51.100.7:/api/v1/runs"

    def test_client_host_fallback(self):
        req = _mock_request(client=MagicMock(host="203.0.113.42"))
        assert RateLimitMiddleware._client_key(req) == "ip:203.0.113.42:/api/v1/runs"

    def test_client_host_none_falls_back_to_unknown(self):
        client = MagicMock()
        client.host = None
        req = _mock_request(client=client)
        assert RateLimitMiddleware._client_key(req) == "ip:unknown:/api/v1/runs"

    def test_no_client_falls_back_to_unknown(self):
        req = _mock_request(client=None)
        assert RateLimitMiddleware._client_key(req) == "ip:unknown:/api/v1/runs"


class TestShutdownCancelledError:
    async def test_shutdown_re_raises_cancelled_error(self, rl_mod):
        failing = MagicMock()
        failing.aclose = AsyncMock(side_effect=asyncio.CancelledError())
        rl_mod._redis_clients.add(failing)
        with pytest.raises(asyncio.CancelledError):
            await rl_mod.shutdown_rate_limiters()
