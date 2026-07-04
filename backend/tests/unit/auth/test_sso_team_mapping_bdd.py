"""Unit tests mirroring BDD sso_team_mapping.feature — admin config, OIDC/SAML JIT, role assignment."""

import base64
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.routes import admin_sso as admin_sso_router_module
from modulo.api.routes import sso as sso_router_module
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import LicenseData, LicenseKeyTier
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PROVIDER_UUID = "00000000-0000-0000-0000-000000000010"

_SAMPLE_IDP_METADATA = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="https://idp.example.com">
  <md:IDPSSODescriptor
   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService
     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
     Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

_SAML_RESPONSE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<samlp:Response
 xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
 xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
  <saml:Assertion ID="_abc123" IssueInstant="2024-01-01T00:00:00Z">
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">
        __EMAIL__
      </saml:NameID>
    </saml:Subject>
    <saml:AttributeStatement>
      <saml:Attribute Name="email">
        <saml:AttributeValue>__EMAIL__</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="displayName">
        <saml:AttributeValue>__DISPLAY_NAME__</saml:AttributeValue>
      </saml:Attribute>
      __GROUPS_XML__
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""


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
        ]),
    )


def _saml_settings(license_key: str = "test-license-key") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=license_key,
        modulo_csrf_enabled=False,
        modulo_saml_enabled=True,
        modulo_saml_entity_id="urn:modulo:sp",
        modulo_public_url="http://localhost:8000",
        modulo_saml_idp_metadata_xml=_SAMPLE_IDP_METADATA,
    )


def _make_saml_response(email: str, display_name: str, groups: list[str] | None = None) -> str:
    groups_xml = ""
    if groups:
        values = "".join(
            f'        <saml:AttributeValue>{g}</saml:AttributeValue>'
            for g in groups
        )
        groups_xml = (
            '      <saml:Attribute Name="groups">\n'
            f"{values}\n"
            '      </saml:Attribute>'
        )
    xml = (
        _SAML_RESPONSE_XML
        .replace("__EMAIL__", email)
        .replace("__DISPLAY_NAME__", display_name)
        .replace("__GROUPS_XML__", groups_xml)
    )
    return base64.b64encode(xml.encode()).decode()


def _make_id_token(email: str, name: str, groups: list[str] | None = None, sub: str = "abc123") -> str:
    claims: dict[str, Any] = {"email": email, "name": name, "sub": sub}
    if groups:
        claims["groups"] = groups
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.signature"


def _sign_state(provider_id: str, secret_key: str = _VALID_32) -> str:
    from modulo.auth.sso import sign_state
    return sign_state(f"{provider_id}:{uuid.uuid4().hex}", secret_key)


_app = FastAPI()
_app.include_router(sso_router_module.router)
_app.include_router(admin_sso_router_module.router)


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

    async def _override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    _app.dependency_overrides[get_settings] = lambda: _oidc_settings()
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _app.dependency_overrides[get_plan_context] = lambda: LicenseKeyTier(
        LicenseData(
            tier="team",
            features=["sso"],
            expires_at="",
            org_id="",
            raw_payload={},
            raw_key="test-license-key",
        )
    )
    _app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="admin",
    )
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


@pytest.fixture()
def saml_client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock(spec=AsyncSession)

    async def _override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    _app.dependency_overrides[get_settings] = lambda: _saml_settings()
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _app.dependency_overrides[get_plan_context] = lambda: LicenseKeyTier(
        LicenseData(
            tier="team",
            features=["sso"],
            expires_at="",
            org_id="",
            raw_payload={},
            raw_key="test-license-key",
        )
    )
    _app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="admin",
    )
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario: Admin configures group mapping for an SSO provider
# ---------------------------------------------------------------------------


class TestAdminSetGroupMappings:
    URL = f"/api/v1/admin/sso/providers/{_PROVIDER_UUID}/group-mappings"

    def test_sets_group_mappings(self, client: TestClient) -> None:
        mappings = [
            {"idp_group": "engineering", "team_id": "team-1", "team_role": "operator"},
        ]
        mock_provider = MagicMock()
        mock_provider.group_mappings = mappings

        with patch("modulo.api.routes.admin_sso.set_group_mappings", new_callable=AsyncMock) as mock_set:
            mock_set.return_value = mock_provider
            resp = client.put(self.URL, json={"mappings": mappings})

        assert resp.status_code == 200
        mock_set.assert_awaited_once()
        call = mock_set.await_args
        assert call is not None
        assert call[0][2] == mappings

    def test_404_on_missing_provider(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.set_group_mappings", new_callable=AsyncMock) as mock_set:
            mock_set.return_value = None
            resp = client.put(self.URL, json={"mappings": []})

        assert resp.status_code == 404

    def test_requires_admin_role(self, client: TestClient) -> None:
        _app.dependency_overrides[get_plan_context] = lambda: LicenseKeyTier(
            LicenseData(tier="team", features=["sso"], expires_at="", org_id="", raw_payload={}, raw_key="k")
        )
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        _app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="viewer",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="viewer",
        )
        try:
            resp = client.put(self.URL, json={"mappings": []})
            assert resp.status_code == 403
        finally:
            _app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Scenario: Admin retrieves configured group mappings
# ---------------------------------------------------------------------------


class TestAdminGetGroupMappings:
    URL = f"/api/v1/admin/sso/providers/{_PROVIDER_UUID}/group-mappings"

    def test_returns_stored_mappings(self, client: TestClient) -> None:
        stored = [
            {"idp_group": "engineering", "team_id": "team-1", "team_role": "operator"},
        ]
        mock_provider = MagicMock()
        mock_provider.group_mappings = stored

        with patch("modulo.api.routes.admin_sso.get_provider", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_provider
            resp = client.get(self.URL)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["mappings"]) == 1
        assert data["mappings"][0]["idp_group"] == "engineering"

    def test_empty_mappings(self, client: TestClient) -> None:
        mock_provider = MagicMock()
        mock_provider.group_mappings = []

        with patch("modulo.api.routes.admin_sso.get_provider", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_provider
            resp = client.get(self.URL)

        assert resp.status_code == 200
        assert resp.json() == {"mappings": []}

    def test_404_on_missing_provider(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.get_provider", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            resp = client.get(self.URL)

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Scenario: Group mapping applied at OIDC JIT provisioning
# ---------------------------------------------------------------------------


class TestOidcGroupMapping:
    def test_apply_group_mappings_called_for_idp_groups(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("alice@example.com", "Alice", groups=["engineering"])
        mappings = [
            {"idp_group": "engineering", "team_id": "team-1", "team_role": "operator"},
        ]

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "alice@example.com",
                "name": "Alice",
                "sub": "abc123",
                "groups": ["engineering"],
            }

            user_mock = MagicMock()
            user_mock.email = "alice@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")

            provider_mock = MagicMock()
            provider_mock.group_mappings = mappings
            mock_lookup.return_value = provider_mock

            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_apply.assert_awaited_once()
        call = mock_apply.await_args
        assert call is not None
        assert call.args[3] == ["engineering"]

    def test_skipped_when_no_idp_groups(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("bob@example.com", "Bob")

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "bob@example.com", "name": "Bob", "sub": "bob123",
            }
            user_mock = MagicMock()
            user_mock.email = "bob@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}
            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_lookup.assert_not_called()
        mock_apply.assert_not_called()

    def test_skipped_when_no_provider_mappings(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("carol@example.com", "Carol", groups=["engineering"])

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "carol@example.com", "name": "Carol", "sub": "carol123",
                "groups": ["engineering"],
            }
            user_mock = MagicMock()
            user_mock.email = "carol@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}
            mock_lookup.return_value = None

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario: Group mapping applied at SAML JIT provisioning
# ---------------------------------------------------------------------------


class TestSamlGroupMapping:
    def test_apply_group_mappings_called_for_idp_groups(self, saml_client: TestClient) -> None:
        encoded = _make_saml_response("bob@example.com", "Bob", groups=["admins"])
        mappings = [
            {"idp_group": "admins", "team_id": "team-2", "team_role": "admin"},
        ]

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = _SAMPLE_IDP_METADATA

            user_mock = MagicMock()
            user_mock.email = "bob@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")

            provider_mock = MagicMock()
            provider_mock.group_mappings = mappings
            mock_lookup.return_value = provider_mock

            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            resp = saml_client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_apply.assert_awaited_once()
        call = mock_apply.await_args
        assert call is not None
        assert call.args[3] == ["admins"]

    def test_skipped_when_no_group_attr(self, saml_client: TestClient) -> None:
        encoded = _make_saml_response("dave@example.com", "Dave")

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = _SAMPLE_IDP_METADATA

            user_mock = MagicMock()
            user_mock.email = "dave@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            resp = saml_client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_lookup.assert_not_called()
        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario: Multiple IdP groups map to multiple teams
# ---------------------------------------------------------------------------


class TestMultipleGroupMappings:
    def test_multiple_idp_groups_create_multiple_memberships(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("charlie@example.com", "Charlie", groups=["engineering", "design"])
        mappings = [
            {"idp_group": "engineering", "team_id": "team-1", "team_role": "operator"},
            {"idp_group": "design", "team_id": "team-2", "team_role": "viewer"},
        ]

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "charlie@example.com",
                "name": "Charlie",
                "sub": "charlie123",
                "groups": ["engineering", "design"],
            }

            user_mock = MagicMock()
            user_mock.email = "charlie@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")

            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            provider_mock = MagicMock()
            provider_mock.group_mappings = mappings
            mock_lookup.return_value = provider_mock

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_apply.assert_awaited_once()
        call = mock_apply.await_args
        assert call is not None
        assert call.args[3] == ["engineering", "design"]
        assert len(call.args[4]) == 2


# ---------------------------------------------------------------------------
# Scenario: User receives the role specified in the group mapping
# ---------------------------------------------------------------------------


class TestRoleFromMapping:
    def test_user_gets_role_from_mapping(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("dave@example.com", "Dave", groups=["viewers"])
        mappings = [
            {"idp_group": "viewers", "team_id": "team-3", "team_role": "viewer"},
        ]

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "dave@example.com",
                "name": "Dave",
                "sub": "dave123",
                "groups": ["viewers"],
            }

            user_mock = MagicMock()
            user_mock.email = "dave@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")

            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            provider_mock = MagicMock()
            provider_mock.group_mappings = mappings
            mock_lookup.return_value = provider_mock

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_apply.assert_awaited_once()
        call = mock_apply.await_args
        assert call is not None
        assert call.args[4][0]["team_role"] == "viewer"


# ---------------------------------------------------------------------------
# Scenario: Unmatched IdP groups are silently ignored
# ---------------------------------------------------------------------------


class TestUnmatchedGroups:
    def test_unmatched_idp_groups_are_passed_but_unmatched(self, client: TestClient) -> None:
        signed = _sign_state("google")
        id_token = _make_id_token("eve@example.com", "Eve", groups=["unknown-group"])
        mappings = [
            {"idp_group": "engineering", "team_id": "team-1", "team_role": "operator"},
        ]

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "eve@example.com",
                "name": "Eve",
                "sub": "eve123",
                "groups": ["unknown-group"],
            }

            user_mock = MagicMock()
            user_mock.email = "eve@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")

            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            provider_mock = MagicMock()
            provider_mock.group_mappings = mappings
            mock_lookup.return_value = provider_mock

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode123&state={signed}",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_apply.assert_awaited_once()
        call = mock_apply.await_args
        assert call is not None
        assert call.args[3] == ["unknown-group"]
        assert call.args[4] == mappings
        matched = any(m["idp_group"] in call.args[3] for m in call.args[4])
        assert not matched, "Expected no matching idp_group in mappings"
