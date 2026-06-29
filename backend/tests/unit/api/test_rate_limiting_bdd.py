"""Unit tests exercising each BDD scenario from rate_limiting.feature.

Covers the seven scenarios as direct pytest tests:
  1. Request within limit passes
  2. Rate limit exceeded returns 429
  3. Rate limit resets after window
  4. Different endpoints have different limits
  5. Per-API-key rate limiting
  6. Retry-After header on 429
  7. Admin can configure limits

These complement the existing unit tests in ``tests/unit/rate_limiter/``
and ``tests/unit/api/test_rate_limiter_middleware.py`` by exercising the
full middleware → registry integration path (still with a mock registry).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.status import HTTP_200_OK, HTTP_429_TOO_MANY_REQUESTS

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings

# ---------------------------------------------------------------------------
# Save/restore RULES to prevent cross-test pollution
# ---------------------------------------------------------------------------

_ORIGINAL_RULES: list[tuple[str, int, int]] | None = None


@pytest.fixture(autouse=True)
def _save_restore_rules() -> None:
    global _ORIGINAL_RULES
    if _ORIGINAL_RULES is None:
        _ORIGINAL_RULES = list(RateLimitMiddleware.RULES)
    yield
    RateLimitMiddleware.RULES = list(_ORIGINAL_RULES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_mock_registry(allowed: bool = True) -> MagicMock:
    registry = MagicMock(spec=RateLimiterRegistry)
    registry.check = AsyncMock(return_value=allowed)
    return registry


def _build_app(
    registry: RateLimiterRegistry | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    app = FastAPI()
    resolved = settings or _make_settings()

    @app.post("/api/v1/runs")
    async def _create_run() -> dict[str, str]:
        return {"id": "run-1"}

    @app.post("/api/v1/triggers")
    async def _create_trigger() -> dict[str, str]:
        return {"id": "trigger-1"}

    @app.get("/api/v1/runs")
    async def _list_runs() -> list:
        return []

    app.add_middleware(
        RateLimitMiddleware,
        settings=resolved,
        registry=registry,
    )
    return app


# ===========================================================================
# Scenario 1: Request within rate limit succeeds
# ===========================================================================


class TestRequestWithinLimit:
    def test_post_within_limit_returns_200(self) -> None:
        registry = _make_mock_registry(allowed=True)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")

        assert resp.status_code == HTTP_200_OK
        assert resp.json() == {"id": "run-1"}
        registry.check.assert_awaited_once()

    def test_multiple_requests_within_limit_all_succeed(self) -> None:
        registry = _make_mock_registry(allowed=True)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            for _ in range(5):
                resp = client.post("/api/v1/runs")
                assert resp.status_code == HTTP_200_OK

        assert registry.check.await_count == 5


# ===========================================================================
# Scenario 2: Rate limit exceeded returns 429
# ===========================================================================


class TestRateLimitExceeded:
    def test_exceeded_limit_returns_429(self) -> None:
        registry = _make_mock_registry(allowed=False)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")

        assert resp.status_code == HTTP_429_TOO_MANY_REQUESTS

    def test_429_has_retry_after_header(self) -> None:
        registry = _make_mock_registry(allowed=False)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")

        assert "Retry-After" in resp.headers

    def test_429_body_indicates_rate_limit_exceeded(self) -> None:
        registry = _make_mock_registry(allowed=False)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")

        body = resp.json()
        assert body["error_code"] == "rate_limit_exceeded"

    def test_retry_after_value_is_positive(self) -> None:
        registry = _make_mock_registry(allowed=False)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")

        retry_after = int(resp.headers["Retry-After"])
        assert retry_after >= 1


# ===========================================================================
# Scenario 3: Rate limit resets after window expires
# ===========================================================================


class TestRateLimitReset:
    def test_requests_succeed_after_window_passes(self) -> None:
        registry = MagicMock(spec=RateLimiterRegistry)

        call_count = 0

        async def _check(*args: object, **kwargs: object) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count > 1

        registry.check = AsyncMock(side_effect=_check)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            denied = client.post("/api/v1/runs")
            allowed = client.post("/api/v1/runs")

        assert denied.status_code == HTTP_429_TOO_MANY_REQUESTS
        assert allowed.status_code == HTTP_200_OK

    def test_registry_receives_correct_arguments(self) -> None:
        registry = MagicMock(spec=RateLimiterRegistry)
        registry.check = AsyncMock(return_value=True)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            client.post("/api/v1/runs")

        registry.check.assert_awaited_once()
        _, kwargs = registry.check.await_args
        assert "max_requests" in kwargs
        assert "window_s" in kwargs


# ===========================================================================
# Scenario 4: Different endpoints have different limits
# ===========================================================================


class TestDifferentEndpointLimits:
    def test_runs_and_triggers_have_separate_counters(self) -> None:
        registry = MagicMock(spec=RateLimiterRegistry)
        registry.check = AsyncMock(return_value=True)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            client.post("/api/v1/runs")
            client.post("/api/v1/triggers")

        assert registry.check.await_count == 2
        run_call = registry.check.await_args_list[0]
        trigger_call = registry.check.await_args_list[1]

        run_key = run_call[0][0]
        trigger_key = trigger_call[0][0]
        assert run_key != trigger_key, "Runs and triggers should use different rate limit keys"

    def test_triggers_not_affected_by_runs_limit(self) -> None:
        registry = MagicMock(spec=RateLimiterRegistry)

        async def side_effect(key: str, **kwargs: object) -> bool:
            if "runs" in key:
                return False
            return True

        registry.check = AsyncMock(side_effect=side_effect)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            runs_resp = client.post("/api/v1/runs")
            triggers_resp = client.post("/api/v1/triggers")

        assert runs_resp.status_code == HTTP_429_TOO_MANY_REQUESTS
        assert triggers_resp.status_code == HTTP_200_OK


# ===========================================================================
# Scenario 5: Per-API-key rate limiting isolates counters
# ===========================================================================


class TestPerApiKeyRateLimiting:
    def test_different_keys_have_independent_counters(self) -> None:
        registry = MagicMock(spec=RateLimiterRegistry)

        tracked: dict[str, int] = {"key_one_": 60, "key_two_": 0}

        async def check(key: str, max_requests: int = 60, window_s: int = 60) -> bool:
            for prefix, count in tracked.items():
                if prefix in key and count >= max_requests:
                    return False
            return True

        registry.check = AsyncMock(side_effect=check)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp_over = client.post(
                "/api/v1/runs",
                headers={"Authorization": "Bearer mk_key_one_prefix_abc123"},
            )
            resp_ok = client.post(
                "/api/v1/runs",
                headers={"Authorization": "Bearer mk_key_two_prefix_def456"},
            )

        assert resp_over.status_code == HTTP_429_TOO_MANY_REQUESTS
        assert resp_ok.status_code == HTTP_200_OK

    def test_key_derivation_uses_api_key_prefix(self) -> None:
        registry = MagicMock(spec=RateLimiterRegistry)
        registry.check = AsyncMock(return_value=True)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            client.post("/api/v1/runs", headers={"Authorization": "Bearer mk_aabbccdd_xyz123"})

        registry.check.assert_awaited_once()
        key = registry.check.await_args[0][0]
        assert "ak:" in key, f"Expected API-key-derived key, got {key}"


# ===========================================================================
# Scenario 6: Retry-After header on 429
# ===========================================================================


class TestRetryAfterHeader:
    def test_retry_after_header_present_on_429(self) -> None:
        registry = _make_mock_registry(allowed=False)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")

        assert "Retry-After" in resp.headers

    def test_retry_after_not_present_on_200(self) -> None:
        registry = _make_mock_registry(allowed=True)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            resp = client.post("/api/v1/runs")

        assert "retry-after" not in resp.headers

    def test_only_post_put_patch_are_rate_limited(self) -> None:
        registry = _make_mock_registry(allowed=False)
        app = _build_app(registry=registry)

        with TestClient(app) as client:
            get_resp = client.get("/api/v1/runs")

        assert get_resp.status_code != 429

    def test_bypass_token_skips_rate_limiting(self) -> None:
        registry = _make_mock_registry(allowed=False)
        settings = _make_settings()
        settings.modulo_ratelimit_bypass_token = "bypass-123"
        app = _build_app(registry=registry, settings=settings)

        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/runs",
                headers={"MODULO_RATELIMIT_BYPASS_TOKEN": "bypass-123"},
            )

        assert resp.status_code == 200


# ===========================================================================
# Scenario 7: Admin can configure limits
# ===========================================================================


class TestAdminRateLimits:
    def test_get_rate_limits_returns_rules(self) -> None:
        app = FastAPI()
        from modulo.api.routes.admin_rate_limits import router
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id="00000000-0000-0000-0000-000000000001",
            user_id="00000000-0000-0000-0000-000000000002",
            org_role="admin",
        )

        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/rate-limits")

        assert resp.status_code == HTTP_200_OK
        body = resp.json()
        assert "rules" in body
        assert "mode" in body
        assert len(body["rules"]) > 0

    def test_put_updates_rules_dynamically(self) -> None:
        app = FastAPI()
        from modulo.api.routes.admin_rate_limits import router
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id="00000000-0000-0000-0000-000000000001",
            user_id="00000000-0000-0000-0000-000000000002",
            org_role="admin",
        )

        new_rules = {"rules": [{"path_prefix": "/api/v1/runs", "max_requests": 10, "window_s": 30}]}

        with TestClient(app) as client:
            resp = client.put("/api/v1/admin/rate-limits", json=new_rules)

        assert resp.status_code == HTTP_200_OK
        body = resp.json()
        rules = body.get("rules", [])
        run_rule = next((r for r in rules if r["path_prefix"] == "/api/v1/runs"), None)
        assert run_rule is not None
        assert run_rule["max_requests"] == 10
        assert run_rule["window_s"] == 30

    def test_put_requires_admin_role(self) -> None:
        app = FastAPI()
        from modulo.api.routes.admin_rate_limits import router
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="viewer",
            organisation_id="00000000-0000-0000-0000-000000000001",
            user_id="00000000-0000-0000-0000-000000000002",
            org_role="viewer",
        )

        new_rules = {"rules": [{"path_prefix": "/api/v1/runs", "max_requests": 10, "window_s": 30}]}

        with TestClient(app) as client:
            resp = client.put("/api/v1/admin/rate-limits", json=new_rules)

        assert resp.status_code == 403

    def test_put_rejects_empty_rules(self) -> None:
        app = FastAPI()
        from modulo.api.routes.admin_rate_limits import router
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id="00000000-0000-0000-0000-000000000001",
            user_id="00000000-0000-0000-0000-000000000002",
            org_role="admin",
        )

        with TestClient(app) as client:
            resp = client.put("/api/v1/admin/rate-limits", json={"rules": []})

        assert resp.status_code == 400

    def test_put_rejects_negative_max_requests(self) -> None:
        app = FastAPI()
        from modulo.api.routes.admin_rate_limits import router
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id="00000000-0000-0000-0000-000000000001",
            user_id="00000000-0000-0000-0000-000000000002",
            org_role="admin",
        )

        with TestClient(app) as client:
            resp = client.put(
                "/api/v1/admin/rate-limits",
                json={"rules": [{"path_prefix": "/api/v1/runs", "max_requests": -1, "window_s": 30}]},
            )

        assert resp.status_code == 422


# ===========================================================================
# Middleware rule defaults match PRD §7.18
# ===========================================================================


class TestDefaultRulesMatchPrd:
    def test_runs_rule_is_60_per_minute(self) -> None:
        rules = RateLimitMiddleware.RULES
        run_rule = next((r for r in rules if r[0] == "/api/v1/runs"), None)
        assert run_rule is not None
        assert run_rule[1] == 60
        assert run_rule[2] == 60

    def test_triggers_rule_is_100_per_minute(self) -> None:
        rules = RateLimitMiddleware.RULES
        trigger_rule = next((r for r in rules if r[0] == "/api/v1/triggers"), None)
        assert trigger_rule is not None
        assert trigger_rule[1] == 100
        assert trigger_rule[2] == 60

    def test_mcp_rule_is_200_per_minute(self) -> None:
        rules = RateLimitMiddleware.RULES
        mcp_rule = next((r for r in rules if r[0] == "/mcp"), None)
        assert mcp_rule is not None
        assert mcp_rule[1] == 200
        assert mcp_rule[2] == 60
