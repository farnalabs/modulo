"""Step definitions for license management: upload, inspect, and verify license keys."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../features/licensing/license_management.feature")
except (FileNotFoundError, OSError):
    pass

from modulo.core.license import (
    clear_license,
    parse_and_verify,
    set_public_key,
    store_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive

_TEST_KP = generate_keypair()
_TEST_PRIV = _TEST_KP["private_key"]
_TEST_PUB = _TEST_KP["public_key"]


def _sign_license_payload(payload: dict, private_key: str = _TEST_PRIV) -> str:
    sig_hex = sign_primitive(payload, private_key)
    sig_bytes = bytes.fromhex(sig_hex)
    payload_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "enterprise",
        "features": ["sso", "team_rbac", "audit_viewer", "admin_spend_limits"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "acme-org",
    }
    payload.update(overrides)
    return payload


def _make_mock_session() -> Any:
    from unittest.mock import AsyncMock, MagicMock

    session = AsyncMock()

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_scalars.first.return_value = None

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = None
    mock_result.first.return_value = None

    session.execute = AsyncMock(return_value=mock_result)

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock()

    return session


def _setup_client(ctx: dict[str, Any]) -> None:
    from modulo.api.dependencies import _get_engine as _eng
    from modulo.api.dependencies import get_db_session
    from modulo.api.main import app as _app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import Settings, get_settings

    set_public_key(_TEST_PUB)

    mock_session = _make_mock_session()

    async def _override_session():
        yield mock_session

    is_admin = ctx.get("is_admin", True)
    _valid_32 = "a" * 32
    _settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_valid_32,
        fernet_key=_valid_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
    )

    _app.dependency_overrides[get_settings] = lambda: _settings
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_eng] = lambda: None
    _app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin" if is_admin else "operator",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin" if is_admin else "operator",
    )

    get_settings.cache_clear()


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


@pytest.fixture
def client() -> Any:
    from fastapi.testclient import TestClient

    from modulo.api.main import app

    return TestClient(app)


# ── Given steps ──────────────────────────────────────────────────────────────


@given(parsers.parse("I am authenticated as an admin in org {org}"))
def authenticated_admin(org: str, ctx: dict[str, Any]) -> None:
    ctx["org"] = org
    ctx["is_admin"] = True


@given(parsers.parse("I am authenticated as a non-admin user"))
def non_admin_user(ctx: dict[str, Any]) -> None:
    ctx["is_admin"] = False


@given("I have a signed enterprise license key")
def signed_enterprise_key(ctx: dict[str, Any]) -> None:
    payload = _valid_payload()
    ctx["license_key"] = _sign_license_payload(payload)


@given("I have a tampered license key")
def tampered_license_key(ctx: dict[str, Any]) -> None:
    payload = _valid_payload()
    key = _sign_license_payload(payload)
    parts = key.split(".")
    tampered_payload_b64 = (
        base64.urlsafe_b64encode(
            json.dumps({"tier": "free"}, separators=(",", ":"), sort_keys=True).encode()
        )
        .decode()
        .rstrip("=")
    )
    ctx["license_key"] = f"{tampered_payload_b64}.{parts[1]}"


@given("I have an expired license key")
def expired_license_key(ctx: dict[str, Any]) -> None:
    payload = _valid_payload(
        expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()
    )
    ctx["license_key"] = _sign_license_payload(payload)


@given("I do not have a license")
def no_license(ctx: dict[str, Any]) -> None:
    ctx["license_key"] = None
    clear_license()


@given("I have stored a valid enterprise license")
def stored_valid_license(ctx: dict[str, Any]) -> None:
    payload = _valid_payload()
    key = _sign_license_payload(payload)
    result = parse_and_verify(key)
    assert result.license_data is not None
    store_license(key, result.license_data)
    ctx["stored_key"] = key


@given("I have stored a valid enterprise license with a known expiry")
def stored_license_with_expiry(ctx: dict[str, Any]) -> None:
    payload = _valid_payload()
    key = _sign_license_payload(payload)
    result = parse_and_verify(key)
    assert result.license_data is not None
    store_license(key, result.license_data)
    ctx["stored_key"] = key
    ctx["expected_expiry"] = payload["expires_at"]


# ── When steps ────────────────────────────────────────────────────────────────


@when(parsers.parse("I POST the license key to /api/v1/admin/license"))
def post_license_key(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.post("/api/v1/admin/license", json={"license_key": ctx["license_key"]})
    _store_response(request, ctx, resp)


@when(parsers.parse("I POST a license key to /api/v1/admin/license"))
def post_license_key_generic(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.post(
        "/api/v1/admin/license", json={"license_key": ctx.get("license_key", "dGVzdA==.dGVzdA==")}
    )
    _store_response(request, ctx, resp)


@when(parsers.parse("I GET /api/v1/admin/license"))
def get_license_status(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_client(ctx)
    resp = client.get("/api/v1/admin/license")
    _store_response(request, ctx, resp)


# ── Then steps ────────────────────────────────────────────────────────────────


@then(parsers.parse("the response status is {status:d}"))
def check_response_status(status: int, request: Any) -> None:
    resp = request.node._resp
    assert resp.status_code == status, (
        f"Expected status {status}, got {resp.status_code} - body: {resp.text}"
    )


@then(parsers.parse('the response contains tier "{tier}"'))
def response_contains_tier(tier: str, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data["tier"] == tier, f"Expected tier '{tier}', got '{data['tier']}'"


@then(parsers.parse('the response contains features "{features}"'))
def response_contains_features(features: str, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    expected = [f.strip() for f in features.split(",")]
    for feat in expected:
        assert feat in data["features"], (
            f"Expected feature '{feat}' in {data['features']}"
        )


@then(parsers.parse('the error detail mentions "{text}"'))
def error_detail_mentions(text: str, request: Any) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "")
    assert text.lower() in detail.lower(), (
        f"Expected detail to mention '{text}', got '{detail}'"
    )


@then("the response shows free tier")
def response_shows_free_tier(request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data["tier"] == "free", f"Expected free tier, got '{data['tier']}'"
    assert data["features"] == [], f"Expected empty features, got {data['features']}"


@then("the response has_license is false")
def response_has_license_false(request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data["has_license"] is False, "Expected has_license to be false"


@then("the response has_license is true")
def response_has_license_true(request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data["has_license"] is True, "Expected has_license to be true"


@then(parsers.parse('the response contains org_id "{org_id}"'))
def response_contains_org_id(org_id: str, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data.get("org_id") == org_id, (
        f"Expected org_id '{org_id}', got '{data.get('org_id')}'"
    )


@then(parsers.parse('the response features include "{feature}"'))
def response_features_include(feature: str, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert feature in data["features"], (
        f"Expected feature '{feature}' in {data['features']}"
    )


@then("the response contains an expires_at date")
def response_contains_expires_at(request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert data.get("expires_at") is not None, "Expected expires_at to be present"
    assert len(data["expires_at"]) > 0, "Expected non-empty expires_at"
