"""Unit tests for HITL review endpoint rate limiting.

PRD §7.18 specifies 20/min for HITL review endpoints. They live under
/api/v1/runs/{run_id}/hitl/{gate_id}/ where the run/gate ids are variable,
so they are matched by a dedicated HITL rule (`HITL_RULE`: 20/min) instead of
the more generous /api/v1/runs rule (60/min) that prefix-matches the path.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.core.rate_limiter import RateLimiterRegistry, RateLimitRule
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
    """Verify HITL review endpoints are rate limited under the dedicated 20/min rule."""

    def test_hitl_rule_is_20_per_min(self) -> None:
        """PRD §7.18 defines a dedicated 20/min rule for HITL review."""
        hitl_rule = RateLimitMiddleware.HITL_RULE
        assert hitl_rule.max_requests == 20
        assert hitl_rule.window_s == 60

    def test_hitl_rule_is_more_restrictive_than_runs(self) -> None:
        """HITL review paths must be capped at 20/min, not the runs 60/min."""
        run_rule = next((r for r in RateLimitMiddleware.RULES if r.path_prefix == "/api/v1/runs"), None)
        assert run_rule is not None
        assert RateLimitMiddleware.HITL_RULE.max_requests < run_rule.max_requests

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
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        mock_registry.check.assert_awaited_once()
        key = mock_registry.check.await_args[0][0]
        assert key == f"ip:testclient:{endpoint}"

    def test_hitl_key_normalizes_variable_uuid_segments(self) -> None:
        """Variable run/gate UUIDs must be normalised to fixed placeholders so
        per-segment bucket rotation never happens (FAR-1304)."""
        run_id = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
        gate_id = "7cba9876-543f-4edc-8ba1-fedcba987654"
        endpoint = f"/api/v1/runs/{run_id}/hitl/{gate_id}/claim"
        app = FastAPI()
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        key = mock_registry.check.await_args[0][0]
        assert key == "ip:testclient:/api/v1/runs/<run_id>/hitl/<gate_id>/claim"

    def test_hitl_key_does_not_leak_raw_uuids(self) -> None:
        """Raw run/gate UUIDs must never surface in a rate-limit bucket key."""
        run_id = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
        gate_id = "7cba9876-543f-4edc-8ba1-fedcba987654"
        endpoint = f"/api/v1/runs/{run_id}/hitl/{gate_id}/claim"
        app = FastAPI()
        app.add_api_route(endpoint, lambda: {"status": "ok"}, methods=["POST"], include_in_schema=False)
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app.add_middleware(
            RateLimitMiddleware,  # type: ignore[arg-type]
            settings=_make_settings(),
            registry=mock_registry,
        )

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        key = mock_registry.check.await_args[0][0]
        assert run_id not in key
        assert gate_id not in key

    @pytest.mark.parametrize("endpoint", HITL_ENDPOINTS)
    def test_hitl_check_uses_20_per_min_budget(self, endpoint: str) -> None:
        """The registry check for HITL review must use the 20/min budget."""
        mock_registry = MagicMock(spec=RateLimiterRegistry)
        mock_registry.check = AsyncMock(return_value=True)
        app = _make_app(registry=mock_registry)

        with TestClient(app) as client:
            resp = client.post(endpoint)

        assert resp.status_code == status.HTTP_200_OK
        mock_registry.check.assert_awaited_once()
        max_requests = mock_registry.check.await_args.kwargs["max_requests"]
        assert max_requests == 20

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

    def test_hitl_prd_20_per_min_is_enforced(self) -> None:
        """PRD §7.18 specifies 20/min for HITL review and it must be enforced."""
        assert RateLimitRule(path_prefix="/hitl/", max_requests=20, window_s=60) == RateLimitMiddleware.HITL_RULE

    def test_rule_for_prefers_hitl_rule_over_runs(self) -> None:
        """_rule_for must resolve HITL paths to the dedicated 20/min rule."""
        instance = RateLimitMiddleware(app=FastAPI(), settings=_make_settings())
        for endpoint in HITL_ENDPOINTS:
            request = MagicMock()
            request.url.path = endpoint
            assert instance._rule_for(request).max_requests == 20

    def test_rule_for_keeps_runs_rule_for_non_hitl(self) -> None:
        """Non-HITL runs paths must stay under the 60/min runs rule."""
        instance = RateLimitMiddleware(app=FastAPI(), settings=_make_settings())
        request = MagicMock()
        request.url.path = "/api/v1/runs/run-123/cancel"
        rule = instance._rule_for(request)
        assert rule.path_prefix == "/api/v1/runs"
        assert rule.max_requests == 60
