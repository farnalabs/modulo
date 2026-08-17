"""BDD step definitions for the health checks feature (feat-infra-health).

Covers liveness (/healthz) and readiness (/healthz/ready) endpoints: the
degraded/unavailable aggregation, per-check status exposure, and the
FAR-199 dispatcher_reconcile two-tier gating semantics. The internal
_check_* coroutines are patched to yield deterministic outcomes, mirroring
the existing FastAPI ``TestClient`` unit tests in
``backend/tests/unit/api/test_health.py``.
"""

import contextlib
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/operations/health_checks.feature")


def _ok(name: str = "ok") -> Any:
    from modulo.api.routes.health import CheckResult

    return CheckResult(status="ok", latency_ms=1.0, detail=f"{name} reachable")


def _degraded(name: str = "degraded") -> Any:
    from modulo.api.routes.health import CheckResult

    return CheckResult(status="degraded", detail=f"{name} degraded")


def _unavailable(name: str = "unavailable") -> Any:
    from modulo.api.routes.health import CheckResult

    return CheckResult(status="unavailable", latency_ms=5000.0, detail=f"{name} down")


@pytest.fixture
def health_ctx() -> dict[str, Any]:
    """Mutable context shared across health-check steps in this module."""
    return {}


# ---------------------------------------------------------------------------
# Steps declaring the state of each dependency check
# ---------------------------------------------------------------------------

_CHECK_PATCH_TARGETS = {
    "database": "modulo.api.routes.health._check_database",
    "redis": "modulo.api.routes.health._check_redis",
    "checkpointer": "modulo.api.routes.health._check_checkpointer",
    "migrations": "modulo.api.routes.health._check_migrations",
    "saq workers": "modulo.api.routes.health._check_saq_workers",
    "system cron": "modulo.api.routes.health._check_system_crons",
    "dispatcher reconcile": "modulo.api.routes.health._check_dispatcher_reconcile",
}

_DEFAULTS = {
    "database": "ok",
    "redis": "ok",
    "checkpointer": "ok",
    "migrations": "ok",
    "saq workers": "ok",
    "system cron": "ok",
    "dispatcher reconcile": "ok",
}


@given(parsers.parse("the {check} check is ok"))
def health_check_ok(check: str, health_ctx: dict[str, Any]) -> None:
    health_ctx["checks"] = dict(_DEFAULTS, **health_ctx.get("checks", {}))
    health_ctx["checks"][check] = "ok"


@given("the application is running")
def application_running() -> None:
    """The TestClient app is already up via the ``client`` fixture."""


@given(parsers.parse("the {check} check is degraded"))
def health_check_degraded(check: str, health_ctx: dict[str, Any]) -> None:
    health_ctx["checks"] = dict(_DEFAULTS, **health_ctx.get("checks", {}))
    health_ctx["checks"][check] = "degraded"


@given(parsers.parse("the {check} check is unavailable"))
def health_check_unavailable(check: str, health_ctx: dict[str, Any]) -> None:
    health_ctx["checks"] = dict(_DEFAULTS, **health_ctx.get("checks", {}))
    health_ctx["checks"][check] = "unavailable"


@when("I GET /healthz/ready")
def get_readiness(client: Any, health_ctx: dict[str, Any], request: Any) -> None:
    states = dict(health_ctx.get("checks", {}))
    with contextlib.ExitStack() as stack:
        for target in _CHECK_PATCH_TARGETS:
            state = states.get(target, "ok")
            factory = {"ok": _ok, "degraded": _degraded, "unavailable": _unavailable}[state]
            stack.enter_context(patch(_CHECK_PATCH_TARGETS[target], AsyncMock(return_value=factory(target))))
        resp = client.get("/healthz/ready")
    request.node._resp = resp
    request.node.response = resp
    health_ctx["response"] = resp


@when("I GET /healthz")
def get_liveness(client: Any, health_ctx: dict[str, Any], request: Any) -> None:
    resp = client.get("/healthz")
    request.node._resp = resp
    request.node.response = resp
    health_ctx["response"] = resp


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


@then(parsers.parse("the response status is {code:d}"))
def health_response_status(code: int, health_ctx: dict[str, Any]) -> None:
    resp = health_ctx.get("response")
    assert resp is not None, "No response stored"
    assert resp.status_code == code, f"Expected {code}, got {resp.status_code}"


@then(parsers.parse('the response body status is "{status}"'))
def health_response_body_status(status: str, health_ctx: dict[str, Any]) -> None:
    resp = health_ctx.get("response")
    assert resp is not None, "No response stored"
    body = resp.json()
    assert body.get("status") == status, f"Expected body status {status!r}, got {body!r}"


@then(parsers.parse('the overall readiness status is "{status}"'))
def overall_readiness_status(status: str, health_ctx: dict[str, Any]) -> None:
    resp = health_ctx.get("response")
    assert resp is not None, "No response stored"
    body = resp.json()
    assert body.get("status") == status, f"Expected overall status {status!r}, got {body!r}"


@then(parsers.parse('the readiness response reports every non-advisory check as "{status}"'))
def readiness_checks_ok(status: str, health_ctx: dict[str, Any]) -> None:
    resp = health_ctx.get("response")
    assert resp is not None, "No response stored"
    body = resp.json()
    checks = body.get("checks", {})
    for key in ("database", "redis", "checkpointer", "migrations", "saq_workers", "system_crons"):
        assert key in checks, f"Missing readiness check key: {key}"
        assert checks[key]["status"] == status, f"Expected {key} check status {status!r}, got {checks[key]['status']!r}"
