"""SSO (OIDC + SAML) unit tests: state signing, provider parsing, JIT provisioning, routes."""

import asyncio
import base64
import json
import os
import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import defusedxml.ElementTree as ET
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.routes.sso import router as sso_router
from modulo.auth.sso import (
    parse_oidc_providers,
    sign_state,
    verify_state,
)
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _override(**kwargs: str | bool) -> Settings:
    base: dict[str, str | bool] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_oidc_providers": json.dumps([
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
                "discovery_url": (
                    "https://token.actions.githubusercontent.com/"
                    ".well-known/openid-configuration"
                ),
            },
        ]),
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _set_env() -> Generator[None, None, None]:
    old = {k: os.environ.pop(k, None) for k in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY")}
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://localhost/test"
    os.environ["SECRET_KEY"] = _VALID_32
    os.environ["FERNET_KEY"] = _VALID_32
    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        get_settings.cache_clear()


_app = FastAPI()
_app.include_router(sso_router)


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock(spec=AsyncSession)

    async def _override_session() -> AsyncMock:
        yield mock_session

    _app.dependency_overrides[get_settings] = lambda: _override()
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


def _override_settings(**kwargs: str | bool) -> None:
    _app.dependency_overrides[get_settings] = lambda: _override(**kwargs)


# ---------------------------------------------------------------------------
# State signing
# ---------------------------------------------------------------------------


class TestStateSigning:
    def test_sign_and_verify(self) -> None:
        signed = sign_state("test-state", _VALID_32)
        assert ":" in signed
        result = verify_state(signed, _VALID_32)
        assert result == "test-state"

    def test_verify_tampered_state_returns_none(self) -> None:
        signed = sign_state("test-state", _VALID_32)
        tampered = signed + "x"
        assert verify_state(tampered, _VALID_32) is None

    def test_verify_wrong_key_returns_none(self) -> None:
        signed = sign_state("test-state", _VALID_32)
        assert verify_state(signed, "b" * 32) is None

    def test_verify_malformed_returns_none(self) -> None:
        assert verify_state("no-colon", _VALID_32) is None

    def test_verify_empty_returns_none(self) -> None:
        assert verify_state("", _VALID_32) is None


# ---------------------------------------------------------------------------
# OIDC provider parsing
# ---------------------------------------------------------------------------


class TestOidcProviderParsing:
    def test_parses_valid_providers(self) -> None:
        settings = _override()
        providers = parse_oidc_providers(settings)
        assert len(providers) == 2
        assert providers[0]["provider_id"] == "google"
        assert providers[1]["provider_id"] == "github"

    def test_empty_when_no_providers(self) -> None:
        settings = _override(modulo_oidc_providers="[]")
        assert parse_oidc_providers(settings) == []

    def test_empty_when_invalid_json(self) -> None:
        settings = _override(modulo_oidc_providers="not-json")
        assert parse_oidc_providers(settings) == []

    def test_skips_missing_fields(self) -> None:
        settings = _override(
            modulo_oidc_providers=json.dumps([
                {"provider_id": "ok", "client_id": "c", "client_secret": "s", "discovery_url": "u"},
                {"provider_id": "bad"},
            ])
        )
        providers = parse_oidc_providers(settings)
        assert len(providers) == 1
        assert providers[0]["provider_id"] == "ok"


# ---------------------------------------------------------------------------
# JIT provisioning
# ---------------------------------------------------------------------------


class TestJitProvisioning:
    async def test_jit_raises_if_no_org(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = AsyncMock(spec=AsyncSession)

        with patch("modulo.auth.sso.get_user_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            exec_mock = MagicMock()
            exec_mock.scalar_one_or_none.return_value = None
            session.execute.return_value = exec_mock

            with pytest.raises(RuntimeError, match="No organisation exists"):
                await jit_provision_user(
                    session, settings, "new@example.com", "New", "oidc", "google:123"
                )


# ---------------------------------------------------------------------------
# SSO providers endpoint
# ---------------------------------------------------------------------------


class TestSsoProvidersEndpoint:
    def test_returns_oidc_providers(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["oidc"]) == 2
        assert body["oidc"][0]["provider_id"] == "google"
        assert body["oidc"][1]["provider_id"] == "github"
        assert body["saml"] is False

    def test_saml_enabled_with_license(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="license-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_url="https://idp.example.com/metadata",
        )
        resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json()["saml"] is True

    def test_saml_disabled_without_license(self, client: TestClient) -> None:
        """SAML not exposed when license is absent, even if SAML config is present."""
        _override_settings(
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_url="https://idp.example.com/metadata",
        )
        resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json()["saml"] is False


# ---------------------------------------------------------------------------
# OIDC login redirect
# ---------------------------------------------------------------------------


class TestOidcLoginEndpoint:
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

    def test_400_for_unknown_provider(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/oidc/unknown/login", follow_redirects=False)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# SAML routes
# ---------------------------------------------------------------------------


class TestSamlRoutes:
    def test_saml_login_requires_license(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
        assert resp.status_code == 402

    def test_saml_acs_requires_license(self, client: TestClient) -> None:
        resp = client.post("/api/v1/auth/saml/acs", data={})
        assert resp.status_code == 402

    def test_saml_metadata_requires_license(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/saml/metadata")
        assert resp.status_code == 402

    def test_saml_login_with_license_and_no_metadata(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="license-123",
            modulo_saml_enabled=True,
        )
        resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
        assert resp.status_code == 400

    def test_saml_acs_missing_response(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="license-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_url="https://idp.example.com/metadata",
        )
        resp = client.post("/api/v1/auth/saml/acs", data={})
        assert resp.status_code == 400

    def test_saml_metadata_with_license(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="license-123",
        )
        resp = client.get("/api/v1/auth/saml/metadata")
        assert resp.status_code == 200
        assert "EntityDescriptor" in resp.text
        assert "SPSSODescriptor" in resp.text


# ---------------------------------------------------------------------------
# OIDC callback flow
# ---------------------------------------------------------------------------


class TestOidcCallbackEndpoint:
    def test_missing_code_or_state(self, client: TestClient) -> None:
        resp = client.get("/api/v1/auth/oidc/google/callback")
        assert resp.status_code == 400

    def test_invalid_state(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/auth/oidc/google/callback?code=abc&state=tampered"
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SAML IdP metadata parsing
# ---------------------------------------------------------------------------


class TestSamlMetadataParsing:
    SAMPLE_IDP_METADATA = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="https://idp.example.com">
  <md:IDPSSODescriptor
   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService
     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
     Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

    def test_saml_auth_url_uses_metadata(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(
            modulo_license_key="license-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
        )

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock)
            as mock_fetch
        ):
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA

            url, req_id = asyncio.run(
                saml_get_auth_url(
                    settings, "https://modulo.example.com/api/v1/auth/saml/acs"
                )
            )
            assert "idp.example.com" in url
            assert "SAMLRequest" in url
            assert req_id.startswith("_")

    def test_saml_acs_parses_response_xml(self) -> None:
        """Verify SAML response XML parsing extracts NameID and attributes."""
        decoded_saml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<samlp:Response'
            ' xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
            ' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '  <saml:Assertion ID="_abc123" IssueInstant="2024-01-01T00:00:00Z">'
            '    <saml:Subject>'
            '      <saml:NameID'
            '       Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">'
            "        user@example.com"
            "      </saml:NameID>"
            "    </saml:Subject>"
            "    <saml:AttributeStatement>"
            '      <saml:Attribute Name="email">'
            "        <saml:AttributeValue>user@example.com</saml:AttributeValue>"
            "      </saml:Attribute>"
            '      <saml:Attribute Name="displayName">'
            "        <saml:AttributeValue>Test User</saml:AttributeValue>"
            "      </saml:Attribute>"
            "    </saml:AttributeStatement>"
            "  </saml:Assertion>"
            "</samlp:Response>"
        )

        root = ET.fromstring(decoded_saml)
        ns = {
            "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
            "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
        }

        assertion = root.find(".//saml:Assertion", ns)
        assert assertion is not None

        subject = assertion.find(".//saml:Subject/saml:NameID", ns)
        assert subject is not None
        assert subject.text is not None
        assert subject.text.strip() == "user@example.com"

        attrs = {}
        for attr in assertion.findall(".//saml:Attribute", ns):
            name = attr.get("Name", "")
            values = [v.text.strip() for v in attr.findall("saml:AttributeValue", ns) if v.text]
            if values:
                attrs[name] = values[0]

        assert attrs.get("email") == "user@example.com"
        assert attrs.get("displayName") == "Test User"


# ---------------------------------------------------------------------------
# ID token decoding
# ---------------------------------------------------------------------------


class TestDecodeIdTokenClaims:
    def test_decodes_valid_token(self) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            b'{"email":"user@example.com","name":"Test User","sub":"abc123"}'
        ).rstrip(b"=").decode()
        sig = base64.urlsafe_b64encode(b"signature").rstrip(b"=").decode()
        id_token = f"{header}.{payload}.{sig}"

        claims = _decode_id_token_claims(id_token)
        assert claims["email"] == "user@example.com"
        assert claims["name"] == "Test User"
        assert claims["sub"] == "abc123"

    def test_returns_empty_for_malformed_token(self) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        assert _decode_id_token_claims("not-a-jwt") == {}
        assert _decode_id_token_claims("no.dots") == {}

    def test_returns_empty_on_bad_padding(self) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        id_token = "header.bad-payload.sig"
        assert _decode_id_token_claims(id_token) == {}

    def test_returns_empty_on_empty_string(self) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        assert _decode_id_token_claims("") == {}


# ---------------------------------------------------------------------------
# JIT provisioning — additional cases
# ---------------------------------------------------------------------------


class TestJitProvisioningExtended:
    async def test_creates_user_when_org_exists(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        org_id = uuid.uuid4()

        with (
            patch("modulo.auth.sso.get_user_by_email", new_callable=AsyncMock) as mock_get,
            patch("modulo.auth.sso.select") as mock_select,
        ):
            mock_get.return_value = None
            mock_org = MagicMock()
            mock_org.id = org_id
            exec_mock = MagicMock()
            exec_mock.scalar_one_or_none.return_value = mock_org
            mock_select.return_value.order_by.return_value.limit.return_value = "query"
            session.execute.return_value = exec_mock

            user = await jit_provision_user(
                session, settings, "new@example.com", "New User", "oidc", "google:456"
            )

            assert user.email == "new@example.com"
            assert user.display_name == "New User"
            assert user.auth_provider == "oidc"
            assert user.sso_subject == "google:456"
            assert user.org_role == "runner"
            session.add.assert_called_once_with(user)
            session.flush.assert_called()

    async def test_finds_existing_user_and_updates_sso(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        existing = MagicMock()
        existing.email = "existing@example.com"
        existing.sso_subject = None
        existing.auth_provider = "local"

        with patch("modulo.auth.sso.get_user_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = existing

            user = await jit_provision_user(
                session, settings, "existing@example.com", "Existing", "oidc", "google:789"
            )

            assert user is existing
            assert user.sso_subject == "google:789"
            assert user.auth_provider == "oidc"

    async def test_uses_default_org_id(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        org_id = uuid.uuid4()

        with patch("modulo.auth.sso.get_user_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            user = await jit_provision_user(
                session, settings, "user@example.com", "User", "oidc", "sub:1",
                default_org_id=org_id,
            )

            assert user.organisation_id == org_id
            # session.execute should NOT have been called to look up org
            session.execute.assert_not_called()

    async def test_raises_if_no_org_and_no_default(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = AsyncMock(spec=AsyncSession)

        with patch("modulo.auth.sso.get_user_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            exec_mock = MagicMock()
            exec_mock.scalar_one_or_none.return_value = None
            session.execute.return_value = exec_mock

            with pytest.raises(RuntimeError, match="No organisation exists"):
                await jit_provision_user(
                    session, settings, "new@example.com", "New", "oidc", "google:123"
                )


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


class TestIssueSsoTokens:
    async def test_issues_access_and_refresh_tokens(self) -> None:
        from modulo.auth.sso import issue_sso_tokens

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        user = MagicMock()
        user.id = uuid.uuid4()
        user.email = "user@example.com"
        user.organisation_id = uuid.uuid4()
        user.org_role = "runner"

        token_family = MagicMock()
        token_family.family_id = uuid.uuid4()

        with (
            patch("modulo.auth.sso.update_last_login", new_callable=AsyncMock) as mock_upd,
            patch("modulo.auth.sso.create_family", new_callable=AsyncMock) as mock_fam,
            patch("modulo.auth.sso.create_access_token", return_value="access-xyz") as mock_at,
            patch("modulo.auth.sso.create_refresh_token", return_value="refresh-xyz") as mock_rt,
        ):
            mock_fam.return_value = token_family

            result = await issue_sso_tokens(user, session, settings)

            mock_upd.assert_awaited_once_with(session, user.id)
            mock_fam.assert_awaited_once_with(session, user.id, user.organisation_id)
            mock_at.assert_called_once()
            mock_rt.assert_called_once()
            assert result["access_token"] == "access-xyz"
            assert result["refresh_token"] == "refresh-xyz"
            assert result["token_type"] == "bearer"


# ---------------------------------------------------------------------------
# OIDC helpers — edge cases
# ---------------------------------------------------------------------------


class TestOidcGetAuthorizeUrl:
    async def test_raises_for_unknown_provider(self) -> None:
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override()
        with pytest.raises(ValueError, match="not configured"):
            await oidc_get_authorize_url("nonexistent", settings, "http://localhost/callback")

    async def test_raises_when_discovery_missing_authz_endpoint(self) -> None:
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {"token_endpoint": "https://example.com/token"}

            with pytest.raises(ValueError, match="No authorization_endpoint"):
                await oidc_get_authorize_url("google", settings, "http://localhost/callback")

    async def test_returns_url_and_state(self) -> None:
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            }

            url, raw_state = await oidc_get_authorize_url(
                "google", settings, "http://localhost/callback"
            )

            assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
            assert "client_id=google-client-id" in url
            assert "response_type=code" in url
            assert len(raw_state) > 0


# ---------------------------------------------------------------------------
# OIDC callback — full flow
# ---------------------------------------------------------------------------


class TestOidcProcessCallback:
    async def test_full_success_flow(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        raw_state = "test-raw-state"

        signed = sign_state(f"google:{raw_state}", settings.secret_key)

        id_token = (
            base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(
                b'{"email":"user@example.com","name":"Test User","sub":"abc123"}'
            ).rstrip(b"=").decode()
            + "."
            + "sig"
        )

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_jit.return_value = MagicMock()
            mock_tok.return_value = {
                "access_token": "at",
                "refresh_token": "rt",
                "token_type": "bearer",
            }

            result = await oidc_process_callback(
                "auth-code",
                signed,
                settings,
                session,
                "http://localhost/callback",
            )

            assert result["access_token"] == "at"
            assert result["token_type"] == "bearer"
            mock_jit.assert_awaited_once()
            mock_tok.assert_awaited_once()

    async def test_raises_on_bad_state(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = AsyncMock(spec=AsyncSession)

        with pytest.raises(ValueError, match="CSRF"):
            await oidc_process_callback(
                "code", "tampered-state", settings, session, "http://localhost/callback"
            )

    async def test_raises_when_provider_not_found_after_state_check(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        signed = sign_state("ghost:state", settings.secret_key)

        with pytest.raises(ValueError, match="not found"):
            await oidc_process_callback(
                "code", signed, settings, session, "http://localhost/callback"
            )


# ---------------------------------------------------------------------------
# SAML helpers — edge cases
# ---------------------------------------------------------------------------


class TestSamlGetAuthUrl:
    async def test_raises_when_saml_disabled(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(modulo_saml_enabled=False)
        with pytest.raises(ValueError, match="SAML is not enabled"):
            await saml_get_auth_url(settings, "http://localhost/acs")

    async def test_raises_when_no_license(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(modulo_saml_enabled=True)
        with pytest.raises(ValueError, match="requires a license"):
            await saml_get_auth_url(settings, "http://localhost/acs")

    async def test_raises_when_no_metadata_source(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock)
            as mock_fetch
        ):
            mock_fetch.side_effect = ValueError("SAML IdP metadata not configured")
            with pytest.raises(ValueError, match="metadata not configured"):
                await saml_get_auth_url(settings, "http://localhost/acs")


class TestSamlProcessResponse:
    SAMPLE_IDP_METADATA = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="https://idp.example.com">
  <md:IDPSSODescriptor
   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService
     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
     Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

    SAML_RESPONSE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<samlp:Response
 xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
 xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
  <saml:Assertion ID="_abc123" IssueInstant="2024-01-01T00:00:00Z">
    <saml:Subject>
      <saml:NameID
       Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">
        user@example.com
      </saml:NameID>
    </saml:Subject>
    <saml:AttributeStatement>
      <saml:Attribute Name="email">
        <saml:AttributeValue>user@example.com</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="displayName">
        <saml:AttributeValue>Test User</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""

    async def test_raises_when_saml_disabled(self) -> None:
        from modulo.auth.sso import saml_process_response

        settings = _override(modulo_saml_enabled=False)
        session = AsyncMock(spec=AsyncSession)
        with pytest.raises(ValueError, match="SAML is not enabled"):
            await saml_process_response("response", settings, session)

    async def test_raises_when_no_license(self) -> None:
        from modulo.auth.sso import saml_process_response

        settings = _override(modulo_saml_enabled=True)
        session = AsyncMock(spec=AsyncSession)
        with pytest.raises(ValueError, match="requires a license"):
            await saml_process_response("response", settings, session)

    async def test_raises_when_no_assertion(self) -> None:
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
        )
        session = AsyncMock(spec=AsyncSession)

        empty_response = base64.b64encode(b"<root/>").decode()
        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock)
            as mock_fetch
        ):
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            with pytest.raises(ValueError, match="No SAML Assertion"):
                await saml_process_response(empty_response, settings, session)

    async def test_full_success_flow(self) -> None:
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
        )
        session = AsyncMock(spec=AsyncSession)

        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock)
            as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            mock_jit.return_value = MagicMock()
            mock_tok.return_value = {
                "access_token": "at-saml",
                "refresh_token": "rt-saml",
                "token_type": "bearer",
            }

            result = await saml_process_response(encoded, settings, session)

            assert result["access_token"] == "at-saml"
            mock_jit.assert_awaited_once_with(
                session, settings, "user@example.com", "Test User", "saml",
                "saml:https://idp.example.com:user@example.com",
            )
            mock_tok.assert_awaited_once()


class TestSamlFetchIdpMetadata:
    async def test_uses_inline_xml(self) -> None:
        from modulo.auth.sso import _saml_fetch_idp_metadata

        settings = _override(modulo_saml_idp_metadata_xml="<md>inline</md>")
        result = await _saml_fetch_idp_metadata(settings)
        assert result == "<md>inline</md>"

    async def test_fetches_from_url(self) -> None:
        from modulo.auth.sso import _saml_fetch_idp_metadata

        settings = _override(
            modulo_saml_idp_metadata_url="https://idp.example.com/metadata",
        )
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.text = "<md>remote</md>"
            mock_client.get.return_value = mock_resp

            result = await _saml_fetch_idp_metadata(settings)
            assert result == "<md>remote</md>"
            mock_client.get.assert_awaited_once_with(
                "https://idp.example.com/metadata", timeout=15
            )

    async def test_raises_when_not_configured(self) -> None:
        from modulo.auth.sso import _saml_fetch_idp_metadata

        settings = _override(
            modulo_saml_idp_metadata_url="",
            modulo_saml_idp_metadata_xml="",
        )
        with pytest.raises(ValueError, match="metadata not configured"):
            await _saml_fetch_idp_metadata(settings)


class TestSamlParseIdpMetadata:
    SAMPLE_METADATA = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="https://idp.example.com">
  <md:IDPSSODescriptor
   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService
     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
     Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

    def test_parses_sso_url_and_entity_id(self) -> None:
        from modulo.auth.sso import _saml_parse_idp_metadata

        sso_url, entity_id = _saml_parse_idp_metadata(self.SAMPLE_METADATA)
        assert sso_url == "https://idp.example.com/sso"
        assert entity_id == "https://idp.example.com"

    def test_raises_when_no_idp_sso_descriptor(self) -> None:
        from modulo.auth.sso import _saml_parse_idp_metadata

        xml = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="test">
  <md:SPSSODescriptor/>
</md:EntityDescriptor>"""
        with pytest.raises(ValueError, match="No IDPSSODescriptor"):
            _saml_parse_idp_metadata(xml)

    def test_falls_back_to_first_sso_service(self) -> None:
        from modulo.auth.sso import _saml_parse_idp_metadata

        xml = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="https://idp.example.com">
  <md:IDPSSODescriptor
   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService
     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
     Location="https://idp.example.com/sso-post"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""
        sso_url, _ = _saml_parse_idp_metadata(xml)
        assert sso_url == "https://idp.example.com/sso-post"


# ---------------------------------------------------------------------------
# SAML route endpoint — additional coverage
# ---------------------------------------------------------------------------


class TestSamlRoutesExtended:
    def test_saml_login_with_license_and_metadata(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml="""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="idp">
  <md:IDPSSODescriptor>
    <md:SingleSignOnService Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>""",
        )
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m:
            m.return_value = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="idp">
  <md:IDPSSODescriptor>
    <md:SingleSignOnService Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""
            resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
            assert resp.status_code == 307
            assert "idp.example.com" in resp.headers.get("location", "")

    def test_saml_acs_with_license_and_valid_response(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml="""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="idp">
  <md:IDPSSODescriptor>
    <md:SingleSignOnService Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>""",
        )

        saml_xml = """<?xml version="1.0" encoding="UTF-8"?>
<samlp:Response
 xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
 xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
  <saml:Assertion ID="_abc123" IssueInstant="2024-01-01T00:00:00Z">
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">
        user@example.com
      </saml:NameID>
    </saml:Subject>
    <saml:AttributeStatement>
      <saml:Attribute Name="email">
        <saml:AttributeValue>user@example.com</saml:AttributeValue>
      </saml:Attribute>
      <saml:Attribute Name="displayName">
        <saml:AttributeValue>Test User</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""
        encoded = base64.b64encode(saml_xml.encode()).decode()

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as m_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as m_tok,
        ):
            m.return_value = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="idp">
  <md:IDPSSODescriptor>
    <md:SingleSignOnService Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""
            m_jit.return_value = MagicMock()
            m_tok.return_value = {
                "access_token": "at-saml",
                "refresh_token": "rt-saml",
                "token_type": "bearer",
            }

            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": encoded},
                follow_redirects=False,
            )
            assert resp.status_code == 307  # RedirectResponse
            assert "access_token=at-saml" in resp.headers.get("location", "")

    def test_saml_acs_malformed_response(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml="""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="idp">
  <md:IDPSSODescriptor>
    <md:SingleSignOnService Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>""",
        )

        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m:
            m.return_value = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="idp">
  <md:IDPSSODescriptor>
    <md:SingleSignOnService Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""
            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": base64.b64encode(b"<bad/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# OIDC route — callback success
# ---------------------------------------------------------------------------


class TestOidcCallbackEndpointExtended:
    def test_success_redirects_with_tokens(self, client: TestClient) -> None:
        from modulo.auth.sso import sign_state

        settings = _override()
        raw_state = "state-xyz"
        signed = sign_state(f"google:{raw_state}", settings.secret_key)

        id_token = (
            base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(
                b'{"email":"user@example.com","name":"Test User","sub":"abc"}'
            ).rstrip(b"=").decode()
            + "."
            + "sig"
        )

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_jit.return_value = MagicMock()
            mock_tok.return_value = {
                "access_token": "at-oidc",
                "refresh_token": "rt-oidc",
                "token_type": "bearer",
            }

            resp = client.get(
                f"/api/v1/auth/oidc/google/callback?code=authcode&state={signed}",
                follow_redirects=False,
            )

            assert resp.status_code == 307
            location = resp.headers.get("location", "")
            assert "access_token=at-oidc" in location
            assert "refresh_token=rt-oidc" in location
