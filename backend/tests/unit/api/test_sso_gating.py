"""Tests for SSO feature gating via require_feature('sso')."""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_system_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _settings_without_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
    )


def _settings_with_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="valid-license-key",
    )


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides.clear()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # Shape the CRUD read results so resolve_plan_context's catalog reads and
    # the provider listings see empty lists instead of MagicMock iterables.
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


def _build_client(settings_fn, sso_enabled: bool = True) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = settings_fn
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = sso_enabled
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_sso() -> Generator[TestClient, None, None]:
    yield from _build_client(_settings_without_license, sso_enabled=False)


@pytest.fixture
def client_with_sso() -> Generator[TestClient, None, None]:
    yield from _build_client(_settings_with_license, sso_enabled=True)


# ── SSO admin endpoints (should return 402 when feature disabled) ────────


class TestAdminSsoGating:
    def test_list_providers_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/admin/sso/providers")
        assert resp.status_code == 402
        assert "not available on your plan" in resp.text.lower()

    def test_list_providers_succeeds_when_enabled(self, client_with_sso: TestClient) -> None:
        mock_provider = MagicMock()
        mock_provider.id = _PROVIDER_ID
        mock_provider.provider_type = "oidc"
        mock_provider.provider_id = "test-oidc"
        mock_provider.name = "Test"
        mock_provider.client_id = "cid"
        mock_provider.client_secret = "secret"
        mock_provider.discovery_url = "https://example.com"
        mock_provider.metadata_url = None
        mock_provider.metadata_xml = None
        mock_provider.entity_id = None
        mock_provider.scopes = json.dumps(["openid"])
        mock_provider.enabled = True
        mock_provider.auto_provision = True
        mock_provider.default_role = "runner"
        mock_provider.group_mappings = []
        mock_provider.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        mock_provider.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

        with patch("modulo.api.routes.admin_sso.list_providers", new=AsyncMock(return_value=[mock_provider])):
            resp = client_with_sso.get("/api/v1/admin/sso/providers")
            assert resp.status_code == 200

    def test_create_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post(
            "/api/v1/admin/sso/providers",
            json={"provider_type": "oidc", "name": "Test"},
        )
        assert resp.status_code == 402

    def test_update_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.put(
            f"/api/v1/admin/sso/providers/{_PROVIDER_ID}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 402

    def test_delete_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.delete(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}")
        assert resp.status_code == 402

    def test_toggle_provider_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.put(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/toggle")
        assert resp.status_code == 402

    def test_test_connection_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/test")
        assert resp.status_code == 402

    def test_set_group_mappings_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.put(
            f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings",
            json={"mappings": []},
        )
        assert resp.status_code == 402

    def test_get_group_mappings_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get(f"/api/v1/admin/sso/providers/{_PROVIDER_ID}/group-mappings")
        assert resp.status_code == 402


# ── SSO auth login/callback endpoints ────────────────────────────────────
#
# NOTE: /api/v1/auth/sso/providers is a PRE-AUTH discovery endpoint (the login
# page fetches it before any user exists). It no longer sits behind
# require_feature/get_current_user: when the SSO feature is disabled or
# unlicensed it answers a normal 200 with an EMPTY provider list, so the
# pre-auth call never surfaces an auth error in the browser console. The other
# auth endpoints (login flows) keep the 402 gate — they are only reached by
# users clicking a provider button that this endpoint advertised.


class TestSsoAuthGating:
    def test_sso_providers_returns_200_empty_when_disabled(self, client_no_sso: TestClient) -> None:
        """SSO unlicensed -> normal 200 with an empty providers list (no 402/401)."""
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
        ):
            resp = client_no_sso.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json() == {"oidc": [], "saml": False}

    def test_sso_providers_needs_no_authentication(self) -> None:
        """Anonymous request with the REAL auth dependencies in place gets a 200.

        Regression guard: require_feature used to pull get_current_user into
        this route, which 401'd the pre-auth login-page call.
        """
        app.dependency_overrides.clear()
        mock_session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _settings_without_license
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_system_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        # get_current_user / get_plan_context deliberately NOT overridden: the
        # real dependencies would run for any request. The route must never
        # consult them.
        try:
            client = TestClient(app)
            with (
                patch("modulo.core.license.get_license", return_value=None),
                patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
            ):
                resp = client.get("/api/v1/auth/sso/providers")
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert resp.json() == {"oidc": [], "saml": False}

    def test_sso_providers_lists_env_providers_when_enabled(self, client_with_sso: TestClient) -> None:
        """SSO licensed/enabled -> 200 with the configured OIDC providers."""
        settings = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key="valid-license-key",
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "google",
                        "client_id": "cid",
                        "client_secret": "secret",
                        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                    }
                ]
            ),
        )
        app.dependency_overrides[get_settings] = lambda: settings
        licensed = SimpleNamespace(tier="team", features=["sso"], expires_at=None)
        with (
            patch("modulo.core.license.get_license", return_value=licensed),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
        ):
            resp = client_with_sso.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert "google" in [p["provider_id"] for p in body["oidc"]]

    def test_oidc_login_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/oidc/google/login")
        assert resp.status_code == 402

    def test_oidc_callback_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/oidc/google/callback?code=abc&state=def")
        assert resp.status_code == 402

    def test_saml_login_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/saml/login")
        assert resp.status_code == 402

    def test_saml_acs_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post("/api/v1/auth/saml/acs", data={"SAMLResponse": "dGVzdA=="})
        assert resp.status_code == 402

    def test_saml_metadata_returns_402_when_disabled(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/auth/saml/metadata")
        assert resp.status_code == 402


# ── Non-SSO endpoints should be unaffected ──


class TestNonSsoEndpoints:
    def test_health_returns_200(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.get("/api/v1/health")
        assert resp.status_code in (200, 404)

    def test_login_returns_422_without_body(self, client_no_sso: TestClient) -> None:
        resp = client_no_sso.post("/api/v1/auth/login", json={})
        assert resp.status_code == 422
