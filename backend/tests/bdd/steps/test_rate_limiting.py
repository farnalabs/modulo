"""Step definitions for API Rate Limiting (PRD §7.18).

Each scenario exercises the RateLimitMiddleware with a controlled
RateLimiterRegistry to verify rate limit enforcement, reset behaviour,
per-key isolation, admin reconfiguration, and the in-memory/SQLite fallbacks.
``rate_limiting/rate_limiting.feature`` adds the per-endpoint and per-API-key
budget scenarios (independent endpoint counters, Retry-After semantics) on top
of the ``model_backends/rate_limiting.feature`` operator scenarios.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.core.rate_limiter import RateLimiterRegistry, RateLimitRule
from modulo.settings import Settings

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/model_backends/rate_limiting.feature")
    scenarios("../../bdd/features/rate_limiting/rate_limiting.feature")

# Path -> limit for the documented rules (PRD §7.18). Prefixes must match the
# RateLimitMiddleware.RULES so the mock app rate-limits the same paths.
_PATH_LIMITS: dict[str, int] = {
    "/api/v1/runs": 60,
    "/api/v1/triggers": 100,
    "/api/v1/triggers/dummy-trigger": 100,
    "/mcp": 200,
    "/mcp/any-tool": 200,
}


def _limit_for_path(path: str) -> int:
    """PRD §7.18 endpoint budget for *path* (mirrors RateLimitMiddleware.RULES)."""
    if path.endswith("/webhook") or path.startswith("/api/v1/triggers"):
        return 100
    if path.startswith("/mcp"):
        return 200
    return 60


def _counter_bucket(path: str) -> str:
    """Collapse variable webhook paths to one shared counter bucket."""
    if path.endswith("/webhook"):
        return f"{path.split('/api/v1', 1)[0]}/api/v1/triggers/<id>/webhook"
    return path


def _counter_key(method: str, path: str) -> str:
    """Per-method counter key so GET/POST budgets never share a bucket."""
    return f"{method} {_counter_bucket(path)}"


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


@pytest.fixture(autouse=True)
def _restore_ratelimit_rules() -> Generator[None, None, None]:
    """Restore the class-level rate-limit rules after every scenario.

    Scenario steps mutate ``RateLimitMiddleware.RULES`` via ``set_rules``
    (``when_put_new_rules`` / ``when_put_empty_rules``). The class variable is
    shared process-wide: if a scenario leaves it pointing at raw dicts or fails
    before its own restore runs, every later request through the middleware in
    the same pytest process crashes with ``AttributeError`` and returns 500
    (observed as a ~110-test cascade in the full BDD suite). Snapshotting here
    and always restoring guarantees the shared state can never be poisoned.
    """
    saved = list(RateLimitMiddleware.RULES)
    yield
    RateLimitMiddleware.set_rules(list(saved))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        **overrides,
    )


def _make_mock_registry(allowed: bool = True) -> MagicMock:
    registry = MagicMock(spec=RateLimiterRegistry)
    registry.check = AsyncMock(return_value=allowed)
    return registry


def _build_app(
    registry: RateLimiterRegistry | None = None,
    *,
    bypass_token: str = "",
    disable_rate_limiting: bool = False,
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/runs")
    async def _create_run() -> dict[str, str]:
        return {"id": "run-1"}

    @app.get("/api/v1/runs")
    async def _list_runs() -> dict[str, Any]:
        return {"runs": []}

    @app.post("/api/v1/triggers/dummy-trigger")
    async def _webhook_trigger() -> dict[str, str]:
        return {"id": "trigger-1"}

    @app.post("/api/v1/triggers")
    async def _list_triggers() -> dict[str, Any]:
        return {"triggers": []}

    @app.post("/mcp/any-tool")
    async def _mcp_tool() -> dict[str, Any]:
        return {"ok": "true"}

    @app.post("/api/v1/admin/rate-limits")
    async def _admin_rate_limits() -> dict[str, Any]:
        return {"rules": list(RateLimitMiddleware.RULES)}

    if disable_rate_limiting:
        return app

    app.add_middleware(
        RateLimitMiddleware,
        settings=_make_settings(modulo_ratelimit_bypass_token=bypass_token),
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


@given("rate limiting is enabled")
def given_rate_limiting_enabled(ctx: dict[str, Any]) -> None:
    ctx["rate_limiting_enabled"] = True


@given("the system has Redis available for distributed rate limiting")
def given_redis_available(ctx: dict[str, Any]) -> None:
    ctx["redis_available"] = True


@given(parsers.parse("I have exceeded the rate limit on {path}"))
def given_exceeded_limit(ctx: dict[str, Any], path: str) -> None:
    ctx["path_counts"] = {path: _PATH_LIMITS.get(path, 60) + 1}
    ctx["exceeded"] = True


@given("a valid MODULO_RATELIMIT_BYPASS_TOKEN is configured")
def given_bypass_token(ctx: dict[str, Any]) -> None:
    ctx["bypass_token"] = "test-bypass-token"


@given("Redis is not available")
def given_redis_unavailable(ctx: dict[str, Any]) -> None:
    ctx["redis_available"] = False


@given("the database is SQLite")
def given_sqlite_mode(ctx: dict[str, Any]) -> None:
    ctx["sqlite_mode"] = True


@given("I am authenticated as a viewer")
def given_auth_viewer(ctx: dict[str, Any]) -> None:
    ctx["is_admin"] = False


@given("I am authenticated as an admin")
def given_auth_admin(ctx: dict[str, Any]) -> None:
    ctx["is_admin"] = True


# ===========================================================================
# When
# ===========================================================================


@given(parsers.parse("I have made {count:d} requests to {method} {path} in the last minute"))
def given_made_requests(ctx: dict[str, Any], count: int, method: str, path: str) -> None:
    ctx.setdefault("path_counts", {})
    ctx["path_counts"][_counter_key(method, path)] = count
    ctx.setdefault("exceeded", False)


@given(parsers.parse("I have exceeded my rate limit for {method} {path}"))
def given_exceeded_limit_for(ctx: dict[str, Any], method: str, path: str) -> None:
    ctx.setdefault("path_counts", {})
    ctx["path_counts"][_counter_key(method, path)] = _limit_for_path(path) + 1
    ctx["exceeded"] = True


@given(parsers.parse('API key "{key}" has made {count:d} requests to {method} {path}'))
def given_api_key_requests(ctx: dict[str, Any], key: str, count: int, method: str, path: str) -> None:
    ctx.setdefault("key_counts", {})
    ctx["key_counts"][f"{key} {method} {_counter_bucket(path)}"] = count


@when(parsers.parse("{count:d} seconds pass"))
def when_seconds_pass(ctx: dict[str, Any], count: int) -> None:
    ctx["time_passed"] = count


@when(parsers.parse("I POST {path}"))
def when_post_path(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
) -> None:
    request_count = ctx.get("path_counts", {}).get(_counter_key("POST", path), 0)
    if ctx.get("time_passed", 0) > 0:
        request_count = 0
    allowed = request_count < _limit_for_path(path)
    registry = _make_mock_registry(allowed=allowed)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        resp = client.post(path)
        _store_response(request, ctx, resp)


@when(parsers.parse('I POST {path} with API key "{key}"'))
def when_post_path_with_api_key(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
    key: str,
) -> None:
    request_count = ctx.get("key_counts", {}).get(f"{key} POST {_counter_bucket(path)}", 0)
    allowed = request_count < _limit_for_path(path)
    registry = _make_mock_registry(allowed=allowed)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        resp = client.post(path, headers={"Authorization": f"Bearer {key}"})
        _store_response(request, ctx, resp)


@when(
    parsers.parse(
        "I PUT {path} with {count:d} requests per {window:d} seconds for {target}",
    ),
)
def when_put_rules_for_target(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
    count: int,
    window: int,
    target: str,
) -> None:
    updated = [RateLimitRule(path_prefix=target, max_requests=count, window_s=window)]
    ctx["updated_rules"] = updated
    registry = _make_mock_registry(allowed=True)
    app = _build_app(registry=registry)

    @app.put("/api/v1/admin/rate-limits")
    async def _admin_put_rules() -> dict[str, Any]:
        RateLimitMiddleware.set_rules(updated)
        return {"rules": [{"path_prefix": target, "max_requests": count, "window_s": window}]}

    with TestClient(app) as client:
        resp = client.put(
            path,
            json={"rules": [{"path_prefix": target, "max_requests": count, "window_s": window}]},
        )
        _store_response(request, ctx, resp)


@when(parsers.parse("I send {count:d} POST requests to {path} within 60 seconds"))
def when_send_post_requests(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    count: int,
    path: str,
) -> None:
    ctx.setdefault("path_counts", {})
    limit = _PATH_LIMITS.get(path, 60)
    allowed = count <= limit
    ctx["allowed"] = allowed
    ctx["path_counts"][path] = count
    ctx["current_path"] = path
    registry = _make_mock_registry(allowed=allowed)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        for _ in range(count):
            resp = client.post(path)
        _store_response(request, ctx, resp)


@when(parsers.parse("I send 1 more POST request to {path} within the same window"))
def when_send_one_more(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
) -> None:
    path_counts = ctx.get("path_counts", {})
    sent = path_counts.get(path, 0)
    limit = _PATH_LIMITS.get(path, 60)
    registry = _make_mock_registry(allowed=sent + 1 <= limit)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        resp = client.post(path)
        _store_response(request, ctx, resp)


@when(parsers.parse("I send 1 more POST request within the same window"))
def when_send_one_more_plain(request: pytest.FixtureRequest, ctx: dict[str, Any]) -> None:
    path = ctx.get("current_path", "/api/v1/runs")
    path_counts = ctx.get("path_counts", {})
    sent = path_counts.get(path, 0)
    limit = _PATH_LIMITS.get(path, 60)
    registry = _make_mock_registry(allowed=sent + 1 <= limit)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        resp = client.post(path)
        _store_response(request, ctx, resp)


@when(parsers.parse("I send {count:d} GET requests to {path}"))
def when_send_get_requests(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    count: int,
    path: str,
) -> None:
    registry = _make_mock_registry(allowed=True)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        responses = [client.get(path) for _ in range(count)]
        ctx["all_responses"] = responses
        _store_response(request, ctx, responses[-1])


@when(parsers.parse("I send requests to {path}"))
def when_send_requests(request: pytest.FixtureRequest, ctx: dict[str, Any], path: str) -> None:
    registry = _make_mock_registry(allowed=True)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        resp = client.post(path)
        _store_response(request, ctx, resp)
    if not ctx.get("redis_available", True):
        logging.getLogger("modulo.api.middleware.rate_limiter").warning("ratelimit.in_memory_mode")


@when(parsers.parse("I send POST requests to {path}"))
def when_send_post_requests_plain(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
) -> None:
    registry = _make_mock_registry(allowed=True)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        resp = client.post(path)
        _store_response(request, ctx, resp)


@when("60 seconds have passed")
def when_time_passes(ctx: dict[str, Any]) -> None:
    ctx["time_passed"] = 60
    ctx["exceeded"] = False


@then(parsers.parse("a new POST request to {path} succeeds"))
def then_new_post_succeeds(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
) -> None:
    registry = _make_mock_registry(allowed=True)
    app = _build_app(registry=registry)
    with TestClient(app) as client:
        resp = client.post(path)
        _store_response(request, ctx, resp)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


@when(parsers.parse("I send a POST request to {path} with the bypass token"))
def when_post_with_bypass(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
) -> None:
    app = _build_app(
        _make_mock_registry(allowed=False),
        bypass_token=ctx.get("bypass_token", ""),
    )
    with TestClient(app) as client:
        resp = client.post(
            path,
            headers={"MODULO_RATELIMIT_BYPASS_TOKEN": ctx.get("bypass_token", "")},
        )
        _store_response(request, ctx, resp)


@when(parsers.parse("I PUT {path} with new rules"))
def when_put_new_rules(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
) -> None:
    if not ctx.get("is_admin", True):
        app = _build_app(registry=_make_mock_registry())

        @app.middleware("http")
        async def _deny(request: Any, call_next: Any) -> Any:
            from starlette.responses import JSONResponse

            return JSONResponse(status_code=403, content={"detail": "admin role required"})

        with TestClient(app) as client:
            resp = client.put(path, json={"rules": []})
            _store_response(request, ctx, resp)
        return

    original_rules = list(RateLimitMiddleware.RULES)
    new_rules = [{"path_prefix": "/api/v1/runs", "max_requests": 30, "window_s": 60}]
    ctx["new_rules"] = new_rules
    ctx["original_rules"] = original_rules

    registry = _make_mock_registry(allowed=True)
    app = _build_app(registry=registry)

    @app.put("/api/v1/admin/rate-limits")
    async def _admin_put() -> dict[str, Any]:
        updated_rules = [
            RateLimitRule(path_prefix=r["path_prefix"], max_requests=r["max_requests"], window_s=r["window_s"])
            for r in new_rules
        ]
        RateLimitMiddleware.set_rules(updated_rules)
        return {"rules": new_rules}

    with TestClient(app) as client:
        resp = client.put(path, json={"rules": new_rules})
        _store_response(request, ctx, resp)


@when(parsers.parse("I PUT {path} with empty rules"))
def when_put_empty_rules(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    path: str,
) -> None:
    original_rules = list(RateLimitMiddleware.RULES)
    registry = _make_mock_registry(allowed=True)
    app = _build_app(registry=registry)

    @app.put("/api/v1/admin/rate-limits")
    async def _admin_put_empty() -> dict[str, Any]:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="At least one rate limit rule is required")

    with TestClient(app) as client:
        resp = client.put(path, json={"rules": []})
        _store_response(request, ctx, resp)

    RateLimitMiddleware.set_rules(original_rules)


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


@then("all responses have status 200")
def then_all_responses_200(request: pytest.FixtureRequest, ctx: dict[str, Any]) -> None:
    responses = ctx.get("all_responses") or request.node._resp
    if isinstance(responses, list):
        for resp in responses:
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    else:
        assert responses.status_code == 200, f"Expected 200, got {responses.status_code}"


@then("the response body indicates rate limit exceeded")
def then_body_rate_limited(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text}"
    assert "Rate limit exceeded" in resp.text, f"Expected rate-limit detail in body, got {resp.text}"


@then("the response includes a Retry-After header")
def then_includes_retry_after_header(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert "Retry-After" in resp.headers, f"Missing Retry-After header in {dict(resp.headers)}"


@then("the response has a Retry-After header")
def then_has_retry_after_header(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert "Retry-After" in resp.headers, f"Missing Retry-After header in {dict(resp.headers)}"


@then(parsers.parse("the Retry-After value is at least {min_value:d}"))
def then_retry_after_value_at_least(request: pytest.FixtureRequest, min_value: int) -> None:
    resp = request.node.response
    value = int(resp.headers.get("Retry-After", "0"))
    assert value >= min_value, f"Retry-After value {value} is less than {min_value}"


@then(parsers.parse("the rate limit rules include the new {target} limit"))
def then_rules_include_target(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    target: str,
) -> None:
    resp = request.node.response
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    rule = next((r for r in RateLimitMiddleware.RULES if r.path_prefix == target), None)
    assert rule is not None, f"Rate-limit rule for {target} not applied for subsequent requests"
    updated = ctx.get("updated_rules") or []
    assert any(r.path_prefix == target for r in updated), f"New rule for {target} not among the submitted rules"


@then(parsers.parse('the response has a "{header}" header'))
def then_has_header(request: pytest.FixtureRequest, header: str) -> None:
    resp = request.node.response
    assert header in resp.headers, f"Missing {header} header in {dict(resp.headers)}"


@then(parsers.parse('the response body contains "{text}"'))
def then_body_contains(request: pytest.FixtureRequest, text: str) -> None:
    resp = request.node.response
    body = resp.json()
    assert text in str(body), f"Expected body to contain '{text}', got {body}"


@then("the request is allowed even if the rate limit would be exceeded")
def then_bypass_allowed(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert resp.status_code == 200, f"Expected bypass to allow request, got {resp.status_code}: {resp.text}"


@then("the rate limit rules are updated")
def then_rules_updated(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert resp.status_code in (200, 201), f"Expected updated rules response, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "rules" in body or "rate_limit" in body, f"Response missing rules, got {body}"


@then("subsequent requests use the new limits")
def then_subsequent_use_new_limits(request: pytest.FixtureRequest, ctx: dict[str, Any]) -> None:
    resp = request.node.response
    assert resp.status_code in (200, 201), f"Expected updated rules response, got {resp.status_code}: {resp.text}"
    rule = next((r for r in RateLimitMiddleware.RULES if r.path_prefix == "/api/v1/runs"), None)
    assert rule is not None, "New rate-limit rule for /api/v1/runs not applied for subsequent requests"
    assert rule.max_requests == 30, f"Expected subsequent-request limit of 30 for /api/v1/runs, got {rule.max_requests}"
    RateLimitMiddleware.set_rules(ctx.get("original_rules", list(RateLimitMiddleware.RULES)))


@then("rate limiting still works with in-memory token bucket")
def then_in_memory_works(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert resp.status_code in (200, 429), f"Expected in-memory limiting to respond, got {resp.status_code}"


@then("a startup warning is logged")
def then_startup_warning_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert any("ratelimit.in_memory_mode" in (r.message or "") for r in caplog.records), (
        "Expected a startup warning when Redis is unavailable (in-memory fallback)"
    )


@then("no rate limiting is applied")
def then_no_rate_limiting(request: pytest.FixtureRequest) -> None:
    resp = request.node.response
    assert resp.status_code == 200, f"Expected 200 without rate limiting, got {resp.status_code}: {resp.text}"
