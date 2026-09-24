"""Step definitions for JWT security feature — access tokens, refresh tokens, token family invalidation."""

import base64
import contextlib
import json as json_mod
import time as time_mod
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.rate_limiter import AuthRateLimiter
from modulo.settings import Settings

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/auth/jwt_security.feature")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
        redis_url="",
        modulo_auth_rate_limit_enabled=False,
    )


# ---------------------------------------------------------------------------
# Shared response context
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


def _active_account() -> MagicMock:
    """An active account mock for the FAR-463 refresh-time active check.

    The refresh endpoint now re-reads ACCOUNT.ACTIVE on every call; under the
    fully-mocked BDD session get_account_by_id would otherwise return a bare
    MagicMock whose .active is not exactly True and trip the fail-closed deny.
    """
    account = MagicMock()
    account.active = True
    account.email = "user@example.com"
    return account


def _patch_active_account() -> Any:
    return patch(
        "modulo.api.routes.auth.get_account_by_id",
        new=AsyncMock(return_value=_active_account()),
    )


def _arm_refresh_cookie(client: TestClient, token: str) -> None:
    """FAR-1197: arm the httpOnly refresh transport in the TestClient jar.

    Drops any modulo_refresh already in the jar first — the login response
    seeds one, and duplicate same-name cookies make httpx raise
    CookieConflict on read.
    """
    for cookie in list(client.cookies.jar):
        if cookie.name == "modulo_refresh":
            client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
    client.cookies.set("modulo_refresh", token)


def _response_refresh_cookie(resp: Any) -> str | None:
    """Read the fresh modulo_refresh value straight off the Set-Cookie headers."""
    value = None
    for raw in resp.headers.get_list("Set-Cookie"):
        head = raw.split(";", 1)[0]
        if "=" in head:
            name, _, val = head.partition("=")
            if name.strip() == "modulo_refresh":
                value = val.strip()
    return value


def _post_refresh(client: TestClient) -> Any:
    return client.post("/api/v1/auth/refresh")


def _post_logout(client: TestClient) -> Any:
    return client.post("/api/v1/auth/logout")


# ---------------------------------------------------------------------------
# Token-authenticated TestClient (overrides DB session but NOT get_current_user)
# ---------------------------------------------------------------------------


@pytest.fixture
def token_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import get_settings

    async def override_session() -> Generator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    with (
        patch.object(AuthRateLimiter, "check_login", new=AsyncMock(return_value=(True, 0))),
        patch.object(AuthRateLimiter, "record_failure", new=AsyncMock()),
        patch.object(AuthRateLimiter, "record_success", new=AsyncMock()),
    ):
        yield TestClient(app)

    app.dependency_overrides.clear()


# ===========================================================================
# Scenario 1 — Login returns access + refresh tokens
# ===========================================================================


@given(parsers.parse('a user exists with email "{email}" and password "{password}"'))
def user_exists(email: str, password: str) -> None:
    return


@when(parsers.parse('I POST /api/auth/login with email "{email}" and password "{password}"'))
def login_jwt(email: str, password: str, request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    mock_user = MagicMock()
    mock_user.id = _USER_ID
    mock_user.email = email
    mock_user.organisation_id = _ORG_ID
    mock_user.org_role = "admin"
    mock_user.password_hash = "hash"
    mock_user.active = True
    mock_user.is_system_admin = False

    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth.list_memberships_for_account",
            new=AsyncMock(return_value=[MagicMock(organisation_id=_ORG_ID, role="admin")]),
        ),
    ):
        resp = token_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        _store_response(request, ctx, resp)


@then("the response contains an access_token")
def has_access_token(request: Any) -> None:
    body = request.node.response.json()
    assert "access_token" in body, f"Response missing access_token: {body}"
    assert isinstance(body["access_token"], str), f"access_token is not a string: {body['access_token']}"
    assert body["access_token"], "access_token is empty"


@then("the response contains a refresh_token")
def has_refresh_token(request: Any) -> None:
    # FAR-1197: the refresh token rides in an httpOnly cookie, never the body.
    body = request.node.response.json()
    assert "refresh_token" not in body, f"refresh_token leaked into body: {body.keys()}"
    cookie = request.node.response.cookies.get("modulo_refresh")
    assert cookie, f"Response missing modulo_refresh cookie: {request.node.response.headers.get('set-cookie')}"


# ===========================================================================
# Scenarios 2, 3, 7, 8 — JWT-based /api/auth/me access
# ===========================================================================


@given(parsers.parse('I have a valid JWT for org "{org}"'))
def valid_jwt(request: Any, org: str) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": "testuser",
        "org_id": str(_ORG_ID),
        "user_id": str(_USER_ID),
        "org_role": "admin",
        "iat": now - timedelta(minutes=5),
        "exp": now + timedelta(hours=1),
    }
    token = str(pyjwt.encode(payload, _VALID_32, algorithm="HS256"))
    request.node._jwt_token = token


@given(parsers.parse('I have an expired JWT for org "{org}"'))
def expired_jwt(request: Any, org: str) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": "testuser",
        "org_id": str(_ORG_ID),
        "user_id": str(_USER_ID),
        "org_role": "admin",
        "iat": now - timedelta(hours=48),
        "exp": now - timedelta(hours=1),
    }
    token = str(pyjwt.encode(payload, _VALID_32, algorithm="HS256"))
    request.node._jwt_token = token


@given(parsers.parse('I have a tampered JWT for org "{org}"'))
def tampered_jwt(request: Any, org: str) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": "testuser",
        "org_id": str(_ORG_ID),
        "user_id": str(_USER_ID),
        "org_role": "admin",
        "iat": now - timedelta(minutes=5),
        "exp": now + timedelta(hours=1),
    }
    token = str(pyjwt.encode(payload, _VALID_32, algorithm="HS256"))
    parts = token.split(".")
    parts[2] = "tampered"
    request.node._jwt_token = ".".join(parts)


@given(parsers.parse('I have a JWT with alg=none for org "{org}"'))
def alg_none_jwt(request: Any, org: str) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": "testuser",
        "org_id": str(_ORG_ID),
        "user_id": str(_USER_ID),
        "org_role": "admin",
        "iat": int(now.timestamp()) - 300,
        "exp": int(now.timestamp()) + 3600,
    }
    header_b64 = base64.urlsafe_b64encode(json_mod.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json_mod.dumps(payload).encode()).rstrip(b"=").decode()
    token = f"{header_b64}.{payload_b64}."
    request.node._jwt_token = token


@when(parsers.parse("I make an authenticated request to /api/auth/me"))
def authenticated_request_me(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    token = request.node._jwt_token

    mock_user = MagicMock()
    mock_user.id = _USER_ID
    mock_user.email = "testuser"
    mock_user.display_name = "Test User"
    mock_user.org_role = "admin"
    mock_user.active = True
    mock_user.created_at = datetime.now(UTC)

    with (
        patch("modulo.api.routes.auth.get_account_by_id", new=AsyncMock(return_value=mock_user)),
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value="admin"),
        ),
    ):
        resp = token_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        _store_response(request, ctx, resp)


@then("I see my user profile")
def see_user_profile(request: Any) -> None:
    body = request.node.response.json()
    assert "email" in body, f"Response missing email: {body}"
    assert body["email"] == "testuser", f"Unexpected email: {body['email']}"
    assert "display_name" in body, f"Response missing display_name: {body}"
    assert "org_role" in body, f"Response missing org_role: {body}"


# ===========================================================================
# Scenario 4 — Login then refresh token rotation
# ===========================================================================


@given(parsers.parse('I am logged in as "{email}"'))
def logged_in_as(email: str, request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    family_id = uuid.uuid4()
    mock_family = MagicMock()
    mock_family.family_id = family_id

    mock_user = MagicMock()
    mock_user.id = _USER_ID
    mock_user.email = email
    mock_user.organisation_id = _ORG_ID
    mock_user.org_role = "admin"
    mock_user.password_hash = "hash"
    mock_user.active = True
    mock_user.is_system_admin = False

    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
        patch(
            "modulo.api.routes.auth.list_memberships_for_account",
            new=AsyncMock(return_value=[MagicMock(organisation_id=_ORG_ID, role="admin")]),
        ),
    ):
        resp = token_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "correct-horse-battery"},
        )
        _store_response(request, ctx, resp)

    body = resp.json()
    ctx["login_access_token"] = body["access_token"]
    ctx["login_refresh_token"] = resp.cookies.get("modulo_refresh")
    ctx["token_family_id"] = str(family_id)


@when("I POST /api/auth/refresh with my refresh token")
def refresh_with_stored_token(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    refresh_token = ctx.get("login_refresh_token")
    time_mod.sleep(1.0)
    with (
        _patch_active_account(),
        patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, False, False))),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        _arm_refresh_cookie(token_client, refresh_token)
        resp = _post_refresh(token_client)
        _store_response(request, ctx, resp)

    if resp.status_code == 200:
        body = resp.json()
        ctx["new_access_token"] = body.get("access_token")
        ctx["new_refresh_token"] = _response_refresh_cookie(resp)


@then("the response contains a new access_token")
def has_new_access_token(request: Any, ctx: dict[str, Any]) -> None:
    body = request.node.response.json()
    assert "access_token" in body, f"Response missing access_token: {body}"
    old_token = ctx.get("login_access_token")
    assert body["access_token"] != old_token, "access_token was not rotated"


@then("the response contains a new refresh_token")
def has_new_refresh_token(request: Any, ctx: dict[str, Any]) -> None:
    body = request.node.response.json()
    assert "refresh_token" not in body, "refresh_token leaked into body on rotation"
    old_token = ctx.get("login_refresh_token")
    new_token = request.node.response.cookies.get("modulo_refresh")
    assert new_token, f"Rotation response missing modulo_refresh cookie: {old_token}"
    assert new_token != old_token, "refresh_token was not rotated"


@then("the new tokens differ from the old pair")
def tokens_differ_from_old(request: Any, ctx: dict[str, Any]) -> None:
    body = request.node.response.json()
    assert body["access_token"] != ctx.get("login_access_token"), "access_token was reused"
    old_token = ctx.get("login_refresh_token")
    new_token = ctx.get("new_refresh_token")
    assert new_token is not None, "refresh_token was not rotated"
    assert new_token != old_token, "refresh_token was reused"


# ===========================================================================
# Scenario 5 — Refresh token single-use / theft detection
# ===========================================================================


@given("I have a refresh token with sequence 0")
def refresh_token_seq0(request: Any, ctx: dict[str, Any]) -> None:
    from modulo.auth.jwt import create_refresh_token

    family_id = str(uuid.uuid4())
    ctx["theft_family_id"] = family_id
    token = create_refresh_token(
        "alice",
        _VALID_32,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
        token_family=family_id,
        token_sequence=0,
        client_kind="browser",
    )
    ctx["theft_refresh_token"] = token


@when("I refresh my tokens once")
def refresh_once(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    refresh_token = ctx.get("theft_refresh_token")
    with (
        _patch_active_account(),
        patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, False, False))),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        _arm_refresh_cookie(token_client, refresh_token)
        resp = _post_refresh(token_client)
        _store_response(request, ctx, resp)


@when("I refresh my tokens again with the same refresh token")
def refresh_again_same_token(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    refresh_token = ctx.get("theft_refresh_token")
    with (
        _patch_active_account(),
        patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, True, False))),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        _arm_refresh_cookie(token_client, refresh_token)
        resp = _post_refresh(token_client)
        _store_response(request, ctx, resp)


@when("I refresh my tokens again with the same refresh token within the grace window")
def refresh_again_within_grace(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    """Reuse a refresh token within the grace window — accepted and mints (200)."""
    refresh_token = ctx.get("theft_refresh_token")
    with (
        _patch_active_account(),
        patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, False, True))),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        _arm_refresh_cookie(token_client, refresh_token)
        resp = _post_refresh(token_client)
        _store_response(request, ctx, resp)


@when("I refresh my tokens again with the same refresh token beyond the grace window")
def refresh_again_beyond_grace(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    """Reuse a refresh token beyond the grace window — detected as theft (401)."""
    refresh_token = ctx.get("theft_refresh_token")
    with (
        _patch_active_account(),
        patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, True, False))),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        _arm_refresh_cookie(token_client, refresh_token)
        resp = _post_refresh(token_client)
        _store_response(request, ctx, resp)


@then("the error indicates suspected theft")
def error_suspected_theft(request: Any) -> None:
    body = request.node.response.json()
    detail = body.get("detail", "")
    assert "theft" in detail.lower() or "revoked" in detail.lower(), f"Expected theft-related error, got: {detail}"


# ===========================================================================
# Scenario 6 — Logout / family invalidation
# ===========================================================================


@when(parsers.parse("I POST /api/auth/logout with my refresh token"))
def logout(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    refresh_token = ctx.get("login_refresh_token")
    with patch("modulo.api.routes.auth.blacklist_family", new=AsyncMock(return_value=True)):
        _arm_refresh_cookie(token_client, refresh_token)
        resp = _post_logout(token_client)
        _store_response(request, ctx, resp)


@then("subsequent refresh attempts are rejected")
def refresh_rejected_after_logout(request: Any, ctx: dict[str, Any], token_client: TestClient) -> None:
    refresh_token = ctx.get("login_refresh_token")
    with (
        patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(0, True, False))),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        _arm_refresh_cookie(token_client, refresh_token)
        resp = _post_refresh(token_client)
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"
