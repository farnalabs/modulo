"""Unit tests for /api/v1/admin/sso endpoints."""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_oidc_providers=json.dumps([
            {
                "provider_id": "google",
                "client_id": "google-client-id",
                "client_secret": "google-client-secret",
                "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
            }
        ]),
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_mock_provider(**overrides: object) -> MagicMock:
    provider = MagicMock()
    provider.id = overrides.get("id", _PROVIDER_ID)
    provider.provider_type = overrides.get("provider_type", "oidc")
    provider.name = overrides.get("name", "Test OIDC Provider")
    provider.client_id = overrides.get("client_id", "test-client-id")
    provider.client_secret = overrides.get("client_secret", "test-client-secret")
    provider.discovery_url = overrides.get("discovery_url", "https://example.com/.well-known/openid-configuration")
    provider.metadata_url = overrides.get("metadata_url", None)
    provider.metadata_xml = overrides.get("metadata_xml", None)
    provider.entity_id = overrides.get("entity_id", None)
    provider.scopes = overrides.get("scopes", json.dumps(["openid", "profile", "email"]))
    provider.enabled = overrides.get("enabled", True)
    provider.auto_provision = overrides.get("auto_provision", True)
    provider.default_role = overrides.get("default_role", "runner")
    provider.group_mappings = overrides.get("group_mappings", [])
    provider.created_at = _NOW
    provider.updated_at = _NOW
    return provider


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListProviders:
    URL = "/api/v1/admin/sso/providers"

    def test_lists_providers(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider()
        with patch("modulo.api.routes.admin_sso.list_providers", new=AsyncMock(return_value=[mock_provider])):
            resp = client.get(self.URL)
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["name"] == "Test OIDC Provider"
            assert data[0]["provider_type"] == "oidc"
            assert data[0]["enabled"] is True

    def test_empty_list(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.list_providers", new=AsyncMock(return_value=[])):
            resp = client.get(self.URL)
            assert resp.status_code == 200
            assert resp.json() == []

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_requires_admin(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403


class TestCreateProvider:
    URL = "/api/v1/admin/sso/providers"

    def test_create_oidc_provider(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider()
        with patch("modulo.api.routes.admin_sso.create_provider", new=AsyncMock(return_value=mock_provider)):
            resp = client.post(self.URL, json={
                "provider_type": "oidc",
                "name": "Test OIDC",
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "discovery_url": "https://example.com/.well-known/openid-configuration",
                "scopes": ["openid", "profile"],
                "auto_provision": True,
                "default_role": "operator",
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["name"] == "Test OIDC Provider"
            assert data["provider_type"] == "oidc"

    def test_create_saml_provider(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider(provider_type="saml", name="Test SAML", metadata_url="https://idp.example.com/metadata")
        with patch("modulo.api.routes.admin_sso.create_provider", new=AsyncMock(return_value=mock_provider)):
            resp = client.post(self.URL, json={
                "provider_type": "saml",
                "name": "Test SAML",
                "metadata_url": "https://idp.example.com/metadata",
                "entity_id": "modulo",
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["provider_type"] == "saml"

    def test_validates_provider_type(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={
            "provider_type": "invalid",
            "name": "Test",
        })
        assert resp.status_code == 422

    def test_validates_default_role(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={
            "provider_type": "oidc",
            "name": "Test",
            "default_role": "admin",
        })
        assert resp.status_code == 422


class TestUpdateProvider:
    URL = "/api/v1/admin/sso/providers/00000000-0000-0000-0000-000000000010"

    def test_update_provider(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider(name="Updated Name")
        with patch("modulo.api.routes.admin_sso.update_provider", new=AsyncMock(return_value=mock_provider)):
            resp = client.put(self.URL, json={"name": "Updated Name"})
            assert resp.status_code == 200
            assert resp.json()["name"] == "Updated Name"

    def test_404_on_missing(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.update_provider", new=AsyncMock(return_value=None)):
            resp = client.put(self.URL, json={"name": "Updated Name"})
            assert resp.status_code == 404

    def test_400_on_empty_body(self, client: TestClient) -> None:
        resp = client.put(self.URL, json={})
        assert resp.status_code == 400


class TestDeleteProvider:
    URL = "/api/v1/admin/sso/providers/00000000-0000-0000-0000-000000000010"

    def test_delete_provider(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.delete_provider", new=AsyncMock(return_value=True)):
            resp = client.delete(self.URL)
            assert resp.status_code == 204

    def test_404_on_missing(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.delete_provider", new=AsyncMock(return_value=False)):
            resp = client.delete(self.URL)
            assert resp.status_code == 404


class TestToggleProvider:
    URL = "/api/v1/admin/sso/providers/00000000-0000-0000-0000-000000000010/toggle"

    def test_toggle_enabled(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider(enabled=False)
        with patch("modulo.api.routes.admin_sso.toggle_provider", new=AsyncMock(return_value=mock_provider)):
            resp = client.put(self.URL)
            assert resp.status_code == 200
            assert resp.json()["enabled"] is False

    def test_404_on_missing(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.toggle_provider", new=AsyncMock(return_value=None)):
            resp = client.put(self.URL)
            assert resp.status_code == 404


class TestTestConnection:
    URL = "/api/v1/admin/sso/providers/00000000-0000-0000-0000-000000000010/test"

    def test_oidc_success(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider()
        mock_discovery = {
            "issuer": "https://example.com",
            "authorization_endpoint": "https://example.com/auth",
            "token_endpoint": "https://example.com/token",
            "userinfo_endpoint": "https://example.com/userinfo",
            "jwks_uri": "https://example.com/jwks",
            "scopes_supported": ["openid", "profile", "email"],
        }
        with (
            patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=mock_provider)),
            patch("modulo.api.routes.admin_sso._test_oidc_connection", new=AsyncMock(return_value=MagicMock(
                success=True,
                message="Successfully connected to OIDC provider. Endpoints discovered.",
                provider_info=mock_discovery,
            ))),
        ):
            resp = client.post(self.URL)
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert "OIDC" in data["message"]

    def test_saml_success(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider(
            provider_type="saml",
            metadata_xml=(
                '<?xml version="1.0"?>'
                '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://idp.example.com">'
                '  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
                '    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="https://idp.example.com/sso"/>'
                '  </md:IDPSSODescriptor>'
                '</md:EntityDescriptor>'
            ),
        )
        with (
            patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=mock_provider)),
            patch("modulo.api.routes.admin_sso._test_saml_connection", new=AsyncMock(return_value=MagicMock(
                success=True,
                message="Successfully parsed SAML metadata.",
                provider_info={"entity_id": "https://idp.example.com", "sso_url": "https://idp.example.com/sso", "certificates": []},
            ))),
        ):
            resp = client.post(self.URL)
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True

    def test_404_on_missing(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=None)):
            resp = client.post(self.URL)
            assert resp.status_code == 404


class TestOidcConnection:
    async def test_missing_discovery_url(self) -> None:
        from modulo.api.routes.admin_sso import _test_oidc_connection
        provider = _make_mock_provider(discovery_url=None)
        result = await _test_oidc_connection(provider)
        assert result.success is False
        assert "Discovery URL" in result.message

    async def test_discovery_fetch_failure(self) -> None:
        from modulo.api.routes.admin_sso import _test_oidc_connection
        provider = _make_mock_provider()
        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=Exception("Connection refused"))):
            result = await _test_oidc_connection(provider)
            assert result.success is False

    async def test_successful_discovery(self) -> None:
        from modulo.api.routes.admin_sso import _test_oidc_connection
        provider = _make_mock_provider()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "issuer": "https://example.com",
            "authorization_endpoint": "https://example.com/auth",
            "token_endpoint": "https://example.com/token",
        })
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            result = await _test_oidc_connection(provider)
            assert result.success is True
            assert result.provider_info is not None
            assert result.provider_info["issuer"] == "https://example.com"


class TestSamlConnection:
    SAML_METADATA = (
        '<?xml version="1.0"?>'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://idp.example.com">'
        '  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        '    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="https://idp.example.com/sso"/>'
        '    <md:KeyDescriptor use="signing">'
        '      <md:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
        '        <ds:X509Data>'
        '          <ds:X509Certificate>MIIDazCCAlMCFQCu8R7F9ABC123def456ghi789jkl012</ds:X509Certificate>'
        '        </ds:X509Data>'
        '      </md:KeyInfo>'
        '    </md:KeyDescriptor>'
        '  </md:IDPSSODescriptor>'
        '</md:EntityDescriptor>'
    )

    async def test_missing_metadata(self) -> None:
        from modulo.api.routes.admin_sso import _test_saml_connection
        provider = _make_mock_provider(provider_type="saml", metadata_url=None, metadata_xml=None)
        result = await _test_saml_connection(provider)
        assert result.success is False
        assert "Metadata" in result.message

    async def test_successful_parse(self) -> None:
        from modulo.api.routes.admin_sso import _test_saml_connection
        provider = _make_mock_provider(provider_type="saml", metadata_xml=self.SAML_METADATA)
        result = await _test_saml_connection(provider)
        assert result.success is True
        assert result.provider_info is not None
        assert result.provider_info["entity_id"] == "https://idp.example.com"
        assert result.provider_info["sso_url"] == "https://idp.example.com/sso"

    async def test_missing_idp_descriptor(self) -> None:
        from modulo.api.routes.admin_sso import _test_saml_connection
        bad_xml = (
            '<?xml version="1.0"?>'
            '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="test"/>'
        )
        provider = _make_mock_provider(provider_type="saml", metadata_xml=bad_xml)
        result = await _test_saml_connection(provider)
        assert result.success is False
        assert "IDPSSODescriptor" in result.message

    async def test_invalid_xml(self) -> None:
        from modulo.api.routes.admin_sso import _test_saml_connection
        provider = _make_mock_provider(provider_type="saml", metadata_xml="not xml at all")
        result = await _test_saml_connection(provider)
        assert result.success is False


class TestEnvVarSeeding:
    def _make_factory_mock(self, session_mock):
        """Build a mock for get_or_create_session_factory return value.

        The factory is called (factory()) and the result is used as an
        async context manager (async with factory() as session).
        """
        factory_mock = MagicMock()
        factory_ctx = MagicMock()
        factory_ctx.__aenter__ = AsyncMock(return_value=session_mock)
        factory_ctx.__aexit__ = AsyncMock(return_value=False)
        factory_mock.return_value = factory_ctx
        return factory_mock

    async def test_seeds_from_env_var(self) -> None:
        from modulo.api.main import _seed_sso_providers
        settings = _make_settings()
        mock_session = _make_mock_session()
        mock_session.execute.return_value = (
            MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.api.dependencies.get_or_create_session_factory", return_value=self._make_factory_mock(mock_session)),
        ):
            await _seed_sso_providers(settings)

            call_args = [call for call in mock_session.add.call_args_list]
            assert len(call_args) >= 1
            added = call_args[0][0][0]
            assert added.provider_type == "oidc"
            assert added.name == "google"

    async def test_skips_if_providers_exist(self) -> None:
        from modulo.api.main import _seed_sso_providers
        settings = _make_settings()
        mock_session = _make_mock_session()
        mock_session.execute.return_value = (
            MagicMock(scalar_one_or_none=MagicMock(return_value=object()))
        )

        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.api.dependencies.get_or_create_session_factory", return_value=self._make_factory_mock(mock_session)),
        ):
            await _seed_sso_providers(settings)
            mock_session.add.assert_not_called()

    async def test_skips_if_empty_env_var(self) -> None:
        from modulo.api.main import _seed_sso_providers
        settings = _make_settings()
        settings.modulo_oidc_providers = "[]"

        await _seed_sso_providers(settings)


class TestSetGroupMappings:
    URL = "/api/v1/admin/sso/providers/00000000-0000-0000-0000-000000000010/group-mappings"

    def test_set_mappings(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider(group_mappings=[
            {"idp_group": "engineering", "team_id": "00000000-0000-0000-0000-000000000020", "team_role": "operator"},
            {"idp_group": "viewers", "team_id": "00000000-0000-0000-0000-000000000030", "team_role": "viewer"},
        ])
        with patch("modulo.api.routes.admin_sso.set_group_mappings", new=AsyncMock(return_value=mock_provider)):
            resp = client.put(self.URL, json={
                "mappings": [
                    {"idp_group": "engineering", "team_id": "00000000-0000-0000-0000-000000000020", "team_role": "operator"},
                    {"idp_group": "viewers", "team_id": "00000000-0000-0000-0000-000000000030", "team_role": "viewer"},
                ],
            })
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["mappings"]) == 2
            assert data["mappings"][0]["idp_group"] == "engineering"
            assert data["mappings"][0]["team_role"] == "operator"

    def test_404_on_missing(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.set_group_mappings", new=AsyncMock(return_value=None)):
            resp = client.put(self.URL, json={"mappings": []})
            assert resp.status_code == 404

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put(self.URL, json={"mappings": []})
        assert resp.status_code in (401, 403)

    def test_requires_admin(self, operator_client: TestClient) -> None:
        resp = operator_client.put(self.URL, json={"mappings": []})
        assert resp.status_code == 403


class TestGetGroupMappings:
    URL = "/api/v1/admin/sso/providers/00000000-0000-0000-0000-000000000010/group-mappings"

    def test_get_mappings(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider(group_mappings=[
            {"idp_group": "engineering", "team_id": "00000000-0000-0000-0000-000000000020", "team_role": "operator"},
        ])
        with patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=mock_provider)):
            resp = client.get(self.URL)
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["mappings"]) == 1
            assert data["mappings"][0]["idp_group"] == "engineering"

    def test_empty_mappings(self, client: TestClient) -> None:
        mock_provider = _make_mock_provider(group_mappings=[])
        with patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=mock_provider)):
            resp = client.get(self.URL)
            assert resp.status_code == 200
            assert resp.json() == {"mappings": []}

    def test_404_on_missing(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=None)):
            resp = client.get(self.URL)
            assert resp.status_code == 404

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_requires_admin(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403


class TestApplyGroupMappings:
    async def test_adds_new_memberships(self) -> None:
        from modulo.auth.sso import apply_group_mappings

        session = _make_mock_session()
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        user = MagicMock()
        user.id = _USER_ID
        user.organisation_id = _ORG_ID

        mappings = [
            {"idp_group": "engineering", "team_id": "00000000-0000-0000-0000-000000000020", "team_role": "operator"},
        ]
        await apply_group_mappings(session, user, ["engineering", "design"], mappings)

        add_calls = [c for c in session.add.call_args_list if c[0][0].__class__.__name__ == "TeamMembership"]
        assert len(add_calls) == 1

    async def test_skips_non_matching_groups(self) -> None:
        from modulo.auth.sso import apply_group_mappings

        session = _make_mock_session()

        user = MagicMock()
        user.id = _USER_ID
        user.organisation_id = _ORG_ID

        mappings = [
            {"idp_group": "engineering", "team_id": "00000000-0000-0000-0000-000000000020", "team_role": "operator"},
        ]
        await apply_group_mappings(session, user, ["design"], mappings)

        add_calls = [c for c in session.add.call_args_list if c[0][0].__class__.__name__ == "TeamMembership"]
        assert len(add_calls) == 0

    async def test_skips_empty_mappings(self) -> None:
        from modulo.auth.sso import apply_group_mappings

        session = _make_mock_session()

        user = MagicMock()
        user.id = _USER_ID
        user.organisation_id = _ORG_ID

        await apply_group_mappings(session, user, ["engineering"], [])

        add_calls = [c for c in session.add.call_args_list if c[0][0].__class__.__name__ == "TeamMembership"]
        assert len(add_calls) == 0

    async def test_updates_existing_membership_role(self) -> None:
        from modulo.auth.sso import apply_group_mappings

        session = _make_mock_session()
        existing = MagicMock()
        existing.id = _USER_ID
        existing.role = "viewer"
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))

        user = MagicMock()
        user.id = _USER_ID
        user.organisation_id = _ORG_ID

        mappings = [
            {"idp_group": "engineering", "team_id": "00000000-0000-0000-0000-000000000020", "team_role": "operator"},
        ]
        await apply_group_mappings(session, user, ["engineering"], mappings)

        assert existing.role == "operator"
