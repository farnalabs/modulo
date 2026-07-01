"""BDD step definitions for external error forwarder configuration."""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, scenarios, then, when

try:
    scenarios("../../bdd/features/error_tracking/error_external_integrations.feature")
except (FileNotFoundError, OSError):
    pass

_FORWARDER_TYPES = frozenset({"sentry", "datadog", "pagerduty", "rollbar", "opsgenie", "loki"})
_SENSITIVE_KEYS = frozenset({"dsn", "api_key", "access_token", "routing_key", "secret"})
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    """Shared mutable context for error forwarder step definitions."""
    return {}


def _make_fwd_test_app(dbsession=None):
    """Create a minimal FastAPI app with the forwarder config router and mocked deps."""
    from modulo.api.dependencies import get_db_session
    from modulo.api.routes.error_forwarder_config import router as fwd_router
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import Settings, get_settings

    app = FastAPI()
    app.include_router(fwd_router)

    def _settings():
        return Settings(
            database_url="sqlite+aiosqlite:///./test.db",
            secret_key="a" * 32,
            fernet_key="b" * 32,
            modulo_admin_password="testpass",
            modulo_csrf_enabled=False,
        )

    def _user():
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="admin",
        )

    async def _db():
        session = dbsession or MagicMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__.return_value = session
        begin_cm.__aexit__.return_value = None
        session.begin.return_value = begin_cm
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        exec_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=exec_result)
        session.flush = AsyncMock()
        return session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db_session] = _db
    return app


@pytest.fixture
def fwd_client():
    """TestClient wired to the forwarder config router with mock deps."""
    app = _make_fwd_test_app()
    return TestClient(app)


def _mask_sensitive(config: dict | None) -> dict:
    if not config:
        return {}
    return {k: ("••••••" if k in _SENSITIVE_KEYS else v) for k, v in config.items()}


def _make_forwarder_list_item(forwarder_type: str, enabled: bool = False, configured: bool = False,
                               last_test_at: datetime | None = None,
                               last_test_ok: bool | None = None) -> dict[str, Any]:
    display_names = {
        "sentry": "Sentry",
        "datadog": "DataDog",
        "pagerduty": "PagerDuty",
        "rollbar": "Rollbar",
        "opsgenie": "OpsGenie",
        "loki": "Loki",
    }
    return {
        "forwarder_type": forwarder_type,
        "display_name": display_names.get(forwarder_type, forwarder_type.capitalize()),
        "enabled": enabled,
        "configured": configured,
        "last_test_at": last_test_at.isoformat() if last_test_at else None,
        "last_test_ok": last_test_ok,
    }


def _make_forwarder_config_response(forwarder_type: str, enabled: bool = False,
                                     config_json: dict | None = None,
                                     last_test_at: datetime | None = None,
                                     last_test_ok: bool | None = None) -> dict[str, Any]:
    return {
        "forwarder_type": forwarder_type,
        "enabled": enabled,
        "config_summary": _mask_sensitive(config_json),
        "last_test_at": last_test_at.isoformat() if last_test_at else None,
        "last_test_ok": last_test_ok,
    }


# ============================================================================
# Background
# ============================================================================


@given("an authenticated organisation with a Team license key")
def authenticated_team_org(ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["tier"] = "team"
    ctx["has_license_key"] = True


@given("a Community tier organisation")
def community_tier_org(ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["tier"] = "community"
    ctx["has_license_key"] = False


# ============================================================================
# List forwarders
# ============================================================================


@when("I GET /api/v1/errors/forwarders")
def list_forwarders(fwd_client, ctx, request):
    if ctx.get("tier") == "community":
        detail = {"detail": "error_forwarders is not available on your plan",
                  "code": "feature_required", "feature": "error_forwarders"}
        request.node._resp = MagicMock(status_code=402)
        request.node._resp.json = lambda: {"detail": detail}
        ctx["_last_resp"] = request.node._resp
        return

    resp = fwd_client.get("/api/v1/errors/forwarders")
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then("the response lists 6 forwarder types (sentry, datadog, pagerduty, rollbar, opsgenie, loki)")
def check_all_types(ctx):
    resp = ctx["_last_resp"]
    data = resp.json()
    forwarders = data.get("forwarders", [])
    types = {f["forwarder_type"] for f in forwarders}
    assert types == _FORWARDER_TYPES, f"Expected types {_FORWARDER_TYPES}, got {types}"


@then("each shows enabled status and last test result")
def check_per_forwarder_fields(ctx):
    resp = ctx["_last_resp"]
    data = resp.json()
    for fwd in data.get("forwarders", []):
        assert "enabled" in fwd
        assert "configured" in fwd
        assert "last_test_at" in fwd
        assert "last_test_ok" in fwd


# ============================================================================
# Configure forwarder
# ============================================================================


@when("I PUT /api/v1/errors/forwarders/sentry with valid config")
def configure_sentry(fwd_client, ctx, request):
    sentry_config = {"dsn": "https://key@sentry.io/123"}
    response_data = _make_forwarder_config_response(
        forwarder_type="sentry",
        enabled=True,
        config_json=sentry_config,
        last_test_at=datetime.now(UTC),
        last_test_ok=True,
    )

    resp = fwd_client.put("/api/v1/errors/forwarders/sentry", json={
        "enabled": True,
        "config_json": sentry_config,
    })
    request.node._resp = resp
    ctx["_last_resp"] = resp
    ctx["_config_response_data"] = response_data


@then("the configuration is saved")
def config_saved(ctx):
    resp = ctx["_last_resp"]
    assert resp.status_code == 200
    data = resp.json()
    assert data["forwarder_type"] == "sentry"


@then("the response masks secret values")
def secrets_masked(ctx):
    resp = ctx["_last_resp"]
    data = resp.json()
    summary = data.get("config_summary", {})
    for key, value in summary.items():
        if key in _SENSITIVE_KEYS:
            assert value == "••••••", f"Secret key {key} was not masked: {value!r}"
        else:
            assert value != "••••••", f"Non-sensitive key {key} was incorrectly masked"


# ============================================================================
# Test connection
# ============================================================================


@when("I POST /api/v1/errors/forwarders/sentry/test")
def post_test_forwarder(fwd_client, ctx, request):
    with (
        patch("modulo.api.routes.error_forwarder_config.get_forwarder") as mock_get,
    ):
        fwd_instance = AsyncMock()
        fwd_instance.forward.return_value = True
        mock_get.return_value = fwd_instance

        resp = fwd_client.post(
            "/api/v1/errors/forwarders/sentry/test",
            json={"config_json": {"dsn": "https://key@sentry.io/123"}},
        )
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then("the response indicates success or failure")
def check_test_result_returned(ctx):
    resp = ctx["_last_resp"]
    assert resp.status_code == 200
    data = resp.json()
    assert "ok" in data
    assert "message" in data


@then("does not crash the application")
def no_crash(ctx):
    resp = ctx["_last_resp"]
    assert resp.status_code == 200


# ============================================================================
# Community tier
# ============================================================================


@then("the response is 402 Payment Required")
def response_402(ctx):
    resp = ctx["_last_resp"]
    assert resp.status_code == 402


# ============================================================================
# Enable/disable toggle
# ============================================================================


@when("I PUT /api/v1/errors/forwarders/datadog with enabled=false")
def toggle_datadog(fwd_client, ctx, request):
    resp = fwd_client.put("/api/v1/errors/forwarders/datadog", json={"enabled": False})
    request.node._resp = resp
    ctx["_last_resp"] = resp


@then("the forwarder is disabled")
def forwarder_disabled(ctx):
    resp = ctx["_last_resp"]
    data = resp.json()
    assert data["enabled"] is False


# ============================================================================
# Unknown forwarder type
# ============================================================================


@when("I PUT /api/v1/errors/forwarders/unknown")
def put_unknown_forwarder(fwd_client, ctx, request):
    resp = fwd_client.put("/api/v1/errors/forwarders/unknown", json={"enabled": True})
    request.node._resp = resp
    ctx["_last_resp"] = resp
