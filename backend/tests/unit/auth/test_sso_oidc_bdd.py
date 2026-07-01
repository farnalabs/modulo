"""Unit tests mirroring BDD sso_oidc.feature scenarios — OIDC login, callback, JIT provisioning, gating."""

import base64
import json
import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.routes.sso import router as sso_router
from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry
from modulo.settings import Settings, get_settings

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
        modulo_public_url="http://localhost:8000",
        modulo_oidc_providers=json.dumps([
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
        ]),
    )


def _make_id_token(email: str, name: str, sub: str = "abc123") -> str:
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps({"email": email, "name": name, "sub": sub}).encode()
    ).rstrip(b"=").decode()
    return f"{header_b64}.{payload_b64}.signature"


def _sign_state(provider_id: str, secret_key: str = _VALID_32) -> str:
    from modulo.auth.sso import sign_state

    return sign_state(f"{provider_id}:{uuid.uuid4().hex}", secret_key)


_app = FastAPI()
_app.include_router(sso_router)


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock(spec=AsyncSession)

    async def _override_session() -> AsyncMock:
        yield mock_session

    _app.dependency_overrides[get_settings] = lambda: _oidc_settings()
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _app.dependency_overrides[get_plan_context] = lambda: DbPlanContext(FeatureFlagRegistry(current_tier="team"))
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario: OIDC login initiates redirect to provider
# ---------------------------------------------------------------------------


class TestOidcLoginRedirect:
    def test_redirects_to_provider(self, client: TestClient) -> None:
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
            }
            resp = client.get("/api/v1/auth/oidc/google/login", follow_redirects=False)

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "accounts.google.com" in location
        assert "client_id=google-client-id" in location
        assert "response_type=code" in location
        assert "scope=openid+email+profile" in location

    def test_unknown_provider_returns_400(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/oidc/unknown/login", follow_redirects=False)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Scenario: Callback creates new user via JIT provisioning
# ---------------------------------------------------------------------------


class TestCallbackNewUser:
    def test_callback_provisions_new_user_and_returns_tokens(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("newuser@example.com", "New User")

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {"token_endpoint": "https://oauth2.googleapis.com/token"}
            mock_ex.return_value = {"id_token": id_token}

            user_mock = MagicMock()
            user_mock.email = "newuser@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")

            mock_tok.return_value = {
                "access_token": "at-test",
                "refresh_token": "rt-test",
                "token_type": "bearer",
            }

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "access_token=at-test" in location
        assert "refresh_token=rt-test" in location

        mock_jit.assert_awaited_once()
        call = mock_jit.await_args
        assert call is not None
        assert call[0][2] == "newuser@example.com"
        assert call[0][3] == "New User"

        mock_tok.assert_awaited_once()


# ---------------------------------------------------------------------------
# Scenario: Returning OIDC user is logged in without duplicate
# ---------------------------------------------------------------------------


class TestCallbackExistingUser:
    def test_callback_returns_tokens_for_existing_user(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("alice@example.com", "Alice")

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {"token_endpoint": "https://oauth2.googleapis.com/token"}
            mock_ex.return_value = {"id_token": id_token}

            existing = MagicMock()
            existing.email = "alice@example.com"
            existing.id = uuid.uuid4()
            existing.organisation_id = _ORG_ID
            existing.org_role = "admin"
            existing.sso_subject = "google:existing"
            existing.auth_provider = "oidc"
            mock_jit.return_value = (existing, _ORG_ID, "admin")

            mock_tok.return_value = {
                "access_token": "at-existing",
                "refresh_token": "rt-existing",
                "token_type": "bearer",
            }

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "access_token=at-existing" in location

        mock_jit.assert_awaited_once()
        call = mock_jit.await_args
        assert call is not None
        assert call[0][2] == "alice@example.com"

        account = mock_jit.return_value[0]
        assert account.email == "alice@example.com"
        assert account.sso_subject == "google:existing"
        assert account.auth_provider == "oidc"


# ---------------------------------------------------------------------------
# Scenario: State parameter guards against CSRF
# ---------------------------------------------------------------------------


class TestCallbackStateValidation:
    def test_tampered_state_rejected(self, client: TestClient) -> None:
        signed = _sign_state("google")
        tampered = signed + "x"

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock),
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock),
        ):
            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={tampered}",
                follow_redirects=False,
            )

        assert resp.status_code == 401
        detail = resp.json().get("detail", "")
        assert "CSRF" in detail

    def test_missing_state_returns_400(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/auth/oidc/google/callback?code=authcode123",
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_missing_code_and_state_returns_400(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/oidc/google/callback", follow_redirects=False)
        assert resp.status_code == 400
        detail = resp.json().get("detail", "")
        assert "code" in detail.lower() or "state" in detail.lower()


# ---------------------------------------------------------------------------
# Scenario: Enterprise gate blocks OIDC on free tier
# ---------------------------------------------------------------------------


class TestEnterpriseGate:
    def test_oidc_login_blocked_without_license(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: _oidc_settings(license_key="")
        _app.dependency_overrides[get_plan_context] = lambda: DbPlanContext(
            FeatureFlagRegistry(current_tier="community")
        )
        get_settings.cache_clear()

        resp = client.get("/api/v1/auth/oidc/google/login", follow_redirects=False)
        assert resp.status_code == 402
        body = resp.json()
        assert "sso" in body.get("detail", {}).get("detail", "").lower()

    def test_oidc_callback_blocked_without_license(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: _oidc_settings(license_key="")
        _app.dependency_overrides[get_plan_context] = lambda: DbPlanContext(
            FeatureFlagRegistry(current_tier="community")
        )
        get_settings.cache_clear()

        resp = client.get("/api/v1/auth/oidc/google/callback?code=c&state=s", follow_redirects=False)
        assert resp.status_code == 402
        body = resp.json()
        assert "sso" in body.get("detail", {}).get("detail", "").lower()

    def test_sso_providers_blocked_without_license(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: _oidc_settings(license_key="")
        _app.dependency_overrides[get_plan_context] = lambda: DbPlanContext(
            FeatureFlagRegistry(current_tier="community")
        )
        get_settings.cache_clear()

        resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 402


# ---------------------------------------------------------------------------
# Scenario: SSO providers list with license
# ---------------------------------------------------------------------------


class TestSsoProviders:
    def test_returns_configured_oidc_providers(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert "oidc" in body
        assert len(body["oidc"]) == 2
        provider_ids = [p["provider_id"] for p in body["oidc"]]
        assert "google" in provider_ids
        assert "github" in provider_ids
        assert isinstance(body["saml"], bool)
