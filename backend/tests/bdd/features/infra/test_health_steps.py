"""Step definitions for infra health BDD scenarios.

Supports ``features/infra/health.feature`` — the liveness/readiness endpoint
contract: ``/healthz`` is advisory-only, ``/healthz/ready`` aggregates the
non-advisory checks (database, redis, checkpointer, migrations, SAQ workers,
system crons) plus the FAR-199 dispatcher-reconcile tier, and 503s whenever
any gate is unavailable. Self-contained: each scenario builds a fresh app
over the real health router with only the per-check probes patched to a known
status, so no live Postgres/Redis/browser is required.
"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.routes.health import CheckResult
from modulo.settings import Settings

scenarios("health.feature")

_HEALTH_SETTINGS = Settings(
    database_url="postgresql+asyncpg://localhost/test",
    secret_key="a" * 32,
    fernet_key="a" * 32,
    modulo_admin_password="testpass",
    modulo_csrf_enabled=False,
)

#: Every ``_check_*`` probe the readiness endpoint runs, keyed by the name the
#: feature file uses. ``stale_run_recovery`` and ``break_glass`` are advisory,
#: but the probes are still patched so no real dependency is touched.
_CHECK_PROBES = (
    "database",
    "redis",
    "checkpointer",
    "migrations",
    "saq_workers",
    "system_crons",
    "dispatcher_reconcile",
    "stale_run_recovery",
)

_OUTCOMES = ("ok", "degraded", "unavailable")


def _build_app() -> FastAPI:
    """Fresh app over the real health router with settings overridden."""
    from modulo.api.routes import health as health_mod
    from modulo.settings import get_settings

    app = FastAPI()
    app.include_router(health_mod.router)
    app.dependency_overrides[get_settings] = lambda: _HEALTH_SETTINGS
    return app


def _statuses(request: Any) -> dict[str, str]:
    """Scenario-scoped probe statuses, defaulting everything to ``ok``."""
    state = getattr(request.node, "_health_statuses", None)
    if state is None:
        state = dict.fromkeys(_CHECK_PROBES, "ok")
        request.node._health_statuses = state
    return state


def _result(name: str, status: str) -> CheckResult:
    return CheckResult(status=status, latency_ms=1.0, detail=f"{name} {status}")


@when("I GET the liveness endpoint /healthz")
def _bdd_liveness(request: Any) -> None:
    client = TestClient(_build_app())
    request.node._resp = client.get("/healthz")


@when("I GET the readiness endpoint /healthz/ready")
def _bdd_readiness(request: Any) -> None:
    statuses = _statuses(request)
    with ExitStack() as stack:
        for name, status in statuses.items():
            stack.enter_context(
                patch(
                    f"modulo.api.routes.health._check_{name}",
                    AsyncMock(return_value=_result(name, status)),
                )
            )
        client = TestClient(_build_app())
        request.node._resp = client.get("/healthz/ready")


@given("the readiness probe is set to healthy")
def _bdd_probe_healthy(request: Any) -> None:
    for name in _CHECK_PROBES:
        _statuses(request)[name] = "ok"


@given(parsers.parse("the {check} check is {outcome}"))
def _bdd_probe_status(request: Any, check: str, outcome: str) -> None:
    name = check.replace(" ", "_")
    assert name in _CHECK_PROBES, f"unknown health check {check!r}"
    assert outcome in _OUTCOMES, f"unsupported probe outcome {outcome!r}"
    _statuses(request)[name] = outcome


@then('the liveness body is {"status": "ok"}')
def _bdd_liveness_body(request: Any) -> None:
    assert request.node._resp.json() == {"status": "ok"}


@then(parsers.parse('the readiness overall status is "{status}"'))
def _bdd_readiness_overall(request: Any, status: str) -> None:
    body = request.node._resp.json()
    assert body["status"] == status, f"expected overall status {status!r}, got {body['status']!r}"


@then(parsers.parse('the check "{name}" has status "{status}"'))
def _bdd_check_status(request: Any, name: str, status: str) -> None:
    body = request.node._resp.json()
    assert body["checks"][name]["status"] == status, (
        f"check {name!r} status is {body['checks'][name]['status']!r}, expected {status!r}"
    )


@then("every check has status, latency_ms and detail fields")
def _bdd_check_fields(request: Any) -> None:
    body = request.node._resp.json()
    checks = body["checks"]
    assert checks, "readiness response carries no checks"
    for name, entry in checks.items():
        assert "status" in entry, f"check {name!r} missing 'status'"
        assert "latency_ms" in entry, f"check {name!r} missing 'latency_ms'"
        assert "detail" in entry, f"check {name!r} missing 'detail'"
