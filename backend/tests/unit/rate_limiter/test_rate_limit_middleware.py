"""Unit tests for the general RateLimitMiddleware (per-route sliding window).

Covers:
  - RateLimitMiddleware.__init__ with default get_settings() path
  - dispatch: allow / block / bypass / method-and-path filtering
  - _should_rate_limit rule gating (identity keying is covered by
    ``test_middleware_internals.py::TestClientKey``)

Registry construction / no-op fallback / shutdown behaviour is covered by
``test_registry_creation.py`` and ``test_auth_rate_limiter.py`` — see those
modules to avoid duplicating coverage here.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response
from tests.unit.rate_limiter.helpers import ANONYMOUS_GET_RULES, make_mock_request, make_settings

from modulo.api.middleware.rate_limiter import (
    RATELIMIT_BYPASS_HEADER,
    RateLimitMiddleware,
)


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
        args, _kwargs = registry.check.await_args
        assert args[0] == "ip:testclient:/api/v1/runs"
        assert args[1] == 60
        assert args[2] == 60

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

    def test_get_sso_providers_uses_its_60_per_minute_get_rule(self):
        """FAR-1978: the anonymous SSO discovery GET is throttled 60/min/IP.

        Drives the real middleware dispatch, so it proves the rule APPLIES —
        not merely that it was declared.
        """
        registry = _registry(allowed=False)
        app = _make_app(registry=registry)
        with TestClient(app) as client:
            resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "60"
        args, _kwargs = registry.check.await_args
        assert args[0] == "ip:testclient:/api/v1/auth/sso/providers"
        # Called positionally: the noop implementation must underscore-prefix its
        # unused params (ruff ARG) and the union type then requires a positional call.
        assert args[1] == 60  # max_requests
        assert args[2] == 60  # window_s

    def test_bypass_token_skips_limit(self):
        registry = _registry(allowed=False)
        settings = make_settings(modulo_ratelimit_bypass_token="bypass-secret")
        app = _make_app(registry=registry, settings=settings)
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs", headers={RATELIMIT_BYPASS_HEADER: "bypass-secret"})
        assert resp.status_code == 200
        registry.check.assert_not_awaited()

    def test_non_ascii_bypass_header_rate_limited_without_raising(self):
        """A non-ASCII bypass header must be a clean 429, not a 500.

        Starlette decodes header bytes as latin-1, so a high byte reaches the
        comparator as a non-ASCII str — and ``hmac.compare_digest`` raises
        TypeError on those instead of returning False. The raw byte is sent so
        the middleware sees exactly what a real client would produce.
        """
        registry = _registry(allowed=False)
        settings = make_settings(modulo_ratelimit_bypass_token="bypass-secret")
        app = _make_app(registry=registry, settings=settings)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/runs",
                headers=[(RATELIMIT_BYPASS_HEADER.encode(), b"bypass-s\xe9cret")],
            )
        assert resp.status_code == 429
        registry.check.assert_awaited_once()

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

    def test_skips_delete_and_options(self):
        """Only POST/PUT/PATCH (and allow-listed GET reads) are gated — other methods pass through."""
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(method="DELETE")) is False
        assert mw._should_rate_limit(make_mock_request(method="OPTIONS")) is False

    @pytest.mark.parametrize(
        "path",
        [path for path, _prefix in ANONYMOUS_GET_RULES.values()],
        ids=list(ANONYMOUS_GET_RULES),
    )
    def test_rate_limits_anonymous_get_endpoints(self, path):
        """FAR-856: pre-auth anonymous GET endpoints are gated by GET_RULES (enumeration defence)."""
        mw = RateLimitMiddleware(app=FastAPI(), settings=make_settings(), registry=_registry())
        assert mw._should_rate_limit(make_mock_request(method="GET", path=path)) is True

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


# NOTE: _rule_for prefix/no-match branches are covered by
# test_middleware_internals.py::TestRuleFor (plus HITL precedence) — not repeated here.
