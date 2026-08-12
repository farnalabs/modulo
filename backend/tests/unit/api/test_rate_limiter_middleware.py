"""Unit tests for RateLimitMiddleware — constructor override injection."""

import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware import rate_limiter as rate_limiter_module
from modulo.api.middleware.rate_limiter import RateLimitMiddleware, _matches_bypass_token
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_ratelimit_bypass_token="test-bypass",
    )


def _make_app(registry: RateLimiterRegistry | None = None) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/v1/runs")
    async def create_run():
        return {"id": "run-1"}

    @app.post("/api/v1/triggers")
    async def create_trigger():
        return {"id": "trigger-1"}

    app.add_middleware(
        RateLimitMiddleware,
        settings=_make_settings(),
        registry=registry,
    )
    return app


class TestConstructorOverrides:
    def test_accepts_explicit_settings(self):
        """Middleware should accept settings via constructor, not call get_settings()."""
        app = _make_app()
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_accepts_explicit_registry(self):
        """Middleware should use the passed registry instead of creating one."""
        mock_redis = MagicMock()
        registry = RateLimiterRegistry(redis_client=mock_redis)
        app = _make_app(registry=registry)
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_bypass_token_injected_via_settings(self):
        """Bypass token should come from the injected settings object."""
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/runs",
                headers={"MODULO_RATELIMIT_BYPASS_TOKEN": "test-bypass"},
            )
        assert resp.status_code == 200

    def test_rate_limit_exceeded_triggers_429(self):
        """When registry denies, middleware returns 429."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")
        assert resp.status_code == 429
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:rate_limited"
        assert body["status"] == 429

    def test_get_requests_not_rate_limited(self):
        """Only POST/PUT/PATCH are rate limited."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)
        with TestClient(app) as client:
            resp = client.get("/api/v1/runs")
        # Route exists for POST but not GET — the response should be a normal
        # 405 (Method Not Allowed), NOT a 429 from the rate limiter.
        assert resp.status_code != 429

    def test_bypass_token_skips_rate_limit(self):
        """Valid bypass token in header should skip rate limiting."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/runs",
                headers={"MODULO_RATELIMIT_BYPASS_TOKEN": "test-bypass"},
            )
        assert resp.status_code == 200

    def test_wrong_bypass_token_does_not_skip(self):
        """Invalid bypass token should not skip rate limiting."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/runs",
                headers={"MODULO_RATELIMIT_BYPASS_TOKEN": "wrong-token"},
            )
        assert resp.status_code == 429

    def test_mcp_path_rate_limited(self):
        """MCP paths should also be rate limited."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)
        with TestClient(app) as client:
            resp = client.post("/mcp/messages")
        assert resp.status_code == 429

    def test_registry_error_fails_open(self):
        """When registry.check raises (e.g. Redis outage), request passes through."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(side_effect=RuntimeError("redis unavailable"))
        app = _make_app(registry=mock_registry)
        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")
        assert resp.status_code == 200
        assert resp.json() == {"id": "run-1"}


class TestBypassTokenComparison:
    """Bypass token is a shared secret — comparison must be constant-time."""

    def test_uses_hmac_compare_digest(self, monkeypatch):
        """The helper must delegate to hmac.compare_digest (not ==)."""
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            rate_limiter_module.hmac,
            "compare_digest",
            lambda a, b: calls.append((a, b)) or a == b,
        )
        assert _matches_bypass_token("secret", "secret") is True
        assert _matches_bypass_token("secret", "other") is False
        assert calls == [("secret", "secret"), ("secret", "other")]

    def test_empty_token_or_secret_is_never_a_match(self):
        assert _matches_bypass_token("", "secret") is False
        assert _matches_bypass_token("secret", "") is False
        assert _matches_bypass_token("", "") is False

    def test_mismatched_lengths_are_handled(self):
        """compare_digest on different-length inputs returns False, not an error."""
        assert _matches_bypass_token("a", "b" * 100) is False

    def test_compare_digest_rejects_non_str_mismatch(self):
        """compare_digest requires both args to be the same type (both str here)."""
        with pytest.raises(TypeError):
            hmac.compare_digest("a", b"a")

    def test_middleware_delegates_to_constant_time_helper(self, monkeypatch):
        """Valid bypass header must be routed through _matches_bypass_token."""
        seen: list[tuple[str, str]] = []

        def fake_match(token: str, bypass: str) -> bool:
            seen.append((token, bypass))
            return hmac.compare_digest(token, bypass)

        monkeypatch.setattr(rate_limiter_module, "_matches_bypass_token", fake_match)
        app = _make_app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/runs",
                headers={"MODULO_RATELIMIT_BYPASS_TOKEN": "test-bypass"},
            )
        assert resp.status_code == 200
        assert seen == [("test-bypass", "test-bypass")]
