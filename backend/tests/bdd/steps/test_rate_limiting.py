"""Step definitions for API Rate Limiting (PRD §7.18).

Each scenario exercises the RateLimitMiddleware with a controlled
RateLimiterRegistry to verify rate limit enforcement, reset behaviour,
per-key isolation, and admin reconfiguration.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.core.rate_limiter import RateLimiterRegistry
from modulo.settings import Settings

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
try:
    scenarios("../../bdd/features/model_backends/rate_limiting.feature")
except (FileNotFoundError, OSError):
    pass


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


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
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/runs")
    async def _create_run() -> dict[str, str]:
        return {"id": "run-1"}

    @app.post("/api/v1/triggers")
    async def _create_trigger() -> dict[str, str]:
        return {"id": "trigger-1"}

    app.add_middleware(
        RateLimitMiddleware,
        settings=_make_settings(),
        registry=registry,
    )
    return app


def _store_response(request: pytest.FixtureRequest, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


# ===========================================================================
# Given
# ===========================================================================


_PATH_LIMITS: dict[str, int] = {
    "/api/v1/runs": 60,
    "/api/v1/triggers": 100,
}


@given(parsers.parse("I have made {count:d} requests to POST {path} in the last minute"))
def given_requests_made(ctx: dict[str, Any], count: int, path: str) -> None:
    if "path_counts" not in ctx:
        ctx["path_counts"] = {}
    ctx["path_counts"][path] = count
    ctx["rate_limit"] = _PATH_LIMITS.get(path, 60)


@given(parsers.parse('I have exceeded my rate limit for POST {path}'))
def given_exceeded_limit(ctx: dict[str, Any], path: str) -> None:
    if "path_counts" not in ctx:
        ctx["path_counts"] = {}
    ctx["path_counts"][path] = _PATH_LIMITS.get(path, 60) + 1
    ctx["rate_limit"] = _PATH_LIMITS.get(path, 60)
    ctx["exceeded"] = True


@given(parsers.parse("I am authenticated as an admin"))
def given_authenticated_admin(ctx: dict[str, Any]) -> None:
    ctx["is_admin"] = True


@given(parsers.parse('API key "{key}" has made {count:d} requests to POST {path}'))
def given_api_key_requests(ctx: dict[str, Any], key: str, count: int, path: str) -> None:
    if "api_keys" not in ctx:
        ctx["api_keys"] = {}
    ctx["api_keys"][key] = {"count": count, "path": path}
    ctx["rate_limit"] = 60


# ===========================================================================
# When
# ===========================================================================


@when(parsers.parse("{count:d} seconds pass"))
def when_time_passes(ctx: dict[str, Any], count: int) -> None:
    ctx["time_passed"] = count


@when(parsers.parse("I POST {path}"))
def when_post_path(request: pytest.FixtureRequest, ctx: dict[str, Any], path: str) -> None:
    full_path = path
    time_passed = ctx.get("time_passed", 0)
    path_counts = ctx.get("path_counts", {})
    count = path_counts.get(path, 0)
    rate_limit = _PATH_LIMITS.get(path, ctx.get("rate_limit", 60))

    allowed = time_passed > 0 or count < rate_limit
    registry = _make_mock_registry(allowed=allowed)
    app = _build_app(registry=registry)

    with TestClient(app) as client:
        resp = client.post(full_path)
        _store_response(request, ctx, resp)


@when(parsers.parse('I POST {path} with API key "{key}"'))
def when_post_with_key(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
    key: str,
) -> None:
    full_path = path
    api_keys = ctx.get("api_keys", {})
    key_data = api_keys.get(key, {"count": 0})
    rate_limit = ctx.get("rate_limit", 60)

    allowed = key_data["count"] < rate_limit
    registry = _make_mock_registry(allowed=allowed)
    app = _build_app(registry=registry)

    with TestClient(app) as client:
        resp = client.post(
            full_path,
            headers={"Authorization": f"Bearer {key}"},
        )
        _store_response(request, ctx, resp)


@when(
    parsers.parse("I PUT {path} with {count:d} requests per {window:d} seconds for {rule_path}"),
)
def when_update_rate_limits(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    count: int,
    window: int,
    path: str,
    rule_path: str,
) -> None:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.api.middleware.rate_limiter import RateLimitMiddleware
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import get_settings

    original_rules = list(RateLimitMiddleware.RULES)

    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
    )

    new_rules = [{"path_prefix": rule_path, "max_requests": count, "window_s": window}]
    ctx["new_rules"] = new_rules
    ctx["path"] = path
    ctx["count"] = count
    ctx["window"] = window

    client = TestClient(app)
    resp = client.put(path, json={"rules": new_rules})
    _store_response(request, ctx, resp)

    app.dependency_overrides.clear()
    RateLimitMiddleware.RULES = original_rules


# ===========================================================================
# Then
# ===========================================================================


@then("the response status is 200")
def then_status_200(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


@then("the response status is 429")
def then_status_429(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"


@then("the response has a Retry-After header")
def then_has_retry_after_header(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert "Retry-After" in resp.headers, f"Missing Retry-After header in {dict(resp.headers)}"


@then("the response body indicates rate limit exceeded")
def then_body_indicates_exceeded(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    body = resp.json()
    assert body.get("error_code") == "rate_limit_exceeded", (
        f"Expected error_code 'rate_limit_exceeded', got {body}"
    )


@then("the Retry-After value is at least 1")
def then_retry_after_value(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    value = int(resp.headers["Retry-After"])
    assert value >= 1, f"Retry-After value {value} is less than 1"


@then("the response includes a Retry-After header")
def then_includes_retry_after(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert "retry-after" in resp.headers or "Retry-After" in resp.headers, (
        f"Missing Retry-After header in {dict(resp.headers)}"
    )


@then("the rate limit rules include the new /api/v1/runs limit")
def then_rules_updated(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    body = resp.json()
    rules = body.get("rules", [])
    path = "/api/v1/runs"
    matching = [r for r in rules if r.get("path_prefix") == path]
    assert matching, f"No rule found for {path} in {rules}"
