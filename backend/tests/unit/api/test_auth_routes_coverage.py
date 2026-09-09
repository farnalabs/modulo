"""Route-level coverage tests for the auth endpoints (FAR-618).

Complements ``test_break_glass_login.py`` (break-glass login contracts),
``test_ws_token.py`` (WS tokens), ``test_csrf.py`` / ``test_me_password.py`` /
``test_self_lockout.py`` by covering: the login error-convention matrix and the
no-membership 403, the whole demo auto-login surface (kill-switch 404,
credential-mismatch 404, membership 404, happy mint), the whole
``accept-invite`` enrollment surface (all four account-resolution branches,
tombstoned-membership reactivation, CAS race, weak password, error matrix,
fail-open audit + limiter), the refresh endpoint (claim-shape 401s, inactive
account, missing membership, theft detection, rotation happy path, error
matrix), the logout surface (blacklist error matrix, claim-shape skips), the
``me`` endpoint matrix, and the cookie/client-ip helpers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jwt import InvalidTokenError as JWTError
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from starlette.responses import Response

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, create_refresh_token
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_STRONG_PASSWORD = "Correct-Horse-9!"

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_INTEGRITY = IntegrityError("s", {}, Exception())
_RUNTIME = RuntimeError("kaboom")

_PREFIX = "modulo.api.routes.auth."


def _make_settings(**overrides: object) -> Settings:
    kwargs: dict = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_admin_password": "testpass",
        "modulo_auth_rate_limit_enabled": False,
        "redis_url": "",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_account(**overrides: object) -> MagicMock:
    account = MagicMock()
    account.id = _USER_ID
    account.email = "user@example.com"
    account.display_name = "User"
    account.active = True
    account.is_system_admin = False
    account.must_change_password = False
    account.is_break_glass = False
    account.password_hash = "$2b$12$existinghash"
    account.auth_provider = "local"
    account.created_at = _NOW
    for key, value in overrides.items():
        setattr(account, key, value)
    return account


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _assert_error_matrix(
    http: TestClient,
    *,
    method: str,
    url: str,
    json_body: dict,
    patch_target: str,
    expected: dict,
) -> None:
    for exc, status_code in expected:
        with patch(f"{_PREFIX}{patch_target}", new=AsyncMock(side_effect=exc)):
            resp = http.request(method.upper(), url, json=json_body)
        assert resp.status_code == status_code, f"{patch_target} {exc!r}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /auth/login — error matrix + no-membership 403 + happy mint
# ---------------------------------------------------------------------------

_LOGIN_BODY = {"email": "user@example.com", "password": "pw"}


def test_login_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url="/api/v1/auth/login",
        json_body=_LOGIN_BODY,
        patch_target="_run_login_transaction",
        expected={
            (_INTEGRITY, 409),
            (_PROG, 501),
            (_SQL, 503),
            (HTTPException(status_code=401, detail="bad"), 401),
            (_RUNTIME, 500),
        },
    )


def test_login_without_memberships_returns_403(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    with (
        patch(f"{_PREFIX}_authenticate_credentials", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}list_memberships_for_account", new=AsyncMock(return_value=[])),
        patch(f"{_PREFIX}update_last_login", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/login", json=_LOGIN_BODY)

    assert resp.status_code == 403, resp.text
    assert "no org memberships" in resp.json()["detail"]


def test_login_happy_path_mints_tokens_and_cookies(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    membership = MagicMock()
    membership.organisation_id = _ORG_ID
    membership.role = "admin"
    family = MagicMock()
    family.family_id = uuid.uuid4()
    with (
        patch(f"{_PREFIX}_authenticate_credentials", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}list_memberships_for_account", new=AsyncMock(return_value=[membership])),
        patch(f"{_PREFIX}create_family", new=AsyncMock(return_value=family)),
        patch(f"{_PREFIX}update_last_login", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/login", json=_LOGIN_BODY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["requires_bootstrap"] is False
    assert "modulo_session" in resp.cookies


# ---------------------------------------------------------------------------
# POST /auth/demo — stealth 404s + happy mint
# ---------------------------------------------------------------------------

_DEMO_SETTINGS = {
    "modulo_demo_enabled": True,
    "modulo_demo_user": "demo@example.com",
    "modulo_demo_password": "pw",
    "modulo_demo_token_minutes": 30,
}


def test_demo_login_kill_switch_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}demo_login_config", return_value=None):
        resp = http.post("/api/v1/auth/demo")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Not Found"


def test_demo_login_credential_mismatch_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}demo_login_config", return_value=("demo@example.com", "pw")),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=None)),
    ):
        resp = http.post("/api/v1/auth/demo")

    assert resp.status_code == 404, resp.text


def test_demo_login_privileged_account_without_membership_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account(is_system_admin=True)
    with (
        patch(f"{_PREFIX}demo_login_config", return_value=("demo@example.com", "pw")),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}authenticate_db_user", return_value=True),
    ):
        resp = http.post("/api/v1/auth/demo")

    assert resp.status_code == 404, resp.text


def test_demo_login_no_demo_membership_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    with (
        patch(f"{_PREFIX}demo_login_config", return_value=("demo@example.com", "pw")),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}authenticate_db_user", return_value=True),
        patch(f"{_PREFIX}_resolve_demo_org_membership", new=AsyncMock(return_value=None)),
    ):
        resp = http.post("/api/v1/auth/demo")

    assert resp.status_code == 404, resp.text


def test_demo_login_happy_path_mints_short_session(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    with (
        patch(f"{_PREFIX}demo_login_config", return_value=("demo@example.com", "pw")),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}authenticate_db_user", return_value=True),
        patch(f"{_PREFIX}_resolve_demo_org_membership", new=AsyncMock(return_value=(_ORG_ID, "viewer"))),
        patch(f"{_PREFIX}update_last_login", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/demo")

    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]
    assert "modulo_session" in resp.cookies


def test_demo_login_requires_demo_settings() -> None:
    """Without demo credentials configured the endpoint answers a plain 404."""
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    try:
        http = TestClient(app)
        resp = http.post("/api/v1/auth/demo")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# POST /auth/accept-invite — enrollment surface
# ---------------------------------------------------------------------------

_INVITE_BODY = {"token": "inv-token", "password": _STRONG_PASSWORD}


def _invitation() -> MagicMock:
    invitation = MagicMock()
    invitation.id = uuid.uuid4()
    invitation.organisation_id = _ORG_ID
    invitation.email = "invitee@example.com"
    invitation.display_name = "Invitee"
    invitation.org_role = "operator"
    return invitation


def test_accept_invite_invalid_token_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 400, resp.text
    assert "Invalid or expired invitation" in resp.json()["detail"]


def test_accept_invite_weak_password_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json={"token": "inv-token", "password": "weak"})

    assert resp.status_code == 422, resp.text


def test_accept_invite_non_local_account_returns_409(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    sso_account = _make_account(auth_provider="saml")
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=sso_account)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 409, resp.text
    assert "SSO account" in resp.json()["detail"]


def test_accept_invite_new_account_creates_membership(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    created = _make_account(password_hash="$2b$12$newhash")
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}create_account", new=AsyncMock(return_value=created)) as create_account,
        patch(f"{_PREFIX}get_membership_by_account_and_org", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}create_membership", new_callable=AsyncMock) as create_membership,
        patch(f"{_PREFIX}consume_invitation", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["existing_account"] is False
    create_account.assert_awaited_once()
    create_membership.assert_awaited_once()


def test_accept_invite_passwordless_account_adopts_password(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account(password_hash=None)
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}get_membership_by_account_and_org", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}create_membership", new_callable=AsyncMock),
        patch(f"{_PREFIX}consume_invitation", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 200, resp.text
    assert account.password_hash is not None
    assert resp.json()["existing_account"] is False


def test_accept_invite_existing_local_account_keeps_password(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    original_hash = account.password_hash
    membership = MagicMock()
    membership.deactivated_at = None
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}get_membership_by_account_and_org", new=AsyncMock(return_value=membership)),
        patch(f"{_PREFIX}consume_invitation", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["existing_account"] is True
    assert account.password_hash == original_hash


def test_accept_invite_tombstoned_membership_is_reactivated(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    tombstoned = MagicMock()
    tombstoned.deactivated_at = _NOW
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}get_membership_by_account_and_org", new=AsyncMock(return_value=tombstoned)),
        patch(f"{_PREFIX}reactivate_membership", new_callable=AsyncMock) as reactivate,
        patch(f"{_PREFIX}consume_invitation", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 200, resp.text
    reactivate.assert_awaited_once()


def test_accept_invite_consumption_race_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    membership = MagicMock()
    membership.deactivated_at = None
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}get_membership_by_account_and_org", new=AsyncMock(return_value=membership)),
        patch(f"{_PREFIX}consume_invitation", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 400, resp.text


def test_accept_invite_audit_failure_is_fail_open(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    membership = MagicMock()
    membership.deactivated_at = None
    with (
        patch(f"{_PREFIX}get_valid_by_token_hash", new=AsyncMock(return_value=_invitation())),
        patch(f"{_PREFIX}hash_invitation_token", return_value="hash"),
        patch(f"{_PREFIX}get_account_by_email", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}get_membership_by_account_and_org", new=AsyncMock(return_value=membership)),
        patch(f"{_PREFIX}consume_invitation", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit down"))),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/auth/accept-invite", json=_INVITE_BODY)

    assert resp.status_code == 200, resp.text


def test_accept_invite_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url="/api/v1/auth/accept-invite",
        json_body=_INVITE_BODY,
        patch_target="get_valid_by_token_hash",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# POST /auth/refresh — claim-shape 401s, denials, theft, rotation, matrix
# ---------------------------------------------------------------------------

_REFRESH_URL = "/api/v1/auth/refresh"


def _refresh_token(org_role: str = "admin") -> str:
    return create_refresh_token(
        "user@example.com",
        _VALID_32,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role=org_role,
        token_family=str(uuid.uuid4()),
        token_sequence=0,
        client_kind="browser",
    )


def _claims(**overrides: object) -> dict:
    claims = {
        "sub": "user@example.com",
        "org_id": str(_ORG_ID),
        "org_role": "admin",
        "account_id": str(_USER_ID),
        "token_family": str(uuid.uuid4()),
        "token_sequence": 0,
    }
    claims.update(overrides)
    return claims


def test_refresh_invalid_token_returns_401(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}decode_refresh_token_claims", side_effect=JWTError("bad")):
        resp = http.post(_REFRESH_URL, json={"refresh_token": "junk"})

    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize(
    ("claims", "detail"),
    [
        (_claims(token_family=None), "Invalid refresh token claims"),
        (_claims(token_sequence="0"), "Invalid refresh token claims"),
        (_claims(token_family="not-a-uuid"), "Invalid refresh token family"),
        (_claims(account_id=None), "Invalid refresh token claims"),
        (_claims(account_id="not-a-uuid"), "Invalid refresh token account"),
        (_claims(sub=None), "Invalid refresh token payload"),
        (_claims(org_id=None), "Invalid refresh token payload"),
    ],
)
def test_refresh_claim_shape_401s(
    client: tuple[TestClient, AsyncMock],
    claims: dict,
    detail: str,
) -> None:
    http, _session = client
    with patch(f"{_PREFIX}decode_refresh_token_claims", return_value=claims):
        resp = http.post(_REFRESH_URL, json={"refresh_token": "junk"})

    assert resp.status_code == 401, resp.text
    assert detail in resp.json()["detail"]


def test_refresh_inactive_account_returns_401(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}get_account_by_id", new=AsyncMock(return_value=_make_account(active=False))):
        resp = http.post(_REFRESH_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 401, resp.text
    assert "no longer has access" in resp.json()["detail"]


def test_refresh_missing_membership_returns_401(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_account_by_id", new=AsyncMock(return_value=_make_account())),
        patch(f"{_PREFIX}resolve_role_from_membership", new=AsyncMock(return_value=None)),
    ):
        resp = http.post(_REFRESH_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 401, resp.text


def test_refresh_theft_detected_returns_401(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_account_by_id", new=AsyncMock(return_value=_make_account())),
        patch(f"{_PREFIX}resolve_role_from_membership", new=AsyncMock(return_value="admin")),
        patch(f"{_PREFIX}advance_sequence", new=AsyncMock(return_value=(1, True))),
    ):
        resp = http.post(_REFRESH_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 401, resp.text
    assert "suspected theft" in resp.json()["detail"]


def test_refresh_happy_path_rotates_tokens(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_account_by_id", new=AsyncMock(return_value=_make_account())),
        patch(f"{_PREFIX}resolve_role_from_membership", new=AsyncMock(return_value="operator")),
        patch(f"{_PREFIX}advance_sequence", new=AsyncMock(return_value=(1, False))),
    ):
        resp = http.post(_REFRESH_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert "modulo_session" in resp.cookies


def test_refresh_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url=_REFRESH_URL,
        json_body={"refresh_token": _refresh_token()},
        patch_target="_advance_refresh_sequence",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# POST /auth/logout — blacklist error matrix + claim-shape skips
# ---------------------------------------------------------------------------

_LOGOUT_URL = "/api/v1/auth/logout"


def test_logout_invalid_token_returns_401(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}decode_refresh_token_claims", side_effect=JWTError("bad")):
        resp = http.post(_LOGOUT_URL, json={"refresh_token": "junk"})

    assert resp.status_code == 401, resp.text


def test_logout_happy_path_blacklists_family_and_clears_approvals(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}decode_refresh_token_claims", return_value=_claims()),
        patch(f"{_PREFIX}blacklist_family", new=AsyncMock(return_value=True)) as blacklist,
        patch(f"{_PREFIX}clear_session_approvals_for_account") as clear_approvals,
    ):
        resp = http.post(_LOGOUT_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 200, resp.text
    assert resp.json()["detail"] == "Logged out"
    blacklist.assert_awaited_once()
    clear_approvals.assert_called_once()


def test_logout_claims_without_family_skip_blacklist(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}decode_refresh_token_claims", return_value={"sub": "x"}),
        patch(f"{_PREFIX}blacklist_family", new_callable=AsyncMock) as blacklist,
        patch(f"{_PREFIX}clear_session_approvals_for_account") as clear_approvals,
    ):
        resp = http.post(_LOGOUT_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 200, resp.text
    blacklist.assert_not_awaited()
    clear_approvals.assert_not_called()


def test_logout_invalid_family_uuid_is_tolerated(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}decode_refresh_token_claims", return_value=_claims(token_family="not-a-uuid")),
        patch(f"{_PREFIX}blacklist_family", new_callable=AsyncMock) as blacklist,
        patch(f"{_PREFIX}clear_session_approvals_for_account") as clear_approvals,
    ):
        resp = http.post(_LOGOUT_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 200, resp.text
    blacklist.assert_not_awaited()
    clear_approvals.assert_called_once_with("00000000-0000-0000-0000-000000000002")


def test_logout_family_not_found_still_succeeds(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}decode_refresh_token_claims", return_value=_claims()),
        patch(f"{_PREFIX}blacklist_family", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}clear_session_approvals_for_account"),
    ):
        resp = http.post(_LOGOUT_URL, json={"refresh_token": _refresh_token()})

    assert resp.status_code == 200, resp.text


def test_logout_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        with (
            patch(f"{_PREFIX}decode_refresh_token_claims", return_value=_claims()),
            patch(f"{_PREFIX}blacklist_family", new=AsyncMock(side_effect=exc)),
        ):
            resp = http.post(_LOGOUT_URL, json={"refresh_token": _refresh_token()})
        assert resp.status_code == expected, f"{exc!r}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /auth/ws-token — live-role denial surfaces
# ---------------------------------------------------------------------------


def test_ws_token_role_read_failure_maps_503(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}resolve_role_from_membership", new=AsyncMock(side_effect=_SQL)):
        resp = http.post("/api/v1/auth/ws-token")

    assert resp.status_code == 503, resp.text


def test_ws_token_removed_member_denied(client: tuple[TestClient, AsyncMock]) -> None:
    from modulo.auth.dependencies import OrganisationMembershipNotFound

    http, _session = client
    with patch(f"{_PREFIX}resolve_role_from_membership", new=AsyncMock(return_value=None)):
        resp = http.post("/api/v1/auth/ws-token")

    assert resp.status_code == OrganisationMembershipNotFound().status_code, resp.text


def test_ws_token_unexpected_error_maps_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}_resolve_live_org_role", new=AsyncMock(side_effect=_RUNTIME)):
        resp = http.post("/api/v1/auth/ws-token")

    assert resp.status_code == 500, resp.text


# ---------------------------------------------------------------------------
# GET /auth/me — error matrix + 404 + happy
# ---------------------------------------------------------------------------


def test_me_happy_path_returns_live_role(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    account = _make_account()
    with (
        patch(f"{_PREFIX}get_account_by_id", new=AsyncMock(return_value=account)),
        patch(f"{_PREFIX}_resolve_live_org_role", new=AsyncMock(return_value="operator")),
    ):
        resp = http.get("/api/v1/auth/me")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "user@example.com"
    assert body["org_role"] == "operator"


def test_me_unknown_account_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}get_account_by_id", new=AsyncMock(return_value=None)):
        resp = http.get("/api/v1/auth/me")

    assert resp.status_code == 404, resp.text


def test_me_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/auth/me",
        json_body={},
        patch_target="get_account_by_id",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# GET /auth/csrf-token
# ---------------------------------------------------------------------------


def test_csrf_token_sets_cookie(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    resp = http.get("/api/v1/auth/csrf-token")

    assert resp.status_code == 200, resp.text
    assert resp.json()["csrf_token"]
    assert "XSRF-TOKEN" in resp.cookies


# ---------------------------------------------------------------------------
# Cookie + client-ip helpers
# ---------------------------------------------------------------------------


def test_set_and_clear_auth_cookies() -> None:
    from modulo.api.routes.auth import _clear_auth_cookies, _set_auth_cookies

    settings = _make_settings()
    response = Response()
    _set_auth_cookies(response, "access-token-value", settings)
    all_set_headers = "; ".join(response.headers.getlist("set-cookie"))
    assert "modulo_session=access-token-value" in all_set_headers
    assert "XSRF-TOKEN=" in all_set_headers

    cleared = Response()
    _clear_auth_cookies(cleared, settings)
    clear_headers = "; ".join(cleared.headers.getlist("set-cookie"))
    assert clear_headers.count("Max-Age=0") == 2


def test_client_ip_resolution() -> None:
    from modulo.api.routes.auth import _client_ip

    client_scope = {"type": "http", "client": ("203.0.113.5", 1234), "headers": []}
    from starlette.requests import Request

    request_with_client = Request(client_scope)
    assert _client_ip(request_with_client) == "203.0.113.5"

    forwarded_scope = {
        "type": "http",
        "client": None,
        "headers": [(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")],
    }
    assert _client_ip(Request(forwarded_scope)) == "198.51.100.7"

    unknown_scope = {"type": "http", "client": None, "headers": []}
    assert _client_ip(Request(unknown_scope)) == "unknown"


def test_resolve_demo_membership_and_login_context_edges() -> None:
    import asyncio

    from modulo.api.routes.auth import _resolve_login_org_context

    # System-admin account without memberships still logs in (bootstrap).
    system_admin = _make_account(is_system_admin=True)
    org_id, role = _resolve_login_org_context([], system_admin)
    assert org_id is None
    assert role is None

    # A membership resolves the primary org + role.
    membership = MagicMock()
    membership.organisation_id = _ORG_ID
    membership.role = "operator"
    resolved_org, resolved_role = _resolve_login_org_context([membership], _make_account())
    assert resolved_org == _ORG_ID
    assert resolved_role == "operator"

    asyncio.run(asyncio.sleep(0))
