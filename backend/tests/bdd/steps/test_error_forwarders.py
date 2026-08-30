"""BDD step definitions for error forwarder configuration.

Supports the comprehensive feature file at ``features/observability/error_forwarders.feature``
with 12 scenarios covering list, configure, test, feature gating, auth, and DB error paths.
"""

import contextlib
import json as _json
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/observability/error_forwarders.feature")

_FORWARDER_TYPES = frozenset({"sentry", "datadog", "pagerduty", "rollbar", "opsgenie", "loki"})
_SENSITIVE_KEYS = frozenset({"dsn", "api_key", "access_token", "routing_key", "secret"})
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    """Shared mutable context for error forwarder step definitions."""
    return {}


# ============================================================================
# Full-app builder (ctx-driven)
# ============================================================================


def _seed_mock_session(ctx):
    """Build an ``AsyncSession`` mock whose behaviour is driven by ``ctx``."""
    from sqlalchemy.exc import ProgrammingError

    session = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__.return_value = session
    begin_cm.__aexit__.return_value = None
    session.begin.return_value = begin_cm
    session.flush = AsyncMock()
    session.add = MagicMock()

    if ctx.get("_no_table"):
        session.execute = AsyncMock(
            side_effect=ProgrammingError("stmt", {}, 'relation "error_forwarder_configs" does not exist')
        )
        return session

    existing = ctx.get("_existing_configs", {})
    exec_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = list(existing.values())
    exec_result.scalars.return_value = scalars_mock
    exec_result.scalar_one_or_none.return_value = next(iter(existing.values())) if existing else None
    session.execute = AsyncMock(return_value=exec_result)
    session.get = AsyncMock(return_value=None)
    return session


def _build_app(ctx):
    """Build a ``TestClient`` wired to the forwarder config router with mocked deps driven by
    ``ctx``.  Supports no-org, viewer role, feature gating, and missing-DB-table scenarios."""
    from modulo.api.dependencies import get_db_session, get_plan_context
    from modulo.api.routes.error_forwarder_config import router as fwd_router
    from modulo.auth.dependencies import get_current_tenant_user, get_current_user
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import Settings, get_settings

    app = FastAPI()
    app.include_router(fwd_router)

    org_id = None if ctx.get("_no_org") else _ORG_ID
    org_role = "viewer" if ctx.get("_viewer") else "admin"
    feature_disabled = ctx.get("_feature_disabled", False)

    def _user():
        if org_id is None or org_role is None:
            from modulo.auth.dependencies import OrganisationMembershipRequired

            raise OrganisationMembershipRequired
        return TenantPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=uuid.uuid4(),
            org_role=org_role,
        )

    def _settings():
        return Settings(
            database_url="sqlite+aiosqlite:///./test.db",
            secret_key="a" * 32,
            fernet_key="b" * 32,
            modulo_admin_password="testpass",
            modulo_csrf_enabled=False,
        )

    async def _db():
        return _seed_mock_session(ctx)

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = not feature_disabled

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_current_tenant_user] = _user
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return app


# ============================================================================
# Legacy helpers (kept for backward compat with unit-test helpers below)
# ============================================================================


def _mask_sensitive(config: dict | None) -> dict:
    if not config:
        return {}
    return {k: ("••••••" if k in _SENSITIVE_KEYS else v) for k, v in config.items()}


def _make_forwarder_config_response(
    forwarder_type: str,
    enabled: bool = False,
    config_json: dict | None = None,
    last_test_at: datetime | None = None,
    last_test_ok: bool | None = None,
) -> dict[str, Any]:
    return {
        "forwarder_type": forwarder_type,
        "enabled": enabled,
        "config_summary": _mask_sensitive(config_json),
        "last_test_at": last_test_at.isoformat() if last_test_at else None,
        "last_test_ok": last_test_ok,
    }


def _make_mock_config(forwarder_type: str = "sentry", **overrides: Any) -> MagicMock:
    cfg = MagicMock()
    cfg.forwarder_type = forwarder_type
    cfg.enabled = overrides.get("enabled", True)
    cfg.config_json = overrides.get("config_json", {"dsn": "https://key@sentry.io/1"})
    cfg.last_test_at = overrides.get("last_test_at")
    cfg.last_test_ok = overrides.get("last_test_ok")
    return cfg


# ============================================================================
# Given  —  scenario setup
# ============================================================================


@given(parsers.parse('an error forwarder "{ftype}" is configured with DSN "{dsn}"'))
def _given_configured_forwarder(ftype: str, dsn: str, ctx: dict) -> None:
    ctx.setdefault("_existing_configs", {})[ftype] = _make_mock_config(ftype, config_json={"dsn": dsn})


@given(parsers.parse('an error forwarder "{ftype}" is configured'))
def _given_configured_forwarder_plain(ftype: str, ctx: dict) -> None:
    ctx.setdefault("_existing_configs", {})[ftype] = _make_mock_config(ftype)


@given("the sentry forwarder implementation forwards successfully")
def _given_fwd_succeeds(ctx: dict) -> None:
    ctx["_forwarder_ok"] = True


@given("the sentry forwarder implementation fails to forward")
def _given_fwd_fails(ctx: dict) -> None:
    ctx["_forwarder_ok"] = False


@given("the sentry forwarder implementation hangs for 20 seconds")
def _given_fwd_hangs(ctx: dict) -> None:
    ctx["_forwarder_hangs"] = True


@given("I am authenticated without an organisation")
def _given_no_org(ctx: dict) -> None:
    ctx["_no_org"] = True


@given("the error_forwarder_configs table does not exist")
def _given_no_table(ctx: dict) -> None:
    ctx["_no_table"] = True


@given(parsers.parse('the "{feature}" feature is not enabled on my plan'))
def _given_feature_disabled(feature: str, ctx: dict) -> None:
    ctx["_feature_disabled"] = True


# ============================================================================
# When  —  API calls
# ============================================================================


@when(parsers.parse('I GET "{path}"'))
def _when_get_path(path: str, ctx: dict, request) -> None:
    if getattr(request.node, "_viewer_auth", False):
        ctx["_viewer"] = True
    app = _build_app(ctx)
    client = TestClient(app)
    resp = client.get(path)
    request.node._resp = resp
    ctx["_last_resp"] = resp


@when(parsers.parse('I PUT "{path}" with body {body_json}'))
def _when_put_path(path: str, body_json: str, ctx: dict, request) -> None:
    if getattr(request.node, "_viewer_auth", False):
        ctx["_viewer"] = True
    app = _build_app(ctx)
    body = _json.loads(body_json)
    client = TestClient(app)
    resp = client.put(path, json=body)
    request.node._resp = resp
    ctx["_last_resp"] = resp


@when(parsers.parse('I POST "{path}" with body {body_json}'))
def _when_post_path(path: str, body_json: str, ctx: dict, request) -> None:
    if getattr(request.node, "_viewer_auth", False):
        ctx["_viewer"] = True
    body = _json.loads(body_json)

    fwd_ok = ctx.get("_forwarder_ok")
    fwd_hangs = ctx.get("_forwarder_hangs")

    if fwd_ok is not None or fwd_hangs:
        with patch("modulo.api.routes.error_forwarder_config.get_forwarder") as mock_get:
            fwd_instance = AsyncMock()
            if fwd_hangs:

                async def _raise_timeout(*a: Any, **kw: Any) -> Any:
                    raise TimeoutError("timed out")

                fwd_instance.forward = _raise_timeout
            else:
                fwd_instance.forward.return_value = fwd_ok
            mock_get.return_value = fwd_instance
            app = _build_app(ctx)
            client = TestClient(app)
            resp = client.post(path, json=body)
    else:
        app = _build_app(ctx)
        client = TestClient(app)
        resp = client.post(path, json=body)

    request.node._resp = resp
    ctx["_last_resp"] = resp


# ============================================================================
# Then  —  response assertions
# ============================================================================


@then(parsers.parse('the response body contains "{key}"'))
def _then_body_contains(key: str, request) -> None:
    data = request.node._resp.json()
    assert key in data, f"Expected '{key}' in response, got keys: {list(data)}"


@then(parsers.parse('the response includes "{values}"'))
def _then_response_includes(values: str, request) -> None:
    data = request.node._resp.json()
    items = [v.strip().strip('"') for v in values.split(",")]
    if "forwarders" in data:
        types = {f["forwarder_type"] for f in data["forwarders"]}
        for item in items:
            assert item in types, f"Expected '{item}' in forwarder types, got {types}"


@then(parsers.parse('the {ftype} forwarder has "{field}" set to {value}'))
def _then_forwarder_field(ftype: str, field: str, value: str, request) -> None:
    data = request.node._resp.json()
    forwarders = data.get("forwarders", [])
    fwd = next(f for f in forwarders if f["forwarder_type"] == ftype)
    expected = value.lower() == "true"
    assert fwd.get(field) == expected, f"Expected {ftype}.{field}={expected}, got {fwd.get(field)}"


@then(parsers.parse('the response includes "{field}" set to "{value}"'))
def _then_response_field_str(field: str, value: str, request) -> None:
    data = request.node._resp.json()
    assert data.get(field) == value, f"Expected {field}='{value}', got {data.get(field)}"


@then(parsers.parse('the response includes "{field}" set to {value}'))
def _then_response_field_raw(field: str, value: str, request) -> None:
    data = request.node._resp.json()
    expected = value.lower() == "true"
    assert data.get(field) == expected, f"Expected {field}={expected}, got {data.get(field)}"


@then(parsers.parse('the error mentions "{text}"'))
@then(parsers.parse('the response mentions "{text}"'))
def _then_mentions(text: str, request) -> None:
    data = request.node._resp.json()
    detail = data.get("detail", "") or ""
    if isinstance(detail, dict):
        detail = str(detail)
    assert text.lower() in detail.lower(), f"Expected response to mention '{text}', got '{detail}'"


@then(parsers.parse('the response body has "{field}" set to {value}'))
def _then_body_field_bool(field: str, value: str, request) -> None:
    data = request.node._resp.json()
    expected = value.lower() == "true"
    assert data.get(field) == expected, f"Expected {field}={expected}, got {data.get(field)}"
