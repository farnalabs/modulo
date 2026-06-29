"""Unit tests mirroring BDD sso_saml.feature — SP metadata, ACS callback, JIT provisioning, group mapping, gating."""

import base64
import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.routes.sso import router as sso_router
from modulo.core.feature_flags import FreeTier, LicenseData, LicenseKeyTier
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

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

    _app.dependency_overrides[get_settings] = lambda: _saml_settings()
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _app.dependency_overrides[get_plan_context] = lambda: LicenseKeyTier(
        LicenseData(
            tier="enterprise",
            features=["sso"],
            expires_at="",
            org_id="",
            raw_payload={},
            raw_key="test-license-key",
        )
    )
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario: SP metadata endpoint returns valid XML
# ---------------------------------------------------------------------------


class TestSpMetadata:
    def test_metadata_returns_valid_xml(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/saml/metadata")

        assert resp.status_code == 200
        body = resp.text
        assert "<md:EntityDescriptor" in body
        assert "<md:SPSSODescriptor" in body
        assert "AssertionConsumerService" in body
        assert "/api/v1/auth/saml/acs" in body
        assert "HTTP-POST" in body

    def test_metadata_requires_license(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: _saml_settings(license_key="")
        _app.dependency_overrides[get_plan_context] = lambda: FreeTier()
        get_settings.cache_clear()

        resp = client.get("/api/v1/auth/saml/metadata")
        assert resp.status_code == 402


# ---------------------------------------------------------------------------
# Scenario: ACS callback creates new user via JIT provisioning
# ---------------------------------------------------------------------------


class TestAcsNewUser:
    def test_acs_provisions_new_user_and_returns_tokens(self, client: TestClient) -> None:
        encoded = _make_saml_response("newuser@example.com", "New User")

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = _SAMPLE_IDP_METADATA

            user_mock = MagicMock()
            user_mock.email = "newuser@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = user_mock

            mock_tok.return_value = {
                "access_token": "at-saml",
                "refresh_token": "rt-saml",
                "token_type": "bearer",
            }

            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "access_token=at-saml" in location
        assert "refresh_token=rt-saml" in location

        mock_jit.assert_awaited_once()
        call = mock_jit.await_args
        assert call is not None
        assert call[0][2] == "newuser@example.com"
        assert call[0][3] == "New User"

        mock_tok.assert_awaited_once()


# ---------------------------------------------------------------------------
# Scenario: Existing SAML user is logged in without duplicate
# ---------------------------------------------------------------------------


class TestAcsExistingUser:
    def test_acs_returns_tokens_for_existing_user(self, client: TestClient) -> None:
        encoded = _make_saml_response("alice@example.com", "Alice")

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = _SAMPLE_IDP_METADATA

            existing = MagicMock()
            existing.email = "alice@example.com"
            existing.id = uuid.uuid4()
            existing.organisation_id = _ORG_ID
            existing.org_role = "admin"
            existing.sso_subject = "saml:https://idp.example.com:alice@example.com"
            existing.auth_provider = "saml"
            mock_jit.return_value = existing

            mock_tok.return_value = {
                "access_token": "at-existing",
                "refresh_token": "rt-existing",
                "token_type": "bearer",
            }

            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "access_token=at-existing" in location

        mock_jit.assert_awaited_once()
        call = mock_jit.await_args
        assert call is not None
        assert call[0][2] == "alice@example.com"

        user = mock_jit.return_value
        assert user.email == "alice@example.com"
        assert user.sso_subject == "saml:https://idp.example.com:alice@example.com"
        assert user.auth_provider == "saml"


# ---------------------------------------------------------------------------
# Scenario: Invalid SAML response is rejected
# ---------------------------------------------------------------------------


class TestAcsInvalidResponse:
    def test_malformed_saml_response_rejected(self, client: TestClient) -> None:
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = _SAMPLE_IDP_METADATA
            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": base64.b64encode(b"<bad/>").decode()},
                follow_redirects=False,
            )

        assert resp.status_code == 401
        detail = resp.json().get("detail", "")
        assert "Assertion" in detail

    def test_missing_saml_response_rejected(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/saml/acs", data={}, follow_redirects=False)
        assert resp.status_code == 400
        detail = resp.json().get("detail", "")
        assert "SAMLResponse" in detail

    def test_garbage_base64_rejected(self, client: TestClient) -> None:
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = _SAMPLE_IDP_METADATA
            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": "not-base64==="},
                follow_redirects=False,
            )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scenario: IdP group claim maps to team membership
# ---------------------------------------------------------------------------


class TestAcsGroupMapping:
    def test_group_mapping_applied_for_idp_groups(self, client: TestClient) -> None:
        encoded = _make_saml_response("groupuser@example.com", "Group User", groups=["admins", "developers"])

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = _SAMPLE_IDP_METADATA

            user_mock = MagicMock()
            user_mock.email = "groupuser@example.com"
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = user_mock

            provider_mock = MagicMock()
            provider_mock.group_mappings = [
                {"idp_group": "admins", "team_id": "team-1", "team_role": "admin"},
            ]
            mock_lookup.return_value = provider_mock

            mock_tok.return_value = {
                "access_token": "at-group",
                "refresh_token": "rt-group",
                "token_type": "bearer",
            }

            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )

        assert resp.status_code == 307

        mock_jit.assert_awaited_once()
        mock_lookup.assert_awaited_once()
        mock_apply.assert_awaited_once()
        call = mock_apply.await_args
        assert call is not None
        assert call[0][2] == ["admins", "developers"]

    def test_group_mapping_skipped_when_no_provider(self, client: TestClient) -> None:
        encoded = _make_saml_response("noprov@example.com", "No Prov", groups=["admins"])

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = _SAMPLE_IDP_METADATA
            mock_jit.return_value = MagicMock()
            mock_lookup.return_value = None
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_apply.assert_not_called()

    def test_group_mapping_skipped_when_no_group_attr(self, client: TestClient) -> None:
        encoded = _make_saml_response("nogrp@example.com", "No Group")

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = _SAMPLE_IDP_METADATA
            mock_jit.return_value = MagicMock()
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )

        assert resp.status_code == 307
        mock_lookup.assert_not_called()
        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario: Enterprise gate blocks SAML on free tier
# ---------------------------------------------------------------------------


class TestEnterpriseGate:
    def test_saml_acs_blocked_without_license(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: _saml_settings(license_key="")
        _app.dependency_overrides[get_plan_context] = lambda: FreeTier()
        get_settings.cache_clear()

        resp = client.post(
            "/api/v1/auth/saml/acs",
            data={"SAMLResponse": base64.b64encode(b"ignored").decode()},
            follow_redirects=False,
        )
        assert resp.status_code == 402
        detail = resp.json().get("detail", "")
        assert "sso" in detail.lower()

    def test_saml_login_blocked_without_license(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: _saml_settings(license_key="")
        _app.dependency_overrides[get_plan_context] = lambda: FreeTier()
        get_settings.cache_clear()

        resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
        assert resp.status_code == 402


# ---------------------------------------------------------------------------
# Scenario: SAML login initiates redirect to IdP
# ---------------------------------------------------------------------------


class TestSamlLoginRedirect:
    def test_redirects_to_idp_sso_url(self, client: TestClient) -> None:
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = _SAMPLE_IDP_METADATA

            resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)

        assert resp.status_code == 307
        location = resp.headers.get("location", "")
        assert "idp.example.com" in location
        assert "SAMLRequest" in location

    def test_login_requires_license(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: _saml_settings(license_key="")
        _app.dependency_overrides[get_plan_context] = lambda: FreeTier()
        get_settings.cache_clear()

        resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
        assert resp.status_code == 402

    def test_login_raises_400_when_saml_disabled(self, client: TestClient) -> None:
        _app.dependency_overrides[get_settings] = lambda: Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=_VALID_32,
            fernet_key=_VALID_32,
            modulo_admin_password="testpass",
            modulo_license_key="test-license-key",
            modulo_csrf_enabled=False,
            modulo_saml_enabled=False,
            modulo_public_url="http://localhost:8000",
        )
        _app.dependency_overrides[get_plan_context] = lambda: LicenseKeyTier(
            LicenseData(tier="enterprise", features=["sso"], expires_at="", org_id="", raw_payload={}, raw_key="k")
        )
        get_settings.cache_clear()

        resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
        assert resp.status_code == 400
