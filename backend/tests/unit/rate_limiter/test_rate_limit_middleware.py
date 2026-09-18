"""Unit tests for the general RateLimitMiddleware (per-route sliding window).

Covers:
  - RateLimitMiddleware.__init__ with default get_settings() path
  - dispatch: allow / block / bypass / method-and-path filtering
  - _should_rate_limit and _rule_for rule resolution
  - _client_key: auth principal, Bearer API key, Bearer JWT, IP fallbacks

Registry construction / no-op fallback / shutdown behaviour is covered by
``test_registry_creation.py`` and ``test_auth_rate_limiter.py`` — see those
modules to avoid duplicating coverage here.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response
from tests.unit.rate_limiter.helpers import make_mock_request, make_settings

from modulo.api.middleware.rate_limiter import (
    RATELIMIT_BYPASS_HEADER,
    RateLimitMiddleware,
)
from modulo.core.rate_limiter import RateLimitRule


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
        settings=settings or make_settings(),
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


class TestRateLimitMiddlewareInit:
    def test_init_uses_get_settings_default(self, rl_mod):
        settings = make_settings()
        with (
            patch.object(rl_mod, "get_settings", return_value=settings) as get_settings,
            patch.object(rl_mod, "_create_registry", return_value=MagicMock()) as create_registry,
        ):
            RateLimitMiddleware(app=FastAPI())
        get_settings.assert_called_once()
        create_registry.assert_called_once_with(settings)

    def test_init_uses_injected_registry(self, rl_mod):
        settings = make_settings()
        registry = _registry()
        mw = RateLimitMiddleware(app=FastAPI(), settings=settings, registry=registry)
        assert mw._registry is registry

    def test_set_rules_updates_class_rules(self):
        RateLimitMiddleware.set_rules([RateLimitRule(path_prefix="/custom", max_requests=5, window_s=10)])
        assert [RateLimitRule(path_prefix="/custom", max_requests=5, window_s=10)] == RateLimitMiddleware.RULES
        RateLimitMiddleware.set_rules(
            [
                RateLimitRule(path_prefix="/api/v1/runs", max_requests=60, window_s=60),
                RateLimitRule(path_prefix="/api/v1/triggers", max_requests=100, window_s=60),
                RateLimitRule(path_prefix="/api/v1/errors/ingest", max_requests=10, window_s=60),
                RateLimitRule(path_prefix="/mcp", max_requests=200, window_s=60),
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
            resp = client.post("/api/v1/runs")
        assert resp.status_code == 200
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
        settings = make_settings(modulo_ratelimit_bypass_token="bypass-secret")
        app = _make_app(registry=registry, settings=settings)
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs", headers={RATELIMIT_BYPASS_HEADER: "bypass-secret"})
        assert resp.status_code == 200
        registry.check.assert_not_awaited()

    async def test_fails_open_when_registry_check_raises(self):
        """A Redis/registry failure must fail open (200), never block traffic."""
        registry = _registry(allowed=False)
        registry.check = AsyncMock(side_effect=RuntimeError("redis down"))
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=registry)
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await mw.dispatch(make_mock_request(), call_next)
        assert response.status_code == 200
        call_next.assert_awaited_once()

    async def test_propagates_cancelled_error_from_registry(self):
        """asyncio.CancelledError from check must propagate, not fail open."""
        registry = _registry(allowed=True)
        registry.check = AsyncMock(side_effect=asyncio.CancelledError())
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=registry)
        call_next = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await mw.dispatch(make_mock_request(), call_next)
        call_next.assert_not_awaited()


class TestShouldRateLimit:
    def test_skip_get(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(method="GET")) is False

    @pytest.mark.parametrize("method", ["PUT", "PATCH"])
    def test_rate_limits_put_and_patch(self, method):
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(method=method)) is True

    def test_skip_when_bypass_header_matches(self):
        settings = make_settings(modulo_ratelimit_bypass_token="tok")
        mw = RateLimitMiddleware(app=FastAPI(), settings=settings, registry=_registry())
        req = make_mock_request(headers={RATELIMIT_BYPASS_HEADER: "tok"})
        assert mw._should_rate_limit(req) is False

    def test_does_not_skip_when_bypass_token_not_configured(self):
        """A bypass header without a configured MODULO_RATELIMIT_BYPASS_TOKEN must not skip."""
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        req = make_mock_request(headers={RATELIMIT_BYPASS_HEADER: "tok"})
        assert mw._should_rate_limit(req) is True

    def test_true_for_matching_rule_path(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(path="/api/v1/triggers")) is True

    def test_false_for_non_rule_path(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(path="/api/v1/other")) is False

    def test_true_for_hitl_path_without_matching_rule(self):
        """A POST containing /hitl/ must be rate limited even when no RULES
        entry prefix-matches the path."""
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(path="/not/a/rule/hitl/gate/approve")) is True

    def test_false_for_hitl_path_on_get(self):
        """GET requests with /hitl/ in the path must not be rate limited."""
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(method="GET", path="/api/v1/runs/x/hitl/y")) is False


class TestRuleFor:
    def test_matching_prefix(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._rule_for(make_mock_request(path="/api/v1/runs")) == RateLimitRule(
            path_prefix="/api/v1/runs", max_requests=60, window_s=60
        )

    def test_no_match(self):
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._rule_for(make_mock_request(path="/api/v1/other")) == RateLimitRule(
            path_prefix="", max_requests=0, window_s=0
        )


class TestClientKey:
    @pytest.fixture
    def middleware(self) -> RateLimitMiddleware:
        return RateLimitMiddleware(
            app=FastAPI(),
            settings=make_settings(),
            registry=_registry(),
        )

    def test_api_key_principal(self, middleware):
        scope = {"auth_principal": {"type": "api_key", "org_id": "org1", "prefix": "mk_abcdefgh"}}
        req = make_mock_request(scope=scope)
        assert middleware._client_key(req) == "ak:org1:mk_abcdefgh:/api/v1/runs"

    def test_user_principal(self, middleware):
        scope = {"auth_principal": {"type": "user", "org_id": "org1", "user_id": "u1"}}
        req = make_mock_request(scope=scope)
        assert middleware._client_key(req) == "user:org1:u1:/api/v1/runs"

    def test_unknown_principal_type_falls_back_to_ip(self, middleware):
        """A truthy auth_principal with an unrecognised type must fall through to IP keying."""
        scope = {"auth_principal": {"type": "service", "org_id": "org1"}}
        req = make_mock_request(scope=scope, headers={"X-Forwarded-For": "203.0.113.9"})
        assert middleware._client_key(req) == "ip:203.0.113.9:/api/v1/runs"

    def test_bearer_management_api_key(self, middleware):
        token = "mk_abcdefgh1234567890"
        req = make_mock_request(headers={"Authorization": f"Bearer {token}"})
        assert middleware._client_key(req) == "ak:none:abcdefgh:/api/v1/runs"

    def test_bearer_jwt_with_org_and_user(self, middleware):
        token = jwt.encode({"org_id": "o1", "user_id": "u1"}, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}"})
        assert middleware._client_key(req) == "user:o1:u1:/api/v1/runs"

    def test_bearer_jwt_falls_back_to_account_id(self, middleware):
        token = jwt.encode({"org_id": "o1", "account_id": "a1"}, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}"})
        assert middleware._client_key(req) == "user:o1:a1:/api/v1/runs"

    def test_bearer_jwt_without_identity_falls_back_to_ip(self, middleware):
        token = jwt.encode({"scope": "public"}, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "203.0.113.9"})
        assert middleware._client_key(req) == "ip:203.0.113.9:/api/v1/runs"

    def test_bearer_jwt_without_org_id_falls_back_to_ip(self, middleware):
        """A JWT with only user_id must not key on a missing org_id."""
        token = jwt.encode({"user_id": "u1"}, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "203.0.113.9"})
        assert middleware._client_key(req) == "ip:203.0.113.9:/api/v1/runs"

    def test_bearer_jwt_without_user_id_falls_back_to_ip(self, middleware):
        """A JWT with only org_id must not key on a missing user_id."""
        token = jwt.encode({"org_id": "o1"}, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "203.0.113.9"})
        assert middleware._client_key(req) == "ip:203.0.113.9:/api/v1/runs"

    def test_invalid_bearer_token_falls_back_to_ip(self, middleware):
        headers = {"Authorization": "Bearer garbage.not.a.jwt.x", "X-Forwarded-For": "198.51.100.7"}
        req = make_mock_request(headers=headers)
        assert middleware._client_key(req) == "ip:198.51.100.7:/api/v1/runs"

    def test_x_forwarded_for_first_hop(self, middleware):
        req = make_mock_request(headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"})
        assert middleware._client_key(req) == "ip:198.51.100.7:/api/v1/runs"

    def test_client_host_fallback(self, middleware):
        req = make_mock_request(client=MagicMock(host="203.0.113.42"))
        assert middleware._client_key(req) == "ip:203.0.113.42:/api/v1/runs"

    def test_client_host_none_falls_back_to_unknown(self, middleware):
        client = MagicMock()
        client.host = None
        req = make_mock_request(client=client)
        assert middleware._client_key(req) == "ip:unknown:/api/v1/runs"

    def test_no_client_falls_back_to_unknown(self, middleware):
        req = make_mock_request(client=None)
        assert middleware._client_key(req) == "ip:unknown:/api/v1/runs"
