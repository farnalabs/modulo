"""SSO (OIDC + SAML) unit tests: state signing, provider parsing, JIT provisioning, routes."""

import base64
import contextlib
import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from defusedxml import ElementTree
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import etree as lxml_etree
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import (
    _get_engine,
    get_anonymous_plan_context,
    get_db_session,
    get_plan_context,
    get_system_db_session,
)
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
    _app.dependency_overrides[get_anonymous_plan_context] = lambda: DbPlanContext(
        FeatureFlagRegistry(current_tier="team")
    )
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
    _app.dependency_overrides[get_anonymous_plan_context] = lambda: DbPlanContext(
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


_NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
_NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
_NS_STATUS = "urn:oasis:names:tc:SAML:2.0:status"


def _build_xsd_compliant_saml_response(
    *,
    audience: str,
    destination: str | None = None,
    recipient: str | None = None,
    name_id_value: str = "user@example.com",
) -> str:
    """Build a minimal but XSD-compliant SAML Response with fresh timestamps.

    Used by tests that drive the REAL python3-saml strict validation through
    saml_process_response (only the XML signature step is mocked there —
    xmlsec signing cannot round-trip on Windows).
    """
    s = f"{{{_NS_SAML}}}"
    n = f"{{{_NS_SAMLP}}}"
    now = datetime.now(UTC)
    instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    nooa = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nb = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    dummy = lxml_etree.Element("root")
    r = lxml_etree.SubElement(dummy, f"{n}Response")
    r.set("ID", "_resp_kitchen_sink")
    r.set("Version", "2.0")
    r.set("IssueInstant", instant)
    if destination:
        r.set("Destination", destination)
    st = lxml_etree.SubElement(r, f"{n}Status")
    sc = lxml_etree.SubElement(st, f"{n}StatusCode")
    sc.set("Value", f"{_NS_STATUS}:Success")
    a = lxml_etree.SubElement(r, f"{s}Assertion")
    a.set("ID", "_assert_kitchen_sink")
    a.set("Version", "2.0")
    a.set("IssueInstant", instant)
    lxml_etree.SubElement(a, f"{s}Issuer").text = "https://idp.example.com"
    subj = lxml_etree.SubElement(a, f"{s}Subject")
    nid = lxml_etree.SubElement(subj, f"{s}NameID")
    nid.text = name_id_value
    nid.set("Format", "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress")
    sc2 = lxml_etree.SubElement(subj, f"{s}SubjectConfirmation")
    sc2.set("Method", "urn:oasis:names:tc:SAML:2.0:cm:bearer")
    scd = lxml_etree.SubElement(sc2, f"{s}SubjectConfirmationData")
    scd.set("NotOnOrAfter", nooa)
    if recipient:
        scd.set("Recipient", recipient)
    conds = lxml_etree.SubElement(a, f"{s}Conditions")
    conds.set("NotBefore", nb)
    conds.set("NotOnOrAfter", nooa)
    if audience is not None:
        ar = lxml_etree.SubElement(conds, f"{s}AudienceRestriction")
        lxml_etree.SubElement(ar, f"{s}Audience").text = audience
    asn = lxml_etree.SubElement(a, f"{s}AuthnStatement")
    asn.set("AuthnInstant", instant)
    asn.set("SessionIndex", "_s1")
    cx = lxml_etree.SubElement(asn, f"{s}AuthnContext")
    cl = lxml_etree.SubElement(cx, f"{s}AuthnContextClassRef")
    cl.text = "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
    astmt = lxml_etree.SubElement(a, f"{s}AttributeStatement")
    at = lxml_etree.SubElement(astmt, f"{s}Attribute")
    at.set("Name", "email")
    av = lxml_etree.SubElement(at, f"{s}AttributeValue")
    av.text = name_id_value
    atm = lxml_etree.SubElement(astmt, f"{s}Attribute")
    atm.set("Name", "displayName")
    avm = lxml_etree.SubElement(atm, f"{s}AttributeValue")
    avm.text = "Test User"
    dummy.remove(r)
    return lxml_etree.tostring(r, xml_declaration=True, encoding="UTF-8").decode()


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
                sso_provider=ANY,
                email_verified=True,
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

    async def test_saml_process_response_accepts_real_public_acs_destination(self) -> None:
        """FAR-1011: a real-IdP-shaped response whose Destination equals the
        ACS URL derived from ``modulo_public_url`` must now authenticate.

        Reproduced as REJECTED before the fix (strict-mode current_url was
        hardcoded ``http://localhost``, so python3-saml's Destination check
        failed for any real https host). Only the XML signature step is
        mocked here — the real handler and python3-saml validation run.
        """
        from onelogin.saml2.response import OneLogin_Saml2_Response
        from onelogin.saml2.utils import OneLogin_Saml2_Utils

        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
            modulo_public_url="https://app.example.com",
        )
        session = _mock_session()

        acs = "https://app.example.com/api/v1/auth/saml/acs"
        entity_id = "modulo"  # settings.modulo_saml_entity_id default
        xml = _build_xsd_compliant_saml_response(
            destination=acs,
            recipient=acs,
            audience=entity_id,
        )
        encoded = base64.b64encode(xml.encode()).decode()

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch.object(OneLogin_Saml2_Utils, "validate_sign", return_value=True),
            patch.object(
                OneLogin_Saml2_Response,
                "process_signed_elements",
                return_value=["{urn:oasis:names:tc:SAML:2.0:protocol}Response"],
            ),
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_tok.return_value = {"access_token": "at-saml"}
            result = await saml_process_response(encoded, settings, session, session)
            assert result["access_token"] == "at-saml"


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

    def test_saml_metadata_escapes_entity_id_with_db_provider_and_env_flag_off(self, client: TestClient) -> None:
        """A DB-configured entityID containing XML metacharacters must be escaped
        so the SP-metadata document stays well-formed (issue #282..#281: XML
        injection through the org-configurable entity_id)."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=False,
        )

        db_provider = SimpleNamespace(entity_id='urn:x<y>&z>" onload="')
        with patch("modulo.api.routes.sso._get_enabled_saml_global", new_callable=AsyncMock) as m:
            m.return_value = db_provider
            resp = client.get("/api/v1/auth/saml/metadata", follow_redirects=False)

        assert resp.status_code == 200
        assert "urn:x&lt;y&gt;&amp;z&gt;&quot; onload=&quot;" in resp.text
        assert "urn:x<" not in resp.text
        # A raw double quote would terminate the entityID attribute early and
        # let the injected text become new attributes on EntityDescriptor.
        assert '" onload="' not in resp.text
        assert resp.text.count('entityID="') == 1
        # Parse the document: it must be well-formed and the entityID attr
        # must round-trip to exactly the configured value (no injection text
        # became attribute markup).
        root = ElementTree.fromstring(resp.text)
        assert root.tag.endswith("EntityDescriptor")
        assert root.get("entityID") == 'urn:x<y>&z>" onload="'
        assert root.get("onload") is None

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

    def test_saml_metadata_escapes_xml_metacharacters_in_entity_id(self, client: TestClient) -> None:
        """FAR-915 / GitHub #281: entity_id is org-configurable (DB-backed), so a
        malicious value must be XML-escaped — no breakout of the entityID
        attribute, no injected elements, and the output must stay well-formed.
        """
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_public_url="https://app.example.com",
        )

        injected = 'evil"><md:EntitiesDescriptor>&"\''
        db_provider = SimpleNamespace(entity_id=injected)
        with patch("modulo.api.routes.sso._get_enabled_saml_global", new_callable=AsyncMock) as m:
            m.return_value = db_provider
            resp = client.get("/api/v1/auth/saml/metadata", follow_redirects=False)

        assert resp.status_code == 200
        body = resp.text
        # The raw metacharacters must never appear unescaped.
        assert 'entityID="evil"' not in body
        assert "<md:EntitiesDescriptor>" not in body

        # The escaped forms are present and the metadata still parses as XML.
        assert "&lt;md:EntitiesDescriptor&gt;" in body
        assert "&amp;" in body
        import xml.etree.ElementTree as ET

        root = ET.fromstring(body.replace('<?xml version="1.0"?>', ""))  # noqa: S314 - well-formedness check only
        assert root.tag == "{urn:oasis:names:tc:SAML:2.0:metadata}EntityDescriptor"
        entity_id = root.attrib["entityID"]
        assert entity_id == injected

    def test_saml_metadata_escapes_metacharacters_in_acs_url(self, client: TestClient) -> None:
        """FAR-915 defence-in-depth: the Location attribute interpolates the
        public URL too and must be escaped alongside entity_id."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_public_url='https://app.example.com" onload="x',
        )

        with patch("modulo.api.routes.sso._get_enabled_saml_global", new_callable=AsyncMock) as m:
            m.return_value = SimpleNamespace(entity_id="urn:ok")
            resp = client.get("/api/v1/auth/saml/metadata", follow_redirects=False)

        assert resp.status_code == 200
        body = resp.text
        assert 'onload="x"' not in body
        assert "&quot;" in body


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

        db_provider = SimpleNamespace(provider_id="auth0", name="Auth0 SSO", preset="auth0")
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
            sso_provider=ANY,
            email_verified=ANY,
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


# ---------------------------------------------------------------------------
# FAR-855: the SSO join gate (invitation / domain-allowlist / operator flag)
# ---------------------------------------------------------------------------


def _gate_provider(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "provider_id": "corporate",
        "name": "Corporate",
        "provider_type": "oidc",
        "auto_provision": False,
        "allowed_domains": [],
        "default_role": "runner",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _gate_invitation(role: str = "runner") -> SimpleNamespace:
    """A live invitation pending consumption (id/email matched by the CRUD lookup)."""
    return SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4(), org_role=role)


class TestSsoJoinGate:
    """FAR-855: a previously-unknown SSO identity must NOT self-join an org.

    The mocks hang off an ExitStack created BEFORE the denial can raise, so
    the post-assertions (create_membership never awaited, ...) stay reachable
    in the deny paths.
    """

    ORG_ID = uuid.uuid4()

    def _gate_mocks(self) -> tuple[contextlib.ExitStack, SimpleNamespace]:
        stack = contextlib.ExitStack()
        mocks = SimpleNamespace(
            get_acct=stack.enter_context(patch("modulo.auth.sso.get_account_by_email", new_callable=AsyncMock)),
            membership=stack.enter_context(
                patch("modulo.auth.sso.get_membership_by_account_and_org", new_callable=AsyncMock)
            ),
            invite=stack.enter_context(patch("modulo.auth.sso.get_live_for_email", new_callable=AsyncMock)),
            consume=stack.enter_context(patch("modulo.auth.sso.consume_invitation", new_callable=AsyncMock)),
            create=stack.enter_context(patch("modulo.auth.sso.create_membership", new_callable=AsyncMock)),
            reactivate=stack.enter_context(patch("modulo.auth.sso.reactivate_membership", new_callable=AsyncMock)),
            flag=stack.enter_context(
                patch("modulo.auth.sso.resolve_sso_unrestricted_provisioning", new_callable=AsyncMock)
            ),
            audit=stack.enter_context(patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock)),
        )
        mocks.get_acct.return_value = None
        mocks.membership.return_value = None
        mocks.invite.return_value = None
        mocks.flag.return_value = False
        mocks.consume.return_value = True
        mocks.create.return_value = SimpleNamespace(role="runner")
        return stack, mocks

    async def _join(
        self,
        settings: Settings,
        session: AsyncSession,
        provider: SimpleNamespace | None,
        email: str,
        *,
        email_verified: bool = True,
    ) -> tuple[object, object, str]:
        from modulo.auth.sso import jit_provision_user

        # The gate is typed against the SsoProvider model; the tests drive it
        # with structurally-identical stand-ins, so cast explicitly.
        sso_provider = cast("Any", provider)
        return await jit_provision_user(
            session,
            settings,
            email,
            "New User",
            "oidc",
            "corporate:sub",
            default_org_id=self.ORG_ID,
            sso_provider=sso_provider,
            email_verified=email_verified,
        )

    async def test_mode1_default_denies_unknown_identity_without_account_write(self) -> None:
        from modulo.auth.sso import SsoProvisioningDeniedError

        stack, mocks = self._gate_mocks()
        with stack, pytest.raises(SsoProvisioningDeniedError, match="not been invited"):
            await self._join(_override(), _mock_session(), _gate_provider(), "new@example.com")
        mocks.get_acct.assert_awaited_once()
        mocks.create.assert_not_awaited()
        mocks.consume.assert_not_awaited()

    async def test_mode1_live_invitation_joins_with_invitation_role_and_consumes(self) -> None:
        stack, mocks = self._gate_mocks()
        invitation = _gate_invitation(role="operator")
        settings = _override()
        session = _mock_session()
        with stack:
            mocks.get_acct.side_effect = [None, SimpleNamespace(id=uuid.uuid4(), email="new@example.com")]
            mocks.invite.return_value = invitation
            mocks.create.return_value = SimpleNamespace(role="operator")

            _account, org_id, role = await self._join(settings, session, _gate_provider(), "new@example.com")

        assert role == "operator"
        assert org_id == self.ORG_ID
        mocks.create.assert_awaited_once()
        assert mocks.create.await_args.kwargs["role"] == "operator"
        mocks.consume.assert_awaited_once()
        assert mocks.consume.await_args.args[-1] is invitation
        mocks.audit.assert_awaited_once()

    async def test_mode1_invitation_audit_failure_is_fail_open(self) -> None:
        stack, mocks = self._gate_mocks()
        invitation = _gate_invitation(role="runner")
        settings = _override()
        session = _mock_session()
        with stack:
            mocks.get_acct.side_effect = [None, SimpleNamespace(id=uuid.uuid4(), email="new@example.com")]
            mocks.invite.return_value = invitation
            mocks.audit.side_effect = RuntimeError("audit chain unavailable")

            _account, _org_id, role = await self._join(settings, session, _gate_provider(), "new@example.com")

        assert role == "runner"

    async def test_mode1_invitation_consumed_twice_raised_as_denial(self) -> None:
        """A lost CAS race aborts the join atomically (same contract as accept-invite)."""
        from modulo.auth.sso import SsoProvisioningDeniedError

        stack, mocks = self._gate_mocks()
        invitation = _gate_invitation(role="runner")
        settings = _override()
        session = _mock_session()
        with stack:
            mocks.get_acct.side_effect = [None, SimpleNamespace(id=uuid.uuid4(), email="new@example.com")]
            mocks.invite.return_value = invitation
            mocks.consume.return_value = False

            with pytest.raises(SsoProvisioningDeniedError):
                await self._join(settings, session, _gate_provider(), "new@example.com")

    async def test_mode2_domain_allowlist_joins_verified_exact_domain(self) -> None:
        stack, mocks = self._gate_mocks()
        settings = _override()
        session = _mock_session()
        provider = _gate_provider(auto_provision=True, allowed_domains=["  CORP.Example.COM "], default_role="operator")
        with stack:
            mocks.get_acct.side_effect = [None, SimpleNamespace(id=uuid.uuid4(), email="user@corp.example.com")]
            mocks.create.return_value = SimpleNamespace(role="operator")

            _account, _org_id, role = await self._join(settings, session, provider, "user@corp.example.com")

        assert role == "operator"
        mocks.create.assert_awaited_once()
        assert mocks.create.await_args.kwargs["role"] == "operator"
        mocks.invite.assert_awaited_once()
        mocks.consume.assert_not_awaited()

    async def test_mode2_pending_invitation_role_wins_over_allowlist_default(self) -> None:
        """FAR-855: a pending invitation outranks the domain-allowlist default role.

        Regression: the auto-join branch previously called
        ``_provision_membership`` WITHOUT the invitation, granting
        ``provider.default_role`` (runner here) and then CAS-consuming the
        pending invitation anyway - the user joined at the wrong role and the
        invitation was burned. The invitation's role must win.
        """
        stack, mocks = self._gate_mocks()
        invitation = _gate_invitation(role="operator")
        settings = _override()
        session = _mock_session()
        provider = _gate_provider(auto_provision=True, allowed_domains=["corp.example.com"], default_role="runner")
        with stack:
            mocks.get_acct.side_effect = [None, SimpleNamespace(id=uuid.uuid4(), email="user@corp.example.com")]
            mocks.invite.return_value = invitation
            mocks.create.return_value = SimpleNamespace(role="operator")

            _account, _org_id, role = await self._join(settings, session, provider, "user@corp.example.com")

        assert role == "operator"
        mocks.create.assert_awaited_once()
        assert mocks.create.await_args.kwargs["role"] == "operator"
        mocks.consume.assert_awaited_once()
        assert mocks.consume.await_args.args[-1] is invitation
        mocks.audit.assert_awaited_once()

    async def test_mode2_requires_verified_email(self) -> None:
        from modulo.auth.sso import SsoProvisioningDeniedError

        stack, mocks = self._gate_mocks()
        provider = _gate_provider(auto_provision=True, allowed_domains=["corp.example.com"])
        with stack, pytest.raises(SsoProvisioningDeniedError):
            await self._join(_override(), _mock_session(), provider, "user@corp.example.com", email_verified=False)
        mocks.create.assert_not_awaited()

    async def test_mode2_exact_match_rejects_subdomains(self) -> None:
        from modulo.auth.sso import SsoProvisioningDeniedError

        stack, mocks = self._gate_mocks()
        provider = _gate_provider(auto_provision=True, allowed_domains=["corp.example.com"])
        with stack, pytest.raises(SsoProvisioningDeniedError):
            await self._join(_override(), _mock_session(), provider, "attacker@sub.corp.example.com")
        mocks.create.assert_not_awaited()

    async def test_mode2_unverified_email_still_joins_via_invitation(self) -> None:
        stack, mocks = self._gate_mocks()
        invitation = _gate_invitation(role="operator")
        settings = _override()
        session = _mock_session()
        provider = _gate_provider(auto_provision=True, allowed_domains=["corp.example.com"])
        with stack:
            mocks.get_acct.side_effect = [None, SimpleNamespace(id=uuid.uuid4(), email="user@corp.example.com")]
            mocks.invite.return_value = invitation
            mocks.create.return_value = SimpleNamespace(role="operator")

            _account, _org_id, role = await self._join(
                settings, session, provider, "user@corp.example.com", email_verified=False
            )

        assert role == "operator"

    async def test_mode3_flag_off_fails_closed(self) -> None:
        from modulo.auth.sso import SsoProvisioningDeniedError

        stack, mocks = self._gate_mocks()
        provider = _gate_provider(auto_provision=True)  # empty allowed_domains = mode 3
        with stack, pytest.raises(SsoProvisioningDeniedError):
            await self._join(_override(), _mock_session(), provider, "new@example.com")
        mocks.flag.assert_awaited_once()
        mocks.create.assert_not_awaited()

    async def test_mode3_flag_on_joins_verified_email(self) -> None:
        stack, mocks = self._gate_mocks()
        settings = _override()
        session = _mock_session()
        provider = _gate_provider(auto_provision=True, default_role="operator")
        with stack:
            mocks.get_acct.side_effect = [None, SimpleNamespace(id=uuid.uuid4(), email="new@example.com")]
            mocks.flag.return_value = True
            mocks.create.return_value = SimpleNamespace(role="operator")

            _account, _org_id, role = await self._join(settings, session, provider, "new@example.com")

        assert role == "operator"
        mocks.consume.assert_not_awaited()

    async def test_mode3_requires_verified_email(self) -> None:
        from modulo.auth.sso import SsoProvisioningDeniedError

        stack, mocks = self._gate_mocks()
        provider = _gate_provider(auto_provision=True)
        with stack, pytest.raises(SsoProvisioningDeniedError):
            await self._join(_override(), _mock_session(), provider, "new@example.com", email_verified=False)
        mocks.flag.assert_not_awaited()
        mocks.create.assert_not_awaited()

    async def test_existing_member_role_unchanged(self) -> None:
        stack, mocks = self._gate_mocks()
        account = SimpleNamespace(id=uuid.uuid4(), email="member@example.com")
        with stack:
            mocks.get_acct.side_effect = [account, account]
            mocks.membership.return_value = SimpleNamespace(role="runner", deactivated_at=None)

            _account, _org_id, role = await self._join(
                _override(), _mock_session(), _gate_provider(), "member@example.com"
            )

        assert role == "runner"
        mocks.create.assert_not_awaited()
        mocks.invite.assert_not_awaited()
        mocks.consume.assert_not_awaited()

    async def test_tombstoned_membership_reactivated_by_invitation_role(self) -> None:
        stack, mocks = self._gate_mocks()
        invitation = _gate_invitation(role="operator")
        account = SimpleNamespace(id=uuid.uuid4(), email="old@example.com")
        settings = _override()
        session = _mock_session()
        with stack:
            mocks.get_acct.side_effect = [account, account]
            mocks.membership.return_value = SimpleNamespace(role="runner", deactivated_at=datetime.now(UTC))
            mocks.invite.return_value = invitation

            _account, _org_id, role = await self._join(settings, session, _gate_provider(), "old@example.com")

        assert role == "operator"
        mocks.reactivate.assert_awaited_once()
        assert mocks.reactivate.await_args.args[2] == "operator"
        mocks.consume.assert_awaited_once()
        mocks.audit.assert_awaited_once()

    async def test_env_path_provider_keeps_legacy_provisioning(self) -> None:
        stack, mocks = self._gate_mocks()
        settings = _override()
        session = _mock_session()
        with stack:
            mocks.create.return_value = SimpleNamespace(role="runner")

            _account, org_id, role = await self._join(settings, session, None, "legacy@example.com")

        assert org_id == self.ORG_ID
        assert role == "runner"
        mocks.create.assert_awaited_once()
        mocks.consume.assert_not_awaited()
        mocks.flag.assert_not_awaited()

    async def test_env_path_existing_live_member_keeps_role(self) -> None:
        """Legacy env-var provider: an existing live member is left untouched."""
        stack, mocks = self._gate_mocks()
        account = SimpleNamespace(id=uuid.uuid4(), email="member@example.com", sso_subject=None, auth_provider="oidc")
        with stack:
            mocks.get_acct.return_value = account
            mocks.membership.return_value = SimpleNamespace(role="operator", deactivated_at=None)

            _account, org_id, role = await self._join(_override(), _mock_session(), None, "member@example.com")

        assert org_id == self.ORG_ID
        assert role == "operator"
        mocks.create.assert_not_awaited()
        mocks.consume.assert_not_awaited()

    async def test_mode2_email_without_at_sign_is_denied(self) -> None:
        """A malformed email has no verifiable domain, so the allowlist cannot match."""
        from modulo.auth.sso import SsoProvisioningDeniedError

        stack, mocks = self._gate_mocks()
        provider = _gate_provider(auto_provision=True, allowed_domains=["corp.example.com"])
        with stack, pytest.raises(SsoProvisioningDeniedError):
            await self._join(_override(), _mock_session(), provider, "not-an-email")
        mocks.create.assert_not_awaited()

    def test_sso_verified_domain_rejects_non_email(self) -> None:
        from modulo.auth.sso import _sso_verified_domain

        assert _sso_verified_domain("plainname") is None
        assert _sso_verified_domain("a@b") == "b"
        assert _sso_verified_domain("a@b@BAD.com") == "bad.com"


# ---------------------------------------------------------------------------
# Per-provider SAML routes (FAR-1001)
# ---------------------------------------------------------------------------


def _make_saml_provider(
    *,
    provider_id: str = "okta-saml",
    entity_id: str | None = None,
    enabled: bool = True,
    org_id: uuid.UUID | None = None,
) -> MagicMock:
    """Build a mock SsoProvider with SAML type."""
    p = MagicMock()
    p.provider_id = provider_id
    p.provider_type = "saml"
    p.enabled = enabled
    p.entity_id = entity_id
    p.organisation_id = org_id or uuid.uuid4()
    p.metadata_xml = None
    p.metadata_url = None
    p.name = "Okta SAML"
    return p


class TestPerProviderSamlRoutes:
    """Per-provider SAML route surface (FAR-1001 / FAR-1002)."""

    def test_per_provider_acs_valid_slug(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs resolves provider by slug and processes response."""
        provider = _make_saml_provider(provider_id="okta-saml")
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = provider
            mock_process.return_value = {
                "access_token": "at-per",
                "refresh_token": "rt-per",
                "token_type": "bearer",
            }
            resp = client.post(
                "/api/v1/auth/saml/acs/okta-saml",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 307
            assert "access_token=at-per" in resp.headers["location"]
            # Verify provider_id was threaded through to saml_process_response
            mock_process.assert_awaited_once()
            call_kwargs = mock_process.call_args
            assert call_kwargs.kwargs.get("provider_id") == "okta-saml"

    def test_per_provider_acs_unknown_slug_returns_404(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs returns uniform 404 for unknown slug."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = None
            resp = client.post(
                "/api/v1/auth/saml/acs/ghost-saml",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Not Found"

    def test_per_provider_acs_disabled_slug_returns_404(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs returns same 404 for disabled provider."""
        provider = _make_saml_provider(provider_id="okta-saml", enabled=False)
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = provider
            resp = client.post(
                "/api/v1/auth/saml/acs/okta-saml",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Not Found"

    def test_per_provider_acs_non_saml_slug_returns_404(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs returns same 404 for non-SAML provider type."""
        provider = _make_saml_provider(provider_id="google-oidc")
        provider.provider_type = "oidc"
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = provider
            resp = client.post(
                "/api/v1/auth/saml/acs/google-oidc",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Not Found"

    def test_per_provider_metadata_emits_own_entity_id_and_acs(self, client: TestClient) -> None:
        """GET /saml/{slug}/metadata emits provider-specific Entity ID and ACS Location."""
        provider = _make_saml_provider(
            provider_id="okta-saml",
            entity_id="https://idp.okta.com/sp/abc123",
        )
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_public_url="https://app.example.com",
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = provider
            resp = client.get("/api/v1/auth/saml/okta-saml/metadata")
            assert resp.status_code == 200
            body = resp.text
            assert 'entityID="https://idp.okta.com/sp/abc123"' in body
            assert 'Location="https://app.example.com/api/v1/auth/saml/acs/okta-saml"' in body

    def test_per_provider_metadata_default_entity_id(self, client: TestClient) -> None:
        """GET /saml/{slug}/metadata uses default Entity ID when provider has none."""
        provider = _make_saml_provider(provider_id="okta-saml", entity_id=None)
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_public_url="https://app.example.com",
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = provider
            resp = client.get("/api/v1/auth/saml/okta-saml/metadata")
            assert resp.status_code == 200
            body = resp.text
            default_eid = "https://app.example.com/api/v1/auth/saml/okta-saml"
            assert f'entityID="{default_eid}"' in body
            assert 'Location="https://app.example.com/api/v1/auth/saml/acs/okta-saml"' in body

    def test_per_provider_metadata_unknown_slug_returns_404(self, client: TestClient) -> None:
        """GET /saml/{slug}/metadata returns uniform 404 for unknown slug."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = None
            resp = client.get("/api/v1/auth/saml/ghost-saml/metadata")
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Not Found"

    def test_per_provider_login_redirects_to_idp(self, client: TestClient) -> None:
        """GET /saml/{slug}/login resolves provider and redirects to IdP."""
        provider = _make_saml_provider(provider_id="okta-saml")
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_lookup.return_value = provider
            mock_auth.return_value = ("https://idp.okta.com/sso?SAMLRequest=xyz", "")
            resp = client.get("/api/v1/auth/saml/okta-saml/login", follow_redirects=False)
            assert resp.status_code == 307
            assert "idp.okta.com" in resp.headers["location"]
            # Verify provider_id was threaded through
            mock_auth.assert_awaited_once()
            call_kwargs = mock_auth.call_args
            assert call_kwargs.kwargs.get("provider_id") == "okta-saml"

    def test_per_provider_login_unknown_slug_returns_404(self, client: TestClient) -> None:
        """GET /saml/{slug}/login returns uniform 404 for unknown slug."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = None
            resp = client.get("/api/v1/auth/saml/ghost-saml/login", follow_redirects=False)
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Not Found"

    def test_no_preauth_route_401s_without_credentials(self, client: TestClient) -> None:
        """Pre-auth SAML per-provider routes must never 401 for missing Authorization."""
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = None
            # All three per-provider routes should 404 (not 401) when slug is unknown
            for path in [
                "/api/v1/auth/saml/ghost/metadata",
                "/api/v1/auth/saml/ghost/login",
            ]:
                resp = client.get(path, follow_redirects=False)
                assert resp.status_code != 401, f"{path} must not 401"

            resp = client.post(
                "/api/v1/auth/saml/ghost/acs",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code != 401, "acs must not 401"

    def test_two_orgs_each_with_saml_provider_different_metadata(self, client: TestClient) -> None:
        """Two providers: each /saml/{slug}/metadata emits its OWN Entity ID and ACS."""
        provider_a = _make_saml_provider(
            provider_id="okta-a",
            entity_id="https://idp-a.example.com/sp",
        )
        provider_b = _make_saml_provider(
            provider_id="okta-b",
            entity_id="https://idp-b.example.com/sp",
        )
        _override_settings(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_public_url="https://app.example.com",
        )

        def _lookup_by_slug(slug: str) -> MagicMock | None:
            if slug == "okta-a":
                return provider_a
            if slug == "okta-b":
                return provider_b
            return None

        async def _async_lookup(session: object, slug: str) -> MagicMock | None:
            return _lookup_by_slug(slug)

        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.side_effect = _async_lookup

            resp_a = client.get("/api/v1/auth/saml/okta-a/metadata")
            assert resp_a.status_code == 200
            assert 'entityID="https://idp-a.example.com/sp"' in resp_a.text
            assert 'Location="https://app.example.com/api/v1/auth/saml/acs/okta-a"' in resp_a.text

            resp_b = client.get("/api/v1/auth/saml/okta-b/metadata")
            assert resp_b.status_code == 200
            assert 'entityID="https://idp-b.example.com/sp"' in resp_b.text
            assert 'Location="https://app.example.com/api/v1/auth/saml/acs/okta-b"' in resp_b.text


class TestSamlProviderScoping:
    """Provider-scoped resolution: audience containment (FAR-1010) across providers."""

    def test_response_for_provider_a_rejected_at_provider_b(self) -> None:
        """A SAML response minted for provider A's entity_id is rejected at provider B.

        The _enforce_audience_restriction check in saml_handler.py enforces
        audience containment. When provider A's entity_id is not in the
        response's AudienceRestriction, the response is rejected — even if
        the signature is valid.

        We test _enforce_audience_restriction directly because python3-saml's
        own Destination check rejects the response before our audience check
        when Destination doesn't match the handler's ACS URL.
        """
        from modulo.auth.saml_handler import ModuloSamlAuth, SamlAuthError

        # Build a response whose audience is provider A's entity_id
        response_audience = "https://app.example.com/api/v1/auth/saml/okta-a"
        xml = _build_xsd_compliant_saml_response(audience=response_audience)
        encoded = base64.b64encode(xml.encode()).decode()

        # _enforce_audience_restriction rejects when entity_id not in audiences
        with pytest.raises(SamlAuthError, match="audience"):
            ModuloSamlAuth._enforce_audience_restriction(encoded, "https://app.example.com/api/v1/auth/saml/okta-b")

    def test_response_for_provider_a_accepted_at_provider_a(self) -> None:
        """A SAML response minted for provider A's entity_id is accepted at provider A."""
        from modulo.auth.saml_handler import ModuloSamlAuth

        response_audience = "https://app.example.com/api/v1/auth/saml/okta-a"
        xml = _build_xsd_compliant_saml_response(audience=response_audience)
        encoded = base64.b64encode(xml.encode()).decode()

        # entity_id matches the audience, so the containment check accepts the
        # response and returns None (it raises SamlAuthError on any mismatch).
        result = ModuloSamlAuth._enforce_audience_restriction(
            encoded, "https://app.example.com/api/v1/auth/saml/okta-a"
        )
        assert result is None


def _make_saml_db_provider(
    *,
    provider_id: str = "okta-saml",
    provider_type: str = "saml",
    enabled: bool = True,
    entity_id: str | None = "https://idp.example.com/sp",
) -> SimpleNamespace:
    """A minimal DB provider row for the auth-layer resolution helpers."""
    return SimpleNamespace(
        provider_id=provider_id,
        provider_type=provider_type,
        enabled=enabled,
        entity_id=entity_id,
        metadata_xml="<md:EntityDescriptor xmlns:md='urn:oasis:names:tc:SAML:2.0:metadata'/>",
        metadata_url=None,
    )


class TestResolveSamlConfigPerProvider:
    """``_resolve_saml_config(provider_id=...)`` resolution paths (FAR-1001)."""

    async def test_provider_id_resolves_via_system_session(self) -> None:
        """A matching slug is resolved through the system (BYPASSRLS) session."""
        from modulo.auth.sso import _resolve_saml_config

        provider = _make_saml_db_provider()
        settings = _override(modulo_public_url="https://app.example.com")
        system_session = _mock_session(scalar=provider)
        app_session = _mock_session()

        with patch("modulo.auth.sso._set_default_rls_org", new_callable=AsyncMock) as mock_default_org:
            idp_metadata, entity_id, _key, _cert, db_saml = await _resolve_saml_config(
                system_session, app_session, settings, provider_id="okta-saml"
            )

        assert db_saml is provider
        assert entity_id == "https://idp.example.com/sp"
        assert idp_metadata.startswith("<md:EntityDescriptor")
        mock_default_org.assert_not_awaited()

    async def test_provider_id_falls_back_to_app_session(self) -> None:
        """Without a system session, per-provider resolution uses the app session."""
        from modulo.auth.sso import _resolve_saml_config

        provider = _make_saml_db_provider()
        settings = _override(modulo_public_url="https://app.example.com")
        app_session = _mock_session(scalar=provider)

        with (
            patch("modulo.auth.sso._set_default_rls_org", new_callable=AsyncMock),
            patch("modulo.auth.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_get,
        ):
            mock_get.return_value = provider
            _idp, entity_id, _key, _cert, db_saml = await _resolve_saml_config(
                None, app_session, settings, provider_id="okta-saml"
            )

        assert db_saml is provider
        assert entity_id == "https://idp.example.com/sp"

    async def test_provider_id_defaults_entity_id_when_provider_has_none(self) -> None:
        """A per-provider DB row without entity_id defaults to its own SP URL."""
        from modulo.auth.sso import _resolve_saml_config

        provider = _make_saml_db_provider(entity_id=None)
        settings = _override(modulo_public_url="https://app.example.com")
        system_session = _mock_session(scalar=provider)

        with patch("modulo.auth.sso._set_default_rls_org", new_callable=AsyncMock):
            _idp, entity_id, _key, _cert, _db = await _resolve_saml_config(
                system_session, _mock_session(), settings, provider_id="okta-saml"
            )

        assert entity_id == "https://app.example.com/api/v1/auth/saml/okta-saml"

    async def test_read_by_slug_ignores_non_saml_provider(self) -> None:
        """A resolved non-SAML provider is rejected by the by-slug reader."""
        from modulo.auth.sso import _read_system_saml_provider_by_slug

        oidc = _make_saml_db_provider(provider_id="google", provider_type="oidc")
        session = _mock_session(scalar=oidc)

        result = await _read_system_saml_provider_by_slug(session, "google")

        assert result is None

    async def test_read_by_slug_none_without_system_session(self) -> None:
        """An unprovisioned system role yields no per-provider match."""
        from modulo.auth.sso import _read_system_saml_provider_by_slug

        assert await _read_system_saml_provider_by_slug(None, "okta-saml") is None


class TestPerProviderSamlRouteErrorPaths:
    """Error/fallback branches on the per-provider SAML routes (FAR-1001)."""

    def _provider(self) -> MagicMock:
        return _make_saml_provider(provider_id="okta-saml")

    def _post_acs(self, client: TestClient) -> Any:
        return client.post(
            "/api/v1/auth/saml/acs/okta-saml",
            data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
            follow_redirects=False,
        )

    def test_acs_missing_response_returns_400(self, client: TestClient) -> None:
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = self._provider()
            resp = client.post("/api/v1/auth/saml/acs/okta-saml", data={"SAMLResponse": ""}, follow_redirects=False)
        assert resp.status_code == 400

    def test_acs_value_error_returns_401(self, client: TestClient) -> None:
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = self._provider()
            mock_process.side_effect = ValueError("invalid response")
            resp = self._post_acs(client)
        assert resp.status_code == 401

    def test_acs_programming_error_returns_501(self, client: TestClient) -> None:
        from sqlalchemy.exc import ProgrammingError

        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = self._provider()
            mock_process.side_effect = ProgrammingError("select 1", {}, Exception("missing table"))
            resp = self._post_acs(client)
        assert resp.status_code == 501

    def test_acs_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = self._provider()
            mock_process.side_effect = SQLAlchemyError("db down")
            resp = self._post_acs(client)
        assert resp.status_code == 503

    def test_acs_http_exception_passthrough(self, client: TestClient) -> None:
        from fastapi import HTTPException

        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = self._provider()
            mock_process.side_effect = HTTPException(status_code=418, detail="teapot")
            resp = self._post_acs(client)
        assert resp.status_code == 418

    def test_acs_unexpected_error_returns_500(self, client: TestClient) -> None:
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = self._provider()
            mock_process.side_effect = RuntimeError("boom")
            resp = self._post_acs(client)
        assert resp.status_code == 500

    def test_login_value_error_returns_400(self, client: TestClient) -> None:
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_lookup.return_value = self._provider()
            mock_auth.side_effect = ValueError("bad config")
            resp = client.get("/api/v1/auth/saml/okta-saml/login", follow_redirects=False)
        assert resp.status_code == 400

    def test_login_programming_error_returns_501(self, client: TestClient) -> None:
        from sqlalchemy.exc import ProgrammingError

        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_lookup.return_value = self._provider()
            mock_auth.side_effect = ProgrammingError("select 1", {}, Exception("missing table"))
            resp = client.get("/api/v1/auth/saml/okta-saml/login", follow_redirects=False)
        assert resp.status_code == 501

    def test_login_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_lookup.return_value = self._provider()
            mock_auth.side_effect = SQLAlchemyError("db down")
            resp = client.get("/api/v1/auth/saml/okta-saml/login", follow_redirects=False)
        assert resp.status_code == 503

    def test_login_http_exception_passthrough(self, client: TestClient) -> None:
        from fastapi import HTTPException

        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_lookup.return_value = self._provider()
            mock_auth.side_effect = HTTPException(status_code=418, detail="teapot")
            resp = client.get("/api/v1/auth/saml/okta-saml/login", follow_redirects=False)
        assert resp.status_code == 418

    def test_login_unexpected_error_returns_500(self, client: TestClient) -> None:
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_lookup.return_value = self._provider()
            mock_auth.side_effect = RuntimeError("boom")
            resp = client.get("/api/v1/auth/saml/okta-saml/login", follow_redirects=False)
        assert resp.status_code == 500

    def test_metadata_unexpected_error_returns_500(self, client: TestClient) -> None:
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        with patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.side_effect = RuntimeError("boom")
            resp = client.get("/api/v1/auth/saml/okta-saml/metadata", follow_redirects=False)
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# SAML RelayState signing (FAR-1003)
# ---------------------------------------------------------------------------


class TestSamlRelayStateSigning:
    """sign_saml_relay_state / verify_saml_relay_state unit tests."""

    def test_sign_and_verify_returns_payload(self) -> None:
        from modulo.auth.sso import sign_saml_relay_state, verify_saml_relay_state

        signed = sign_saml_relay_state("okta-saml", _VALID_32)
        payload = verify_saml_relay_state(signed, _VALID_32)
        assert payload is not None
        assert payload["pid"] == "okta-saml"
        assert isinstance(payload["ts"], (int, float))

    def test_verify_tampered_returns_none(self) -> None:
        from modulo.auth.sso import sign_saml_relay_state, verify_saml_relay_state

        signed = sign_saml_relay_state("okta-saml", _VALID_32)
        assert verify_saml_relay_state(signed + "x", _VALID_32) is None

    def test_verify_wrong_key_returns_none(self) -> None:
        from modulo.auth.sso import sign_saml_relay_state, verify_saml_relay_state

        signed = sign_saml_relay_state("okta-saml", _VALID_32)
        assert verify_saml_relay_state(signed, "b" * 32) is None

    def test_verify_expired_returns_none(self) -> None:
        from modulo.auth.sso import sign_saml_relay_state, verify_saml_relay_state

        signed = sign_saml_relay_state("okta-saml", _VALID_32)
        # With max_age_seconds=0, anything is expired
        assert verify_saml_relay_state(signed, _VALID_32, max_age_seconds=0) is None

    def test_verify_malformed_returns_none(self) -> None:
        from modulo.auth.sso import verify_saml_relay_state

        assert verify_saml_relay_state("no-colon", _VALID_32) is None

    def test_verify_empty_returns_none(self) -> None:
        from modulo.auth.sso import verify_saml_relay_state

        assert verify_saml_relay_state("", _VALID_32) is None

    def test_different_providers_different_payloads(self) -> None:
        from modulo.auth.sso import sign_saml_relay_state, verify_saml_relay_state

        signed_a = sign_saml_relay_state("okta-a", _VALID_32)
        signed_b = sign_saml_relay_state("okta-b", _VALID_32)
        payload_a = verify_saml_relay_state(signed_a, _VALID_32)
        payload_b = verify_saml_relay_state(signed_b, _VALID_32)
        assert payload_a is not None
        assert payload_b is not None
        assert payload_a["pid"] == "okta-a"
        assert payload_b["pid"] == "okta-b"


# ---------------------------------------------------------------------------
# SAML RelayState route integration (FAR-1003)
# ---------------------------------------------------------------------------


class TestSamlRelayStateRouteIntegration:
    """Per-provider SAML routes: RelayState emitted on login, verified on ACS."""

    def test_login_emits_relay_state_signed_for_provider(self, client: TestClient) -> None:
        """GET /saml/{slug}/login passes a signed relay_state to saml_get_auth_url."""
        from modulo.auth.sso import verify_saml_relay_state

        provider = _make_saml_provider(provider_id="okta-saml")
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)
        settings = _override(modulo_license_key="lic-123", modulo_saml_enabled=True)

        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_get_auth_url", new_callable=AsyncMock) as mock_auth,
        ):
            mock_lookup.return_value = provider
            mock_auth.return_value = ("https://idp.okta.com/sso?SAMLRequest=xyz", "")
            resp = client.get("/api/v1/auth/saml/okta-saml/login", follow_redirects=False)
            assert resp.status_code == 307

            # Verify relay_state was passed to saml_get_auth_url
            call_kwargs = mock_auth.call_args
            relay_state = call_kwargs.kwargs.get("relay_state")
            assert relay_state is not None, "login must pass relay_state to saml_get_auth_url"
            # Verify it's a valid signed payload for this provider
            payload = verify_saml_relay_state(relay_state, settings.secret_key)
            assert payload is not None, "relay_state must be a valid signed token"
            assert payload["pid"] == "okta-saml"

    def test_acs_accepts_valid_signed_relay_state(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs accepts a valid signed RelayState for the right provider."""
        from modulo.auth.sso import sign_saml_relay_state

        provider = _make_saml_provider(provider_id="okta-saml")
        settings = _override(modulo_license_key="lic-123", modulo_saml_enabled=True)
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)

        relay_state = sign_saml_relay_state("okta-saml", settings.secret_key)

        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = provider
            mock_process.return_value = {
                "access_token": "at-rs",
                "refresh_token": "rt-rs",
                "token_type": "bearer",
            }
            resp = client.post(
                "/api/v1/auth/saml/acs/okta-saml",
                data={
                    "SAMLResponse": base64.b64encode(b"<saml/>").decode(),
                    "RelayState": relay_state,
                },
                follow_redirects=False,
            )
            assert resp.status_code == 307
            assert "access_token=at-rs" in resp.headers["location"]

    def test_acs_rejects_tampered_relay_state(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs rejects a tampered RelayState."""
        provider = _make_saml_provider(provider_id="okta-saml")
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)

        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = provider
            resp = client.post(
                "/api/v1/auth/saml/acs/okta-saml",
                data={
                    "SAMLResponse": base64.b64encode(b"<saml/>").decode(),
                    "RelayState": "tampered.payload.signature",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 401
            assert "RelayState" in resp.json()["detail"]
            mock_process.assert_not_awaited()

    def test_acs_rejects_relay_state_signed_for_different_provider(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs rejects a RelayState signed for a DIFFERENT provider."""
        from modulo.auth.sso import sign_saml_relay_state

        provider = _make_saml_provider(provider_id="okta-saml")
        settings = _override(modulo_license_key="lic-123", modulo_saml_enabled=True)
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)

        # Sign for a different provider
        relay_state = sign_saml_relay_state("different-provider", settings.secret_key)

        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = provider
            resp = client.post(
                "/api/v1/auth/saml/acs/okta-saml",
                data={
                    "SAMLResponse": base64.b64encode(b"<saml/>").decode(),
                    "RelayState": relay_state,
                },
                follow_redirects=False,
            )
            assert resp.status_code == 401
            assert "does not match" in resp.json()["detail"]
            mock_process.assert_not_awaited()

    def test_acs_proceeds_normally_when_relay_state_absent(self, client: TestClient) -> None:
        """POST /saml/{slug}/acs proceeds normally when RelayState is absent (IdP-initiated SSO).

        This is the critical regression guard: absence-tolerance is deliberate.
        """
        provider = _make_saml_provider(provider_id="okta-saml")
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)

        with (
            patch("modulo.api.routes.sso.get_provider_by_provider_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_lookup.return_value = provider
            mock_process.return_value = {
                "access_token": "at-idp",
                "refresh_token": "rt-idp",
                "token_type": "bearer",
            }
            # No RelayState in the form data
            resp = client.post(
                "/api/v1/auth/saml/acs/okta-saml",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 307
            assert "access_token=at-idp" in resp.headers["location"]
            mock_process.assert_awaited_once()

    def test_legacy_singleton_path_unchanged_no_relay_state(self, client: TestClient) -> None:
        """Legacy /saml/acs does NOT verify RelayState — preserves backward compatibility."""
        _override_settings(modulo_license_key="lic-123", modulo_saml_enabled=True)

        with (
            patch("modulo.api.routes.sso._get_enabled_saml_global", new_callable=AsyncMock),
            patch("modulo.api.routes.sso.saml_process_response", new_callable=AsyncMock) as mock_process,
        ):
            mock_process.return_value = {
                "access_token": "at-legacy",
                "refresh_token": "rt-legacy",
                "token_type": "bearer",
            }
            resp = client.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": base64.b64encode(b"<saml/>").decode()},
                follow_redirects=False,
            )
            assert resp.status_code == 307
            assert "access_token=at-legacy" in resp.headers["location"]
            # Legacy path doesn't pass relay_state arg
            call_kwargs = mock_process.call_args
            assert "relay_state" not in call_kwargs.kwargs
