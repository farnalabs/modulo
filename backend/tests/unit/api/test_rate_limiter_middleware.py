"""Unit tests for RateLimitMiddleware — constructor override injection."""

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
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
        registry = RateLimiterRegistry(redis_client=None)
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
        assert body["error_code"] == "rate_limit_exceeded"

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
