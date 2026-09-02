"""Step definitions for SSO OIDC integration — login redirect, callback, JIT provisioning, gating."""

import base64
import contextlib
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import get_plan_context
from modulo.core.feature_flags import CommunityTier, LicenseData, LicenseKeyTier
from modulo.settings import Settings, get_settings

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/auth/sso_oidc.feature")

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _oidc_settings(license_key: str = "test-license-key") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=license_key,
        modulo_csrf_enabled=False,
        modulo_oidc_providers=json.dumps(
            [
                {
                    "provider_id": "google",
                    "client_id": "google-client-id",
                    "client_secret": "google-client-secret",
                    "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                },
                {
                    "provider_id": "github",
                    "client_id": "github-client-id",
                    "client_secret": "github-client-secret",
                    "discovery_url": "https://token.actions.githubusercontent.com/.well-known/openid-configuration",
                },
            ]
        ),
    )


def _make_id_token(email: str, name: str, sub: str = "abc123") -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"email": email, "name": name, "sub": sub}).encode()).rstrip(b"=").decode()
    )
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.signature"


def _setup_oidc_client(license_key: str = "test-license-key") -> None:
    from modulo.api.main import app as _app

    if not license_key:
        _plan = CommunityTier()
    else:
        _plan = LicenseKeyTier(
            LicenseData(
                tier="team",
                features=["sso"],
                expires_at="",
                org_id="",
                raw_payload={},
                raw_key=license_key,
            )
        )

    _app.dependency_overrides[get_settings] = lambda: _oidc_settings(license_key)
    _app.dependency_overrides[get_plan_context] = lambda: _plan
    get_settings.cache_clear()


def _sign_state(provider_id: str, settings_override: Settings) -> str:
    from modulo.auth.sso import sign_state

    raw_state = f"{provider_id}:{uuid.uuid4().hex}"
    return sign_state(raw_state, settings_override.secret_key)


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


# ── Background ────────────────────────────────────────────────────────────


@given(parsers.parse('OIDC providers "{p1}" and "{p2}" are configured'))
def oidc_providers_configured(p1: str, p2: str, ctx: dict[str, Any]) -> None:
    ctx["providers"] = [p1, p2]
    _setup_oidc_client()


# ── License gating ────────────────────────────────────────────────────────


@given("I do not have a Team license")
def no_team_license(ctx: dict[str, Any]) -> None:
    ctx["license_key"] = ""


# ── OIDC login ────────────────────────────────────────────────────────────


@when(parsers.parse('I initiate OIDC login with "{provider}"'))
def initiate_oidc_login(provider: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_oidc_client(ctx.get("license_key", "test-license-key"))
    with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
        mock_disc.return_value = {
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
        }
        resp = client.get(f"/api/v1/auth/oidc/{provider}/login", follow_redirects=False)
        _store_response(request, ctx, resp)


# ── OIDC callback ─────────────────────────────────────────────────────────


@when("the OIDC callback returns a valid authorization code and state")
def callback_valid(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_oidc_client()
    settings = _oidc_settings()
    signed = _sign_state("google", settings)
    email = ctx.get("expected_email", "user@example.com")
    name = ctx.get("expected_name", "Test User")
    id_token = _make_id_token(email, name)

    with (
        patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
        patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
        patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
        patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
    ):
        mock_disc.return_value = {
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            "issuer": "https://accounts.google.com",
        }
        mock_ex.return_value = {"id_token": id_token}
        mock_verify.return_value = {
            "email": email,
            "name": name,
            "sub": "abc123",
            "groups": ctx.get("idp_groups", []),
        }

        if ctx.get("is_new_user", True):
            user_mock = MagicMock()
            user_mock.email = email
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")
        else:
            existing = MagicMock()
            existing.email = email
            existing.id = uuid.uuid4()
            existing.organisation_id = _ORG_ID
            existing.org_role = "admin"
            existing.sso_subject = "google:existing"
            existing.auth_provider = "oidc"
            mock_jit.return_value = (existing, _ORG_ID, "admin")

        mock_tok.return_value = {
            "access_token": "at-oidc-test",
            "refresh_token": "rt-oidc-test",
            "token_type": "bearer",
        }

        resp = client.get(
            f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
            follow_redirects=False,
        )
        _store_response(request, ctx, resp)
        ctx["mock_jit"] = mock_jit


@when("the OIDC callback returns a valid code with a tampered state")
def callback_tampered_state(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_oidc_client()
    settings = _oidc_settings()
    signed = _sign_state("google", settings)
    tampered = signed + "x"

    with (
        patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
    ):
        mock_disc.return_value = {"token_endpoint": "https://oauth2.googleapis.com/token"}
        mock_ex.return_value = {"id_token": _make_id_token("user@example.com", "Test")}
        resp = client.get(
            f"/api/v1/auth/oidc/google/callback?code=authcode123&state={tampered}",
            follow_redirects=False,
        )
        _store_response(request, ctx, resp)


@when("the OIDC callback returns without code or state")
def callback_missing_params(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_oidc_client()
    resp = client.get("/api/v1/auth/oidc/google/callback", follow_redirects=False)
    _store_response(request, ctx, resp)


# ── Given: user state for JIT ─────────────────────────────────────────────


@given(parsers.parse('a first-time OIDC user with email "{email}"'))
def first_time_user(email: str, ctx: dict[str, Any]) -> None:
    ctx["expected_email"] = email
    ctx["expected_name"] = email.split("@", maxsplit=1)[0]
    ctx["is_new_user"] = True


@given(parsers.parse('an existing OIDC user with email "{email}"'))
def existing_oidc_user(email: str, ctx: dict[str, Any]) -> None:
    ctx["expected_email"] = email
    ctx["expected_name"] = email.split("@", maxsplit=1)[0]
    ctx["is_new_user"] = False


@given("a valid OIDC login flow")
def valid_login_flow(ctx: dict[str, Any]) -> None:
    ctx["expected_email"] = "user@example.com"
    ctx["is_new_user"] = True


# ── Then: assertions ──────────────────────────────────────────────────────


@then("I am redirected to the OIDC provider")
def redirected_to_provider(request: Any) -> None:
    resp = request.node._resp
    assert resp.status_code == 307, f"Expected 307, got {resp.status_code}"
    location = resp.headers.get("location", "")
    assert location.startswith("https://"), f"Expected absolute URL, got {location}"
    assert "client_id=" in location, f"Missing client_id in redirect: {location}"
    assert "response_type=code" in location, f"Missing response_type in redirect: {location}"


@then("the redirect URL contains the OIDC authorization endpoint")
def redirect_has_auth_endpoint(request: Any) -> None:
    resp = request.node._resp
    location = resp.headers.get("location", "")
    assert "accounts.google.com" in location, f"Expected Google auth endpoint in redirect, got {location}"


@then("the redirect URL contains access and refresh tokens")
def redirect_has_tokens(request: Any) -> None:
    resp = request.node._resp
    location = resp.headers.get("location", "")
    assert "access_token=" in location, f"Missing access_token in redirect: {location}"
    assert "refresh_token=" in location, f"Missing refresh_token in redirect: {location}"


@then("a new user account was provisioned")
def new_user_provisioned(ctx: dict[str, Any], request: Any) -> None:
    mock_jit = ctx.get("mock_jit")
    assert mock_jit is not None, "No mock_jit reference found in context"
    mock_jit.assert_awaited_once()
    call_kwargs = mock_jit.await_args[1] if mock_jit.await_args else {}
    email_arg = mock_jit.call_args[0][2] if mock_jit.call_args else call_kwargs.get("email", "")
    assert email_arg == ctx.get("expected_email", ""), f"Expected JIT for {ctx.get('expected_email')}, got {email_arg}"


@then("no duplicate account was created")
def no_duplicate_account(ctx: dict[str, Any]) -> None:
    mock_jit = ctx.get("mock_jit")
    assert mock_jit is not None, "No mock_jit reference found in context"
    mock_jit.assert_awaited_once()
    returned = mock_jit.return_value
    account = returned[0] if isinstance(returned, tuple) else returned
    assert account.email == ctx.get("expected_email", ""), (
        f"Expected returning user {ctx.get('expected_email')}, got {account.email}"
    )


@then(parsers.parse('the error detail mentions "{text}"'))
def error_detail_mentions(text: str, request: Any) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "")
    assert text.lower() in detail.lower(), f"Expected detail to mention '{text}', got '{detail}'"
