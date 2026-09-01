"""SSO (OIDC + SAML) unit tests: state signing, provider parsing, JIT provisioning, routes."""

import base64
import json
import uuid
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import defusedxml.ElementTree as ElementTree
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_system_db_session
from modulo.api.routes.sso import router as sso_router
from modulo.auth.sso import (
    parse_oidc_providers,
    sign_state,
    verify_state,
)
from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _override(**kwargs: str | bool) -> Settings:
    base: dict[str, str | bool] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_license_key": "test-license",
        "modulo_oidc_providers": json.dumps(
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
                    "discovery_url": ("https://token.actions.githubusercontent.com/.well-known/openid-configuration"),
                },
            ]
        ),
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _VALID_32)
    get_settings.cache_clear()


_app = FastAPI()
_app.include_router(sso_router)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _mock_session()

    async def _override_session() -> AsyncMock:
        yield mock_session

    _app.dependency_overrides[get_settings] = lambda: _override()
    _app.dependency_overrides[get_db_session] = _override_session
    _app.dependency_overrides[get_system_db_session] = _override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _app.dependency_overrides[get_plan_context] = lambda: DbPlanContext(FeatureFlagRegistry(current_tier="team"))
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


def _override_settings(**kwargs: str | bool) -> None:
    FeatureFlagRegistry._overrides.clear()
    _app.dependency_overrides[get_settings] = lambda: _override(**kwargs)
    settings = _override(**kwargs)
    _app.dependency_overrides[get_plan_context] = lambda: DbPlanContext(
        FeatureFlagRegistry(current_tier="team" if settings.modulo_license_key else "community")
    )


def _mock_session(scalar: object = None) -> AsyncMock:
    """Build an AsyncMock session whose DB lookups return ``scalar`` (default None).

    Returning None from scalar_one_or_none preserves the env-var fallback path
    in the SSO runtime helpers for the existing tests.
    """
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


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
        assert not parse_oidc_providers(settings)

    def test_empty_when_invalid_json(self) -> None:
        settings = _override(modulo_oidc_providers="not-json")
        assert not parse_oidc_providers(settings)

    def test_skips_non_object_entry(self) -> None:
        settings = _override(modulo_oidc_providers=json.dumps(["invalid-provider"]))
        assert not parse_oidc_providers(settings)

    def test_skips_missing_fields(self) -> None:
        settings = _override(
            modulo_oidc_providers=json.dumps(
                [
                    {"provider_id": "ok", "client_id": "c", "client_secret": "s", "discovery_url": "u"},
                    {"provider_id": "bad"},
                ]
            )
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
        session = _mock_session()

        with patch("modulo.auth.sso.get_account_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            exec_mock = MagicMock()
            exec_mock.scalar_one_or_none.return_value = None
            session.execute.return_value = exec_mock

            with pytest.raises(RuntimeError, match="No organisation exists"):
                await jit_provision_user(session, settings, "new@example.com", "New", "oidc", "google:123")


# ---------------------------------------------------------------------------
# SSO providers endpoint
# ---------------------------------------------------------------------------


class TestSsoProvidersEndpoint:
    """The /api/v1/auth/sso/providers discovery endpoint (pre-auth, login page).

    Plan resolution is anonymous (no user dependency): SSO enabled via a
    licensed tier is simulated by stubbing the in-memory license store.
    """

    _TEAM_SSO_LICENSE = SimpleNamespace(tier="team", features=["sso"], expires_at=None)

    def test_returns_oidc_providers(self, client: TestClient) -> None:
        with (
            patch("modulo.core.license.get_license", return_value=self._TEAM_SSO_LICENSE),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
        ):
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
        with (
            patch("modulo.core.license.get_license", return_value=self._TEAM_SSO_LICENSE),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
        ):
            resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json()["saml"] is True

    def test_sso_providers_disabled_returns_200_empty(self, client: TestClient) -> None:
        """Unlicensed/feature-disabled -> normal 200 with an EMPTY providers list.

        The endpoint is fetched by the login page BEFORE authentication, so it
        must never raise an auth/plan error (the old 402 behaviour put a console
        error on every login).
        """
        _override_settings(modulo_license_key="", modulo_saml_enabled=True)
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
        ):
            resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json() == {"oidc": [], "saml": False}


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

    @pytest.mark.asyncio
    async def test_saml_auth_url_uses_metadata(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(
            modulo_license_key="license-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
        )

        session = _mock_session()
        with (
            patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock) as mock_db,
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_db.return_value = None
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA

            url, _req_id = await saml_get_auth_url(
                settings, "https://modulo.example.com/api/v1/auth/saml/acs", session, session
            )
            assert "idp.example.com" in url
            assert "SAMLRequest" in url

    def test_saml_acs_parses_response_xml(self) -> None:
        """Verify SAML response XML parsing extracts NameID and attributes."""
        decoded_saml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<samlp:Response"
            ' xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
            ' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
            '  <saml:Assertion ID="_abc123" IssueInstant="2024-01-01T00:00:00Z">'
            "    <saml:Subject>"
            "      <saml:NameID"
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

        root = ElementTree.fromstring(decoded_saml)
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
        payload = (
            base64.urlsafe_b64encode(b'{"email":"user@example.com","name":"Test User","sub":"abc123"}')
            .rstrip(b"=")
            .decode()
        )
        sig = base64.urlsafe_b64encode(b"signature").rstrip(b"=").decode()
        id_token = f"{header}.{payload}.{sig}"

        claims = _decode_id_token_claims(id_token)
        assert claims["email"] == "user@example.com"
        assert claims["name"] == "Test User"
        assert claims["sub"] == "abc123"

    def test_returns_empty_for_malformed_token(self) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        assert not _decode_id_token_claims("not-a-jwt")
        assert not _decode_id_token_claims("no.dots")

    def test_returns_empty_on_bad_padding(self) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        id_token = "header.bad-payload.sig"
        assert not _decode_id_token_claims(id_token)

    def test_returns_empty_on_empty_string(self) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        assert not _decode_id_token_claims("")

    @pytest.mark.parametrize("payload", [[], "claims", None])
    def test_returns_empty_when_payload_is_not_an_object(self, payload: object) -> None:
        from modulo.auth.sso import _decode_id_token_claims

        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

        assert not _decode_id_token_claims(f"header.{encoded}.signature")


class TestOidcJsonResponseShapes:
    @pytest.mark.parametrize("payload", [[], "discovery", None])
    async def test_discovery_rejects_non_object_json(self, payload: object) -> None:
        from modulo.auth.sso import _fetch_discovery

        response = MagicMock()
        response.json.return_value = payload
        client = AsyncMock()
        client.get.return_value = response

        with patch("modulo.auth.sso.httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client
            with pytest.raises(ValueError, match="OIDC discovery document must be a JSON object"):
                await _fetch_discovery("https://issuer.example/.well-known/openid-configuration")

    async def test_discovery_accepts_object_json(self) -> None:
        from modulo.auth.sso import _fetch_discovery

        payload = {"authorization_endpoint": "https://issuer.example/authorize"}
        response = MagicMock()
        response.json.return_value = payload
        client = AsyncMock()
        client.get.return_value = response

        with patch("modulo.auth.sso.httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client
            assert await _fetch_discovery("https://issuer.example/discovery") == payload

    @pytest.mark.parametrize("payload", [[], "token", None])
    async def test_token_exchange_rejects_non_object_json(self, payload: object) -> None:
        from modulo.auth.sso import _exchange_code

        response = MagicMock()
        response.json.return_value = payload
        client = AsyncMock()
        client.post.return_value = response

        with patch("modulo.auth.sso.httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client
            with pytest.raises(ValueError, match="OIDC token response must be a JSON object"):
                await _exchange_code("https://issuer.example/token", "client", "secret", "code", "callback")

    async def test_token_exchange_accepts_object_json(self) -> None:
        from modulo.auth.sso import _exchange_code

        payload = {"id_token": "header.payload.signature"}
        response = MagicMock()
        response.json.return_value = payload
        client = AsyncMock()
        client.post.return_value = response

        with patch("modulo.auth.sso.httpx.AsyncClient") as client_type:
            client_type.return_value.__aenter__.return_value = client
            result = await _exchange_code("https://issuer.example/token", "client", "secret", "code", "callback")

        assert result == payload


# ---------------------------------------------------------------------------
# JIT provisioning — additional cases
# ---------------------------------------------------------------------------


class TestJitProvisioningExtended:
    async def test_creates_user_when_org_exists(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = _mock_session()
        org_id = uuid.uuid4()

        with (
            patch("modulo.auth.sso.get_account_by_email", new_callable=AsyncMock) as mock_get,
            patch("modulo.auth.sso.select") as mock_select,
        ):
            mock_get.return_value = None
            mock_org = MagicMock()
            mock_org.id = org_id
            exec_mock1 = MagicMock()
            exec_mock1.scalar_one_or_none.return_value = mock_org
            exec_mock2 = MagicMock()
            exec_mock2.scalar_one_or_none.return_value = None
            session.execute.side_effect = [exec_mock1, exec_mock2]
            mock_select.return_value.order_by.return_value.limit.return_value = "query"

            account, _actual_org_id, org_role = await jit_provision_user(
                session, settings, "new@example.com", "New User", "oidc", "google:456"
            )

            assert account.email == "new@example.com"
            assert account.display_name == "New User"
            assert account.auth_provider == "oidc"
            assert account.sso_subject == "google:456"
            assert org_role == "runner"

    async def test_finds_existing_user_and_updates_sso(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = _mock_session()
        existing = MagicMock()
        existing.email = "existing@example.com"
        existing.sso_subject = None
        existing.auth_provider = "local"

        org_id = uuid.uuid4()
        exec_mock = MagicMock()
        mock_org = MagicMock()
        mock_org.id = org_id
        exec_mock.scalar_one_or_none.return_value = mock_org
        session.execute.return_value = exec_mock

        with patch("modulo.auth.sso.get_account_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = existing

            account, _, _ = await jit_provision_user(
                session, settings, "existing@example.com", "Existing", "oidc", "google:789"
            )

            assert account is existing
            assert account.sso_subject == "google:789"
            assert account.auth_provider == "oidc"

    async def test_uses_default_org_id(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = _mock_session()
        org_id = uuid.uuid4()

        exec_mock = MagicMock()
        exec_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = exec_mock

        with patch("modulo.auth.sso.get_account_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            account, actual_org_id, _ = await jit_provision_user(
                session,
                settings,
                "user@example.com",
                "User",
                "oidc",
                "sub:1",
                default_org_id=org_id,
            )

            assert actual_org_id == org_id
            # A new account was created — verify fields
            assert account.email == "user@example.com"
            assert account.auth_provider == "oidc"
            assert account.sso_subject == "sub:1"

    async def test_raises_if_no_org_and_no_default(self) -> None:
        from modulo.auth.sso import jit_provision_user

        settings = _override()
        session = _mock_session()

        with patch("modulo.auth.sso.get_account_by_email", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            exec_mock = MagicMock()
            exec_mock.scalar_one_or_none.return_value = None
            session.execute.return_value = exec_mock

            with pytest.raises(RuntimeError, match="No organisation exists"):
                await jit_provision_user(session, settings, "new@example.com", "New", "oidc", "google:123")


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


class TestIssueSsoTokens:
    async def test_issues_access_and_refresh_tokens(self) -> None:
        from modulo.auth.sso import issue_sso_tokens

        settings = _override()
        session = _mock_session()
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

            org_id = user.organisation_id
            result = await issue_sso_tokens(user, org_id, user.org_role, session, settings)

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
        session = _mock_session()
        with pytest.raises(ValueError, match="not configured"):
            await oidc_get_authorize_url("nonexistent", settings, "http://localhost/callback", session, session)

    async def test_raises_when_discovery_missing_authz_endpoint(self) -> None:
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override()
        session = _mock_session()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {"token_endpoint": "https://example.com/token"}

            with pytest.raises(ValueError, match="No authorization_endpoint"):
                await oidc_get_authorize_url("google", settings, "http://localhost/callback", session, session)

    async def test_returns_url_and_state(self) -> None:
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override()
        session = _mock_session()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            }

            url, raw_state = await oidc_get_authorize_url(
                "google", settings, "http://localhost/callback", session, session
            )

            assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
            assert "client_id=google-client-id" in url
            assert "response_type=code" in url
            assert len(raw_state) > 0

    async def test_env_scopes_string_not_character_joined(self) -> None:
        """FAR-464 CHANGES_REQUESTED #3: env-var providers may carry ``scopes`` as a
        plain string. ``" ".join`` on a string would splat it into single characters
        ('o p e ...'); the scope must be passed through verbatim."""
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override(
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "google",
                        "client_id": "google-client-id",
                        "client_secret": "google-client-secret",
                        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                        "scopes": "openid email profile",
                    }
                ]
            )
        )
        session = _mock_session()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            }

            url, _ = await oidc_get_authorize_url("google", settings, "http://localhost/cb", session, session)

            assert "scope=openid+email+profile" in url

    async def test_env_scopes_list_joined(self) -> None:
        """Env-var providers may also carry ``scopes`` as a list — still joined safely."""
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override(
            modulo_oidc_providers=json.dumps(
                [
                    {
                        "provider_id": "google",
                        "client_id": "google-client-id",
                        "client_secret": "google-client-secret",
                        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                        "scopes": ["openid", "email"],
                    }
                ]
            )
        )
        session = _mock_session()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            }

            url, _ = await oidc_get_authorize_url("google", settings, "http://localhost/cb", session, session)

            assert "scope=openid+email" in url


# ---------------------------------------------------------------------------
# OIDC callback — full flow
# ---------------------------------------------------------------------------


class TestOidcProcessCallback:
    async def test_full_success_flow(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        raw_state = "test-raw-state"

        signed = sign_state(f"google:{raw_state}", settings.secret_key)

        id_token = (
            base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(b'{"email":"user@example.com","name":"Test User","sub":"abc123"}')
            .rstrip(b"=")
            .decode()
            + "."
            + "sig"
        )

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://accounts.google.com/token",
                "jwks_uri": "https://accounts.google.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "user@example.com",
                "name": "Test User",
                "sub": "abc123",
            }
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
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
        session = _mock_session()

        with pytest.raises(ValueError, match="CSRF"):
            await oidc_process_callback(
                "code", "tampered-state", settings, session, session, "http://localhost/callback"
            )

    async def test_raises_when_provider_not_found_after_state_check(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("ghost:state", settings.secret_key)

        with pytest.raises(ValueError, match="not found"):
            await oidc_process_callback("code", signed, settings, session, session, "http://localhost/callback")


# ---------------------------------------------------------------------------
# SAML helpers — edge cases
# ---------------------------------------------------------------------------


class TestSamlGetAuthUrl:
    async def test_raises_when_saml_disabled(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(modulo_saml_enabled=False)
        session = _mock_session()
        with patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = None
            with pytest.raises(ValueError, match="SAML is not enabled"):
                await saml_get_auth_url(settings, "http://localhost/acs", session, session)

    async def test_raises_when_no_license(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(modulo_license_key="", modulo_saml_enabled=True)
        session = _mock_session()
        with patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = None
            with pytest.raises(ValueError, match="requires a license"):
                await saml_get_auth_url(settings, "http://localhost/acs", session, session)

    async def test_raises_when_no_metadata_source(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        session = _mock_session()
        with (
            patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock) as mock_db,
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_db.return_value = None
            mock_fetch.side_effect = ValueError("SAML IdP metadata not configured")
            with pytest.raises(ValueError, match="metadata not configured"):
                await saml_get_auth_url(settings, "http://localhost/acs", session, session)


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
        session = _mock_session()
        with pytest.raises(ValueError, match="SAML is not enabled"):
            await saml_process_response("response", settings, session, session)

    async def test_raises_when_no_license(self) -> None:
        from modulo.auth.sso import saml_process_response

        settings = _override(modulo_license_key="", modulo_saml_enabled=True)
        session = _mock_session()
        with pytest.raises(ValueError, match="requires a license"):
            await saml_process_response("response", settings, session, session)

    async def test_raises_when_no_assertion(self) -> None:
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
        )
        session = _mock_session()

        empty_response = base64.b64encode(b"<root/>").decode()
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            with pytest.raises(ValueError, match="SAML response validation failed"):
                await saml_process_response(empty_response, settings, session, session)

    async def test_full_success_flow(self) -> None:
        from modulo.auth.saml_handler import ModuloSamlAuth
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
        )
        session = _mock_session()

        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
            patch.object(
                ModuloSamlAuth,
                "process_response",
                return_value={
                    "name_id": "user@example.com",
                    "attributes": {"email": ["user@example.com"], "displayName": ["Test User"]},
                },
            ),
        ):
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {
                "access_token": "at-saml",
                "refresh_token": "rt-saml",
                "token_type": "bearer",
            }

            result = await saml_process_response(encoded, settings, session, session)

            assert result["access_token"] == "at-saml"
            mock_jit.assert_awaited_once_with(
                session,
                settings,
                "user@example.com",
                "Test User",
                "saml",
                "saml:https://idp.example.com:user@example.com",
                default_org_id=None,
            )
            mock_tok.assert_awaited_once()

    def test_destination_mismatch_rejected(self) -> None:
        from modulo.auth.sso import _validate_saml_response_destination

        xml = self.SAML_RESPONSE_XML.replace(
            "<samlp:Response",
            '<samlp:Response Destination="https://evil.example.com/acs"',
        )
        encoded = base64.b64encode(xml.encode()).decode()

        with pytest.raises(ValueError, match="Destination does not match"):
            _validate_saml_response_destination(encoded, "https://app.example.com/api/v1/auth/saml/acs")

    def test_destination_match_accepted(self) -> None:
        from modulo.auth.sso import _validate_saml_response_destination

        acs = "https://app.example.com/api/v1/auth/saml/acs"
        xml = self.SAML_RESPONSE_XML.replace(
            "<samlp:Response",
            f'<samlp:Response Destination="{acs}"',
        )
        encoded = base64.b64encode(xml.encode()).decode()

        assert _validate_saml_response_destination(encoded, acs) is None

    def test_destination_absent_accepted(self) -> None:
        from modulo.auth.sso import _validate_saml_response_destination

        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
        assert _validate_saml_response_destination(encoded, "https://app.example.com/api/v1/auth/saml/acs") is None

    def test_destination_garbled_response_skipped(self) -> None:
        from modulo.auth.sso import _validate_saml_response_destination

        assert _validate_saml_response_destination("!!not-base64!!", "https://app.example.com/acs") is None

    async def test_saml_process_response_rejects_destination_mismatch(self) -> None:
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
            modulo_public_url="https://app.example.com",
        )
        session = _mock_session()

        xml = self.SAML_RESPONSE_XML.replace(
            "<samlp:Response",
            '<samlp:Response Destination="https://evil.example.com/acs"',
        )
        encoded = base64.b64encode(xml.encode()).decode()

        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            with pytest.raises(ValueError, match="Destination does not match"):
                await saml_process_response(encoded, settings, session, session)


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
            mock_client.get.assert_awaited_once()
            call_args, call_kwargs = mock_client.get.await_args
            assert call_args[0] == "https://idp.example.com/metadata"
            assert call_kwargs["timeout"].connect == 5.0

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
        )
        with patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as m:
            m.return_value = ("https://idp.example.com/sso?SAMLRequest=abc", "_req123")
            resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
            assert resp.status_code == 307
            assert "idp.example.com" in resp.headers.get("location", "")

    def test_saml_acs_with_license_and_valid_response(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )

        with (
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as m,
        ):
            m.return_value = {
                "access_token": "at-saml",
                "refresh_token": "rt-saml",
                "token_type": "bearer",
            }

            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 307  # RedirectResponse
            assert "access_token=at-saml" in resp.headers.get("location", "")

    def test_saml_acs_malformed_response(self, client: TestClient) -> None:
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )

        with patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as m:
            m.side_effect = ValueError("SAML response validation failed: invalid_response")
            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": base64.b64encode(b"<bad/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 401

    def test_saml_metadata_served_for_db_provider_when_env_flag_off(self, client: TestClient) -> None:
        """FAR-464 CHANGES_REQUESTED #2: a DB-configured SAML provider is self-sufficient,
        so /saml/metadata must serve even when modulo_saml_enabled is False — matching
        saml_login/saml_acs which resolve the DB provider without the env flag."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=False,
        )

        db_provider = SimpleNamespace(entity_id="urn:db-saml")
        with patch("modulo.api.routes.sso._get_enabled_saml_global", new_callable=AsyncMock) as m:
            m.return_value = db_provider
            resp = client.get("/api/v1/auth/saml/metadata", follow_redirects=False)

        assert resp.status_code == 200
        assert "urn:db-saml" in resp.text

    def test_saml_metadata_rejected_when_no_db_provider_and_env_flag_off(self, client: TestClient) -> None:
        """Preserves the pure-env-var contract: no DB provider AND flag off -> 400."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=False,
        )

        with patch("modulo.api.routes.sso._get_enabled_saml_global", new_callable=AsyncMock) as m:
            m.return_value = None
            resp = client.get("/api/v1/auth/saml/metadata", follow_redirects=False)

        assert resp.status_code == 400


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
            + base64.urlsafe_b64encode(b'{"email":"user@example.com","name":"Test User","sub":"abc"}')
            .rstrip(b"=")
            .decode()
            + "."
            + "sig"
        )

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://accounts.google.com/token",
                "jwks_uri": "https://accounts.google.com/certs",
                "issuer": "https://accounts.google.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "user@example.com",
                "name": "Test User",
                "sub": "abc",
            }
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
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


# ---------------------------------------------------------------------------
# DB-backed provider resolution (admin UI is now the runtime source of truth)
# ---------------------------------------------------------------------------


class TestOidcGetAuthorizeUrlDb:
    async def test_uses_db_provider(self) -> None:
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override()
        provider = SimpleNamespace(
            provider_type="oidc",
            enabled=True,
            client_id="db-client-id",
            client_secret="db-client-secret",
            discovery_url="https://example.auth0.com/.well-known/openid-configuration",
            scopes=json.dumps(["openid", "email"]),
        )
        session = _mock_session(scalar=provider)
        with patch("modulo.auth.sso._fetch_discovery_pinned", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {
                "authorization_endpoint": "https://example.auth0.com/authorize",
            }
            url, _ = await oidc_get_authorize_url("auth0", settings, "http://localhost/cb", session, session)
            assert "client_id=db-client-id" in url
            assert "example.auth0.com/authorize" in url
            assert "scope=openid+email" in url


class TestOidcProcessCallbackDb:
    async def test_uses_db_provider_secret(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        provider = SimpleNamespace(
            provider_type="oidc",
            enabled=True,
            client_id="db-client-id",
            client_secret="db-client-secret",
            discovery_url="https://example.auth0.com/.well-known/openid-configuration",
            scopes=None,
            group_mappings=[],
            organisation_id=uuid.uuid4(),
        )
        session = _mock_session(scalar=provider)
        signed = sign_state("auth0:raw-state", settings.secret_key)
        id_token = (
            base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(b'{"email":"user@example.com","name":"Test User","sub":"abc"}')
            .rstrip(b"=")
            .decode()
            + "."
            + "sig"
        )

        with (
            patch("modulo.auth.sso._fetch_discovery_pinned", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://example.auth0.com/token",
                "jwks_uri": "https://example.auth0.com/certs",
                "issuer": "https://example.auth0.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "user@example.com",
                "name": "Test User",
                "sub": "abc",
            }
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {
                "access_token": "at-db",
                "refresh_token": "rt-db",
                "token_type": "bearer",
            }

            result = await oidc_process_callback(
                "auth-code", signed, settings, session, session, "http://localhost/callback"
            )

            assert result["access_token"] == "at-db"
            mock_ex.assert_awaited_once_with(
                "https://example.auth0.com/token",
                "db-client-id",
                "db-client-secret",
                "auth-code",
                "http://localhost/callback",
            )


class TestSsoProvidersEndpointDb:
    def test_returns_db_configured_provider(self, client: TestClient) -> None:

        db_provider = SimpleNamespace(provider_id="auth0")
        with (
            patch(
                "modulo.core.license.get_license",
                return_value=SimpleNamespace(tier="team", features=["sso"], expires_at=None),
            ),
            patch.dict("modulo.core.feature_flags.FeatureFlagRegistry._overrides", {}, clear=True),
            patch("modulo.api.routes.sso.list_enabled_oidc_providers", new_callable=AsyncMock) as mock_list,
            patch("modulo.api.routes.sso.get_enabled_saml_provider", new_callable=AsyncMock) as mock_saml,
        ):
            mock_list.return_value = [db_provider]
            mock_saml.return_value = None
            resp = client.get("/api/v1/auth/sso/providers")
            assert resp.status_code == 200
            body = resp.json()
            ids = [p["provider_id"] for p in body["oidc"]]
            assert "auth0" in ids
            assert body["saml"] is False


class TestSamlGetAuthUrlDb:
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

    async def test_uses_db_saml_provider(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(modulo_saml_enabled=False, modulo_license_key="")
        provider = SimpleNamespace(
            metadata_xml=self.SAMPLE_IDP_METADATA,
            metadata_url=None,
            entity_id="https://idp.example.com",
        )
        session = _mock_session(scalar=provider)
        url, _ = await saml_get_auth_url(settings, "https://modulo.example.com/api/v1/auth/saml/acs", session, session)
        assert "idp.example.com" in url
        assert "SAMLRequest" in url


# ---------------------------------------------------------------------------
# System-scoped (modulo_system role) provider resolution — FAR-464 option (a)
# ---------------------------------------------------------------------------


class TestSystemSessionProviderResolution:
    def _oidc_provider(self, org_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(
            provider_type="oidc",
            enabled=True,
            client_id="db-client-id",
            client_secret="db-client-secret",
            discovery_url="https://example.auth0.com/.well-known/openid-configuration",
            scopes=None,
            group_mappings=[],
            organisation_id=org_id,
        )

    async def test_system_session_resolves_non_first_org_provider(self) -> None:
        """A provider in a NON-first org is resolved through the system session.

        The system session (``modulo_system``, BYPASSRLS) returns the provider
        regardless of org, so the resolution is instance-global — it must NOT
        fall back to the app session (which would pin to the first org).
        """
        from modulo.auth.sso import _resolve_oidc_provider

        settings = _override()
        org_id = uuid.uuid4()
        provider = self._oidc_provider(org_id)
        system_session = _mock_session(scalar=provider)
        app_session = _mock_session()

        with patch("modulo.auth.sso._set_default_rls_org", new_callable=AsyncMock) as mock_default_org:
            client_id, _client_secret, discovery_url, _scopes, db_provider = await _resolve_oidc_provider(
                "auth0", system_session, app_session, settings
            )

        assert db_provider is provider
        assert db_provider.organisation_id == org_id
        assert client_id == "db-client-id"
        assert discovery_url == "https://example.auth0.com/.well-known/openid-configuration"
        mock_default_org.assert_not_awaited()

    async def test_oidc_callback_uses_resolved_provider_org_for_jit(self) -> None:
        """The resolved (system) provider's organisation_id drives JIT placement.

        The app session is RLS-scoped to the resolved provider's org and that
        org is passed as ``default_org_id`` so the user is provisioned into it.
        """
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        org_id = uuid.uuid4()
        provider = self._oidc_provider(org_id)
        system_session = _mock_session(scalar=provider)
        app_session = _mock_session()
        signed = sign_state("auth0:raw-state", settings.secret_key)
        id_token = (
            base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(b'{"email":"user@example.com","name":"Test User","sub":"abc"}')
            .rstrip(b"=")
            .decode()
            + "."
            + "sig"
        )

        with (
            patch("modulo.auth.sso._fetch_discovery_pinned", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
            patch("modulo.auth.sso.set_rls_org", new_callable=AsyncMock) as mock_set_rls,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://example.auth0.com/token",
                "jwks_uri": "https://example.auth0.com/certs",
                "issuer": "https://example.auth0.com",
            }
            mock_ex.return_value = {"id_token": id_token}
            mock_verify.return_value = {
                "email": "user@example.com",
                "name": "Test User",
                "sub": "abc",
            }
            mock_jit.return_value = (MagicMock(), org_id, "runner")
            mock_tok.return_value = {
                "access_token": "at-db",
                "refresh_token": "rt-db",
                "token_type": "bearer",
            }

            await oidc_process_callback(
                "auth-code", signed, settings, system_session, app_session, "http://localhost/callback"
            )

        mock_set_rls.assert_awaited_once_with(app_session, org_id)
        mock_jit.assert_awaited_once_with(
            app_session,
            settings,
            "user@example.com",
            "Test User",
            "oidc",
            "auth0:abc",
            default_org_id=org_id,
        )
        mock_tok.assert_awaited_once()

    async def test_app_session_fallback_when_system_resolves_none(self) -> None:
        """Single-org fallback: when the system read returns nothing the app session is used.

        The system role may be unprovisioned (returns zero rows), or the
        provider may genuinely be absent — in either case the resolver falls
        back to the app session scoped to the first org before the env path.
        """
        from modulo.auth.sso import _resolve_oidc_provider

        settings = _override()
        org_id = uuid.uuid4()
        provider = self._oidc_provider(org_id)
        system_session = _mock_session()
        app_session = _mock_session(scalar=provider)

        with patch("modulo.auth.sso._set_default_rls_org", new_callable=AsyncMock) as mock_default_org:
            _client_id, _cs, _du, _scopes, db_provider = await _resolve_oidc_provider(
                "auth0", system_session, app_session, settings
            )

        assert db_provider is provider
        mock_default_org.assert_awaited_once_with(app_session)


# ---------------------------------------------------------------------------
# FAR-506: OIDC derived-endpoint host allowlist (SSRF + client_secret disclosure)
# ---------------------------------------------------------------------------


class TestOidcEndpointHostAllowlist:
    """A compromised/malicious discovery document must NOT be able to point a
    derived endpoint at an internal/cloud-metadata host (the **pinned client** is
    the real SSRF boundary — it blocks metadata/loopback and never POSTs
    ``client_secret`` to a hostile host). The exact-host allowlist is
    defense-in-depth only: multi-host IdPs (Google: ``token_endpoint`` on
    oauth2.googleapis.com, ``jwks_uri`` on www.googleapis.com) are legitimate
    and must NOT be rejected (FAR-506)."""

    _DISCOVERY = "https://issuer.example/.well-known/openid-configuration"
    _ISSUER = "https://issuer.example"

    async def test_callback_accepts_cross_host_token_endpoint_and_exchanges(self) -> None:
        """FAR-506: a sibling-host token endpoint (Google) is allowed — the code
        exchange proceeds and the ``client_secret`` goes only to the pinned,
        publicly-validated host."""
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("google:state", settings.secret_key)

        with (
            patch(
                "modulo.auth.sso._resolve_oidc_provider",
                new_callable=AsyncMock,
                return_value=("cid", "client-secret", self._DISCOVERY, None, None),
            ),
            patch(
                "modulo.auth.sso._fetch_discovery",
                new_callable=AsyncMock,
                return_value={
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                    "jwks_uri": "https://issuer.example/jwks",
                    "issuer": self._ISSUER,
                },
            ),
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_ex.return_value = {"id_token": "jwt"}
            mock_verify.return_value = {"email": "u@e.z", "sub": "s"}
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            result = await oidc_process_callback("code", signed, settings, session, session, "http://cb")

        assert result["access_token"] == "at"
        mock_ex.assert_awaited_once()
        mock_verify.assert_awaited_once()

    async def test_callback_rejects_internal_token_endpoint_no_exchange(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("google:state", settings.secret_key)

        with (
            patch(
                "modulo.auth.sso._resolve_oidc_provider",
                new_callable=AsyncMock,
                return_value=("cid", "client-secret", self._DISCOVERY, None, None),
            ),
            patch(
                "modulo.auth.sso._fetch_discovery",
                new_callable=AsyncMock,
                return_value={
                    "token_endpoint": "http://169.254.169.254/latest/meta-data/iam/security-credentials",
                    "jwks_uri": "https://issuer.example/jwks",
                    "issuer": self._ISSUER,
                },
            ),
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            pytest.raises(ValueError, match="Rejected OIDC token endpoint"),
        ):
            await oidc_process_callback("code", signed, settings, session, session, "http://cb")

        mock_ex.assert_not_called()

    async def test_callback_accepts_cross_host_jwks_uri_and_verifies(self) -> None:
        """FAR-506: a sibling-host ``jwks_uri`` (Google's www.googleapis.com) is
        allowed — the ID-token signature verification proceeds rather than being
        rejected before the JWKS fetch."""
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("google:state", settings.secret_key)

        with (
            patch(
                "modulo.auth.sso._resolve_oidc_provider",
                new_callable=AsyncMock,
                return_value=("cid", "client-secret", self._DISCOVERY, None, None),
            ),
            patch(
                "modulo.auth.sso._fetch_discovery",
                new_callable=AsyncMock,
                return_value={
                    "token_endpoint": "https://issuer.example/token",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "issuer": self._ISSUER,
                },
            ),
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock, return_value={"id_token": "jwt"}),
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_verify.return_value = {"email": "u@e.z", "sub": "s"}
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            result = await oidc_process_callback("code", signed, settings, session, session, "http://cb")

        assert result["access_token"] == "at"
        mock_verify.assert_awaited_once()

    async def test_authorize_accepts_cross_host_authorization_endpoint(self) -> None:
        """FAR-506: a sibling-host ``authorization_endpoint`` is allowed (Google
        redirects to accounts.google.com/auth while discovery hosts differ)."""
        from modulo.auth.sso import oidc_get_authorize_url

        settings = _override()
        session = _mock_session()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc:
            mock_disc.return_value = {
                "authorization_endpoint": "https://oauth2.googleapis.com/authorize",
                "issuer": self._ISSUER,
            }
            url, _ = await oidc_get_authorize_url("google", settings, "http://cb", session, session)

        assert url.startswith("https://oauth2.googleapis.com/authorize")

    async def test_callback_same_host_path_works(self) -> None:
        """The legit same-host path still reaches the token exchange (mocked)."""
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("google:state", settings.secret_key)

        with (
            patch(
                "modulo.auth.sso._resolve_oidc_provider",
                new_callable=AsyncMock,
                return_value=("cid", "client-secret", self._DISCOVERY, None, None),
            ),
            patch(
                "modulo.auth.sso._fetch_discovery",
                new_callable=AsyncMock,
                return_value={
                    "token_endpoint": "https://issuer.example/token",
                    "jwks_uri": "https://issuer.example/jwks",
                    "issuer": self._ISSUER,
                },
            ),
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_ex.return_value = {"id_token": "jwt"}
            mock_verify.return_value = {"email": "u@e.z", "sub": "s"}
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            result = await oidc_process_callback("code", signed, settings, session, session, "http://cb")

        assert result["access_token"] == "at"
        mock_ex.assert_awaited_once_with("https://issuer.example/token", "cid", "client-secret", "code", "http://cb")
        mock_verify.assert_awaited_once()


class TestEnforceOidcEndpointHost:
    def test_accepts_cross_host_sibling(self) -> None:
        """FAR-506: a sibling-host endpoint (Google's token endpoint on
        oauth2.googleapis.com, not accounts.google.com) is NEWLY allowed. The
        exact-host allowlist is now a preferred-log nicety, not a hard gate."""
        from modulo.auth.sso import _enforce_oidc_endpoint_host

        assert (
            _enforce_oidc_endpoint_host(
                "https://oauth2.googleapis.com/token",
                "https://accounts.google.com/.well-known/openid-configuration",
                "https://accounts.google.com",
                "token",
            )
            is None
        )

    def test_rejects_metadata_address(self) -> None:
        from modulo.auth.sso import _enforce_oidc_endpoint_host

        with pytest.raises(ValueError, match="Rejected OIDC jwks endpoint"):
            _enforce_oidc_endpoint_host(
                "http://169.254.169.254/jwks",
                "https://issuer.example/.well-known/openid-configuration",
                "https://issuer.example",
                "jwks",
            )

    def test_rejects_https_internal_literal(self) -> None:
        """A HTTPS internal/metadata literal is still blocked by the SSRF preflight."""
        from modulo.auth.sso import _enforce_oidc_endpoint_host

        with pytest.raises(ValueError, match="Rejected OIDC token endpoint"):
            _enforce_oidc_endpoint_host(
                "https://169.254.169.254/latest/meta-data/credentials",
                "https://issuer.example/.well-known/openid-configuration",
                "https://issuer.example",
                "token",
            )

    def test_accepts_same_host(self) -> None:
        from modulo.auth.sso import _enforce_oidc_endpoint_host

        assert (
            _enforce_oidc_endpoint_host(
                "https://issuer.example/token",
                "https://issuer.example/.well-known/openid-configuration",
                "https://issuer.example",
                "token",
            )
            is None
        )

    def test_issuer_host_also_allowed(self) -> None:
        from modulo.auth.sso import _enforce_oidc_endpoint_host

        # The jwks_uri may target the issuer host even when it differs (in path)
        # from the discovery URL host — the issuer is an OIDC trust anchor.
        assert (
            _enforce_oidc_endpoint_host(
                "https://issuer.example/jwks",
                "https://issuer.example/.well-known/openid-configuration",
                "https://issuer.example",
                "jwks",
            )
            is None
        )

    def test_rejects_non_http_scheme(self) -> None:
        from modulo.auth.sso import _enforce_oidc_endpoint_host

        with pytest.raises(ValueError, match="Rejected OIDC token endpoint"):
            _enforce_oidc_endpoint_host(
                "file:///etc/passwd",
                "https://issuer.example/.well-known/openid-configuration",
                "https://issuer.example",
                "token",
            )


# ---------------------------------------------------------------------------
# FAR-506: multi-host IdP (Google) regression — relaxed endpoint host guard
# ---------------------------------------------------------------------------


class TestOidcMultiHostIdp:
    """FAR-506 regression: Google's real discovery document returns sibling hosts
    (token_endpoint on oauth2.googleapis.com, jwks_uri on www.googleapis.com)
    that are NOT the discovery/issuer host (accounts.google.com). The relaxed
    endpoint guard must let the flow proceed, while an internal/metadata or
    non-HTTPS endpoint is still rejected before the code exchange — the
    client_secret is never sent to a hostile host."""

    async def test_google_multi_host_endpoints_proceed(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("google:multi-host", settings.secret_key)

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
            mock_ex.return_value = {"id_token": "header.payload.signature"}
            mock_verify.return_value = {"email": "user@example.com", "name": "Test User", "sub": "abc123"}
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            result = await oidc_process_callback(
                "auth-code", signed, settings, session, session, "http://localhost/callback"
            )

            assert result["access_token"] == "at"
            mock_ex.assert_awaited_once()  # token exchange was reached — no cross-host rejection

    async def test_metadata_token_endpoint_rejected_before_exchange(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("google:meta", settings.secret_key)

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
        ):
            mock_disc.return_value = {
                "token_endpoint": "https://169.254.169.254/latest/meta-data/credentials",
                "issuer": "https://accounts.google.com",
            }
            with pytest.raises(ValueError, match="Rejected OIDC token endpoint"):
                await oidc_process_callback(
                    "auth-code", signed, settings, session, session, "http://localhost/callback"
                )
            mock_ex.assert_not_awaited()  # client_secret never POSTed to the metadata host

    async def test_non_https_token_endpoint_rejected_before_exchange(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = _mock_session()
        signed = sign_state("google:http", settings.secret_key)

        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
        ):
            mock_disc.return_value = {
                "token_endpoint": "http://oauth2.googleapis.com/token",
                "issuer": "https://accounts.google.com",
            }
            with pytest.raises(ValueError, match="must use https:// scheme"):
                await oidc_process_callback(
                    "auth-code", signed, settings, session, session, "http://localhost/callback"
                )
            mock_ex.assert_not_awaited()
