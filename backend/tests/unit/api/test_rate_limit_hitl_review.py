"""Unit tests for HITL review endpoint rate limiting.

PRD §7.18 specifies 20/min for HITL review endpoints but they are currently
covered by the more generous /api/v1/runs rule (60/min) since the HITL paths
start with /api/v1/runs/{run_id}/hitl/{gate_id}/.

These tests verify the current behaviour: HITL review endpoints ARE rate
limited, and the rule applied is the /api/v1/runs rule. If a dedicated
20/min rule is added in the future, these tests should be updated to match.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings

HITL_ENDPOINTS = [
    "/api/v1/runs/run-123/hitl/gate-abc/approve",
    "/api/v1/runs/run-123/hitl/gate-abc/reject",
    "/api/v1/runs/run-123/hitl/gate-abc/claim",
    "/api/v1/runs/run-123/hitl/gate-abc/deliver-manual",
    "/api/v1/runs/run-123/hitl/gate-abc/approve-with-modification",
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",  # nosec
        modulo_ratelimit_bypass_token="test-bypass",
    )


def _make_app(registry: RateLimiterRegistry | None = None) -> FastAPI:
    app = FastAPI()

    for endpoint in HITL_ENDPOINTS:
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)

    app.add_middleware(
        RateLimitMiddleware,  # type: ignore[arg-type]
        settings=_make_settings(),
        registry=registry,
    )
    return app


class TestHitlReviewRateLimit:
    """Verify HITL review endpoints are rate limited under the /api/v1/runs rule."""

    def test_hitl_approve_matches_runs_rule(self) -> None:
        """The /api/v1/runs prefix catches HITL review POSTs."""
        rules = RateLimitMiddleware.RULES
        run_rule = next((r for r in rules if r[0] == "/api/v1/runs"), None)
        assert run_rule is not None
        assert run_rule[1] == 60
        assert run_rule[2] == 60

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_endpoint_is_rate_limited(self, endpoint: str) -> None:
        """Each HITL endpoint should be rate limited by the middleware."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_within_limit_succeeds(self, endpoint: str) -> None:
        """Within-limit requests to HITL endpoints should succeed."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        mock_registry.check.assert_awaited_once()

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_429_has_retry_after_header(self, endpoint: str) -> None:
        """429 responses must include a Retry-After header."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert "Retry-After" in resp.headers

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_key_includes_runs_prefix(self, endpoint: str) -> None:
        """The rate limit key should be derived from the /api/v1/runs path."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            client.post(endpoint)

        mock_registry.check.assert_awaited_once()
        key = mock_registry.check.await_args[0][0]
        assert "/api/v1/runs" in key

    def test_hitl_get_not_rate_limited(self) -> None:
        """GET requests to HITL endpoints should not be rate limited."""
        app = FastAPI()
        app.add_api_route(
            "/api/v1/runs/run-123/hitl/gate-abc/pending",
            lambda: {"gates": []},
            methods=["GET"],
            include_in_schema=False,
        )
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=False)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.get("/api/v1/runs/run-123/hitl/gate-abc/pending")

        assert resp.status_code != status.HTTP_429_TOO_MANY_REQUESTS

    def test_hitl_prd_documents_20_per_min_intent(self) -> None:
        """PRD §7.18 specifies 20/min for HITL review — document the gap."""
        rules = RateLimitMiddleware.RULES
        run_rule = next((r for r in rules if r[0] == "/api/v1/runs"), None)
        assert run_rule is not None
        assert run_rule[1] == 60, (
            "PRD §7.18 intends 20/min for HITL review. Currently capped at 60/min "
            "by the /api/v1/runs rule. When a dedicated HITL rule is added, update "
            "this assertion and the RULES in rate_limiter.py."
        )
