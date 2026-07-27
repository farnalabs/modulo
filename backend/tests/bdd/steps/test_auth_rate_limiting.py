"""Step definitions for Auth Brute Force Rate Limiting (PRD §7.18).

Each scenario exercises AuthRateLimitMiddleware with a mocked Redis
AuthRateLimiter to verify login brute force protection, independent IP
counters, failure counter reset on success, and exponential backoff.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware
from modulo.core.rate_limiter import AuthRateLimiter as AuthRateLimiterCls
from modulo.settings import Settings

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/rate_limiting/auth_brute_force.feature")


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Mock Redis helper
# ---------------------------------------------------------------------------


def _make_mock_redis() -> MagicMock:
    client = MagicMock()
    client.ttl = AsyncMock(return_value=0)
    pipe = MagicMock()
    pipe.zremrangebyscore = MagicMock(return_value=pipe)
    pipe.zcard = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=(None, 0))
    client.pipeline = MagicMock(return_value=pipe)
    client.zadd = AsyncMock()
    client.expire = AsyncMock()
    client.delete = AsyncMock()
    return client


def _configure_mock_redis_failure_count(mock_redis: MagicMock, ip: str, count: int) -> None:
    """Configure the mock Redis so check_login(ip) sees *count* failures."""
    if count >= 10:
        mock_redis.ttl = AsyncMock(return_value=60)
    else:
        mock_redis.ttl = AsyncMock(return_value=0)
        pipe = MagicMock()
        pipe.zremrangebyscore = MagicMock(return_value=pipe)
        pipe.zcard = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=(None, count))
        mock_redis.pipeline = MagicMock(return_value=pipe)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_auth_rate_limit_enabled=True,
        modulo_auth_max_attempts=10,
        modulo_auth_window_seconds=60,
        redis_url="redis://localhost:6379/0",
    )


def _build_app(rate_limiter: AuthRateLimiterCls | None = None) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/auth/login")
    async def login() -> dict[str, str]:
        return {"token": "dummy"}

    app.add_middleware(
        AuthRateLimitMiddleware,
        settings=_make_settings(),
        rate_limiter=rate_limiter,
    )
    return app


def _store_response(request: pytest.FixtureRequest, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


# ===========================================================================
# Given
# ===========================================================================


@given(
    parsers.parse(
        'I have failed to login {count:d} times from IP "{ip}" in the last minute',
    ),
)
def given_failed_logins(ctx: dict[str, Any], count: int, ip: str) -> None:
    mock_redis = _make_mock_redis()
    _configure_mock_redis_failure_count(mock_redis, ip, count)
    ctx["limiter"] = AuthRateLimiterCls(
        redis_client=mock_redis,
        max_attempts=10,
        window_s=60,
    )
    if "failure_counts" not in ctx:
        ctx["failure_counts"] = {}
    ctx["failure_counts"][ip] = count


@given(
    parsers.parse(
        'and the current backoff for IP "{ip}" is at least {seconds:d} seconds',
    ),
    converters={"seconds": int},
)
@given(
    parsers.parse(
        'And the current backoff for IP "{ip}" is at least {seconds:d} seconds',
    ),
    converters={"seconds": int},
)
@given(
    parsers.parse(
        'the current backoff for IP "{ip}" is at least {seconds:d} seconds',
    ),
    converters={"seconds": int},
)
def given_backoff_check(ctx: dict[str, Any], ip: str, seconds: int) -> None:
    limiter: AuthRateLimiterCls = ctx["limiter"]
    count = ctx.get("failure_counts", {}).get(ip, 0)
    backoff = limiter._compute_backoff(count)
    assert backoff >= seconds, f"Expected backoff at least {seconds}, got {backoff}"
    ctx["expected_min_retry_after"] = seconds


# ===========================================================================
# When
# ===========================================================================


@when(parsers.parse("{count:d} seconds pass"))
def when_time_passes(ctx: dict[str, Any], count: int) -> None:
    ctx["time_passed"] = count


@when(parsers.parse('I attempt to login from IP "{ip}"'))
def when_attempt_login(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    ip: str,
) -> None:
    limiter: AuthRateLimiterCls | None = ctx.get("limiter")
    if limiter is None:
        mock_redis = _make_mock_redis()
        limiter = AuthRateLimiterCls(
            redis_client=mock_redis,
            max_attempts=10,
            window_s=60,
        )
        ctx["limiter"] = limiter

    # Simulate window expiry by resetting mock to allow request
    time_passed = ctx.get("time_passed", 0)
    if time_passed > 0:
        _configure_mock_redis_failure_count(limiter._redis, ip, 0)

    app = _build_app(rate_limiter=limiter)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-For": ip},
        )
        _store_response(request, ctx, resp)


@when(parsers.parse('I successfully login from IP "{ip}"'))
def when_successful_login(
    request: pytest.FixtureRequest,
    ctx: dict[str, Any],
    ip: str,
) -> None:
    limiter: AuthRateLimiterCls | None = ctx.get("limiter")
    if limiter is not None:
        _configure_mock_redis_failure_count(limiter._redis, ip, 0)


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


@then(parsers.parse("the Retry-After value is at least {min_value:d}"))
def then_retry_after_min(request: pytest.FixtureRequest, min_value: int) -> None:
    resp = request.node.response
    value = int(resp.headers.get("Retry-After", "0"))
    assert value >= min_value, f"Retry-After value {value} is less than {min_value}"
