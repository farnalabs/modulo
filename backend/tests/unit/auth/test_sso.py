"""SSO (OIDC + SAML) unit tests: state signing, provider parsing, JIT provisioning, routes."""

import base64
import json
import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import defusedxml.ElementTree as ElementTree
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.routes.sso import router as sso_router
from modulo.auth.secret_storage import encrypt_stored_secret
from modulo.auth.sso import (
    parse_oidc_providers,
    sign_state,
    verify_state,
)
from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32

# A real Fernet key (32 bytes base64url-encoded) — required by the
# encrypted-secret round-trip tests; _VALID_32 is used for Settings only.
_FERNET_KEY = base64.urlsafe_b64encode(b"a" * 32).decode()


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
    mock_session = AsyncMock(spec=AsyncSession)

    async def _override_session() -> AsyncMock:
        yield mock_session

    _app.dependency_overrides[get_settings] = lambda: _override()
    _app.dependency_overrides[get_db_session] = _override_session
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


def _fake_system_factory(session: AsyncMock) -> MagicMock:
    """Return a fake ``async_sessionmaker`` whose ``()`` yields ``session``.

    Mirrors the route's ``async with _new_system_session_factory()() as session``
    usage: calling the factory produces an async context manager that resolves
    to ``session``, and ``session.begin()`` (an ``AsyncMock``) supports ``async with``.
    """
    factory = MagicMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = False
    factory.return_value = cm
    return factory


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
        session = AsyncMock(spec=AsyncSession)

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
    def test_returns_oidc_providers(self, client: TestClient) -> None:
        system_session = AsyncMock(spec=AsyncSession)
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.list_oidc_providers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.api.routes.sso.count_oidc_providers", new_callable=AsyncMock, return_value=0),
        ):
            resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["oidc"]) == 2
        assert body["oidc"][0]["provider_id"] == "google"
        assert body["oidc"][1]["provider_id"] == "github"
        assert body["saml"] is False

    def test_returns_db_providers_first(self, client: TestClient) -> None:
        system_session = AsyncMock(spec=AsyncSession)
        db_providers = [
            {"provider_id": "okta", "client_id": "c", "client_secret": "s", "discovery_url": "u"},
        ]
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch(
                "modulo.api.routes.sso.list_oidc_providers",
                new_callable=AsyncMock,
                return_value=db_providers,
            ) as mock_list,
        ):
            resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        body = resp.json()
        assert body["oidc"] == [{"provider_id": "okta"}]
        mock_list.assert_awaited_once()
        list_args, _ = mock_list.await_args
        assert list_args[0] is system_session

    def test_no_env_fallback_when_all_db_providers_disabled(self, client: TestClient) -> None:
        """An all-disabled DB is authoritative — env providers must not reappear.

        ``list_oidc_providers`` returns ``[]`` both when the DB is empty and when
        every oidc row is ``enabled = False``. The env fallback must only fire
        for the former; an all-disabled DB renders no login providers.
        """
        system_session = AsyncMock(spec=AsyncSession)
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.list_oidc_providers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.api.routes.sso.count_oidc_providers", new_callable=AsyncMock, return_value=1),
        ):
            resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json()["oidc"] == []

    def test_saml_enabled_with_license(self, client: TestClient) -> None:
        system_session = AsyncMock(spec=AsyncSession)
        _override_settings(
            modulo_license_key="license-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_url="https://idp.example.com/metadata",
        )
        with (
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(system_session),
            ),
            patch("modulo.api.routes.sso.list_oidc_providers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.api.routes.sso.count_oidc_providers", new_callable=AsyncMock, return_value=0),
        ):
            resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 200
        assert resp.json()["saml"] is True

    def test_saml_disabled_without_license(self, client: TestClient) -> None:
        """SSO providers list returns 402 when license is absent."""
        _override_settings(modulo_license_key="", modulo_saml_enabled=True)
        resp = client.get("/api/v1/auth/sso/providers")
        assert resp.status_code == 402


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

        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA

            url, _req_id = await saml_get_auth_url(settings, "https://modulo.example.com/api/v1/auth/saml/acs")
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
        session = AsyncMock(spec=AsyncSession)
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
        session = AsyncMock(spec=AsyncSession)
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
        session = AsyncMock(spec=AsyncSession)
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
        session = AsyncMock(spec=AsyncSession)

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

            url, raw_state = await oidc_get_authorize_url("google", settings, "http://localhost/callback")

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
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
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
            await oidc_process_callback("code", "tampered-state", settings, session, "http://localhost/callback")

    async def test_raises_when_provider_not_found_after_state_check(self) -> None:
        from modulo.auth.sso import oidc_process_callback

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        signed = sign_state("ghost:state", settings.secret_key)

        with pytest.raises(ValueError, match="not found"):
            await oidc_process_callback("code", signed, settings, session, "http://localhost/callback")


# ---------------------------------------------------------------------------
# DB-backed OIDC providers — list_oidc_providers + login/callback resolution
# ---------------------------------------------------------------------------


def _make_db_row(
    name: str = "okta", client_id: str = "okta-client-id", secret: str = "okta-client-secret"
) -> MagicMock:
    row = MagicMock()
    row.name = name
    row.client_id = client_id
    row.client_secret = encrypt_stored_secret(secret, _FERNET_KEY)
    row.discovery_url = f"https://{name}.example.com/.well-known/openid-configuration"
    return row


def _statement_filters_on_enabled(stmt: object) -> bool:
    """True when the given select statement constrains the ``enabled`` column.

    ``list_oidc_providers`` / ``resolve_oidc_provider_org`` exclude disabled
    providers via a WHERE clause on ``SsoProvider.enabled``. Unit tests use a
    mocked session, so the SQL itself is never executed — this inspects the
    statement that would be sent to prove the filter is present.
    """
    where = getattr(stmt, "whereclause", None)
    if where is None:
        return False
    for cond in where.get_children():
        left = getattr(cond, "left", None)
        if left is not None and getattr(left, "name", "") == "enabled":
            return True
    return False


class TestListOidcProviders:
    async def test_resolves_provider_with_decrypted_secret(self) -> None:
        from modulo.auth.sso import list_oidc_providers

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalars().all.return_value = [_make_db_row()]
        session.execute.return_value = result
        org_id = uuid.uuid4()

        providers = await list_oidc_providers(session, org_id=org_id, fernet_key=_FERNET_KEY)

        assert providers == [
            {
                "provider_id": "okta",
                "client_id": "okta-client-id",
                "client_secret": "okta-client-secret",
                "discovery_url": "https://okta.example.com/.well-known/openid-configuration",
            }
        ]

    async def test_returns_empty_when_no_rows(self) -> None:
        from modulo.auth.sso import list_oidc_providers

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalars().all.return_value = []
        session.execute.return_value = result

        assert not await list_oidc_providers(session, org_id=uuid.uuid4(), fernet_key=_FERNET_KEY)

    async def test_org_agnostic_when_org_id_none(self) -> None:
        from modulo.auth.sso import list_oidc_providers

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalars().all.return_value = [_make_db_row(name="okta")]
        session.execute.return_value = result

        providers = await list_oidc_providers(session, org_id=None, fernet_key=_FERNET_KEY)

        assert [p["provider_id"] for p in providers] == ["okta"]

    async def test_legacy_plaintext_secret_passes_through(self) -> None:
        from modulo.auth.sso import list_oidc_providers

        row = MagicMock()
        row.name = "legacy"
        row.client_id = "cid"
        row.client_secret = b"plaintext-secret"
        row.discovery_url = "https://legacy.example.com/.well-known/openid-configuration"

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalars().all.return_value = [row]
        session.execute.return_value = result

        providers = await list_oidc_providers(session, org_id=uuid.uuid4(), fernet_key=_FERNET_KEY)

        assert providers[0]["client_secret"] == "plaintext-secret"

    async def test_excludes_disabled_providers(self) -> None:
        from modulo.auth.sso import list_oidc_providers

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalars().all.return_value = []
        session.execute.return_value = result

        await list_oidc_providers(session, org_id=None, fernet_key=_FERNET_KEY)

        stmt = session.execute.await_args.args[0]
        assert _statement_filters_on_enabled(stmt)


class TestResolveOidcProviderOrg:
    async def test_returns_org_of_db_provider(self) -> None:
        from modulo.auth.sso import resolve_oidc_provider_org

        org_id = uuid.uuid4()
        row = MagicMock()
        row.organisation_id = org_id
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        session = AsyncMock(spec=AsyncSession)
        session.execute.return_value = result

        assert await resolve_oidc_provider_org(session, "okta") == org_id

    async def test_returns_none_when_not_in_db(self) -> None:
        from modulo.auth.sso import resolve_oidc_provider_org

        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session = AsyncMock(spec=AsyncSession)
        session.execute.return_value = result

        assert await resolve_oidc_provider_org(session, "env-only-provider") is None

    async def test_returns_org_of_disabled_provider(self) -> None:
        """Disabled providers still resolve their org so the callback can route
        them into the DB path and block login instead of env-falling back."""
        from modulo.auth.sso import resolve_oidc_provider_org

        org_id = uuid.uuid4()
        row = MagicMock()
        row.organisation_id = org_id
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        session = AsyncMock(spec=AsyncSession)
        session.execute.return_value = result

        assert await resolve_oidc_provider_org(session, "okta") == org_id

        stmt = session.execute.await_args.args[0]
        assert not _statement_filters_on_enabled(stmt)


_OKTA_DB_PROVIDER: dict[str, str] = {
    "provider_id": "okta",
    "client_id": "okta-client-id",
    "client_secret": "okta-client-secret",
    "discovery_url": "https://okta.example.com/.well-known/openid-configuration",
}


def _id_token() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(b'{"email":"user@example.com","name":"Test User","sub":"abc123"}')
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


async def _run_authorize_url_test(
    provider_id: str,
    session: AsyncSession,
    org_id: uuid.UUID | None,
    db_provider: list[dict[str, str]],
) -> tuple[str, AsyncMock, Settings]:
    from modulo.auth.sso import oidc_get_authorize_url

    settings = _override()
    with (
        patch("modulo.auth.sso.list_oidc_providers", new_callable=AsyncMock) as mock_list,
        patch("modulo.auth.sso.count_oidc_providers", new_callable=AsyncMock) as mock_count,
        patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
    ):
        mock_list.return_value = db_provider
        mock_count.return_value = 1 if db_provider else 0
        mock_disc.return_value = {"authorization_endpoint": f"https://{provider_id}.example.com/oauth2/v1/authorize"}

        url, _ = await oidc_get_authorize_url(
            provider_id, settings, "http://localhost/callback", session=session, org_id=org_id
        )
    return url, mock_list, settings


async def _run_process_callback_test(
    provider_id: str,
    org_id: uuid.UUID | None,
    db_provider: list[dict[str, str]],
) -> tuple[dict[str, str], dict[str, AsyncMock], Settings, AsyncSession]:
    from modulo.auth.sso import oidc_process_callback, sign_state

    settings = _override()
    session = AsyncMock(spec=AsyncSession)
    signed = sign_state(f"{provider_id}:{'raw-state'}", settings.secret_key)

    with (
        patch("modulo.auth.sso.list_oidc_providers", new_callable=AsyncMock) as mock_list,
        patch("modulo.auth.sso.count_oidc_providers", new_callable=AsyncMock) as mock_count,
        patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
        patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
        patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
        patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
        patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
    ):
        mock_list.return_value = db_provider
        # The DB is authoritative whenever it has any oidc rows; an empty
        # enabled-list only falls back to env when the DB has ZERO rows.
        mock_count.return_value = 1 if db_provider else 0
        mock_disc.return_value = {
            "token_endpoint": f"https://{provider_id}.example.com/oauth2/v1/token",
            "jwks_uri": f"https://{provider_id}.example.com/oauth2/v1/keys",
            "issuer": f"https://{provider_id}.example.com",
        }
        mock_ex.return_value = {"id_token": _id_token()}
        mock_verify.return_value = {
            "email": "user@example.com",
            "name": "Test User",
            "sub": "abc123",
        }
        mock_jit.return_value = (MagicMock(), org_id, "runner")
        mock_tok.return_value = {
            "access_token": "at",
            "refresh_token": "rt",
            "token_type": "bearer",
        }

        result = await oidc_process_callback(
            "auth-code", signed, settings, session, "http://localhost/callback", org_id=org_id
        )

    return (
        result,
        {
            "mock_list": mock_list,
            "mock_disc": mock_disc,
            "mock_ex": mock_ex,
            "mock_verify": mock_verify,
            "mock_jit": mock_jit,
            "mock_tok": mock_tok,
        },
        settings,
        session,
    )


class TestOidcGetAuthorizeUrlDb:
    async def test_uses_db_provider_when_session_and_org_given(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        org_id = uuid.uuid4()
        url, mock_list, settings = await _run_authorize_url_test("okta", session, org_id, [_OKTA_DB_PROVIDER])
        assert url.startswith("https://okta.example.com/oauth2/v1/authorize")
        assert "client_id=okta-client-id" in url
        mock_list.assert_awaited_once_with(session, org_id=org_id, fernet_key=settings.fernet_key)

    async def test_uses_db_provider_org_agnostic_when_org_none(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        url, mock_list, settings = await _run_authorize_url_test("okta", session, None, [_OKTA_DB_PROVIDER])
        assert "client_id=okta-client-id" in url
        mock_list.assert_awaited_once_with(session, org_id=None, fernet_key=settings.fernet_key)

    async def test_falls_back_to_env_when_db_empty(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        url, mock_list, _settings = await _run_authorize_url_test("google", session, uuid.uuid4(), [])
        assert "client_id=google-client-id" in url
        mock_list.assert_awaited_once()

    async def test_no_env_fallback_when_db_has_rows_all_disabled(self) -> None:
        """Login must not resurrect env providers when the DB has oidc rows
        that are all disabled."""
        from modulo.auth.sso import oidc_get_authorize_url

        session = AsyncMock(spec=AsyncSession)
        with (
            patch("modulo.auth.sso.list_oidc_providers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.auth.sso.count_oidc_providers", new_callable=AsyncMock, return_value=1),
            pytest.raises(ValueError, match="not configured"),
        ):
            await oidc_get_authorize_url(
                "google", _override(), "http://localhost/callback", session=session, org_id=uuid.uuid4()
            )


class TestOidcProcessCallbackDb:
    async def test_uses_db_provider_when_org_id_given(self) -> None:
        org_id = uuid.uuid4()
        result, mocks, settings, session = await _run_process_callback_test("okta", org_id, [_OKTA_DB_PROVIDER])
        assert result["access_token"] == "at"
        call_args = mocks["mock_ex"].await_args.args
        assert call_args[1] == "okta-client-id"
        assert call_args[2] == "okta-client-secret"
        mocks["mock_jit"].assert_awaited_once_with(
            session,
            settings,
            "user@example.com",
            "Test User",
            "oidc",
            "okta:abc123",
            default_org_id=org_id,
        )

    async def test_falls_back_to_env_when_db_empty(self) -> None:
        org_id = uuid.uuid4()
        result, mocks, settings, session = await _run_process_callback_test("google", org_id, [])
        assert result["access_token"] == "at"
        call_args = mocks["mock_ex"].await_args.args
        assert call_args[1] == "google-client-id"
        assert call_args[2] == "google-client-secret"
        mocks["mock_list"].assert_awaited_once_with(session, org_id=org_id, fernet_key=settings.fernet_key)

    async def test_callback_no_env_fallback_when_db_all_disabled(self) -> None:
        """A disabled DB provider must not resolve from env in the callback.

        The callback resolves the provider's org (disabled providers still
        resolve their org) and routes into the DB path; ``list_oidc_providers``
        returns ``[]`` but the DB still has oidc rows, so there is no env
        fallback and the provider is treated as not found.
        """
        from modulo.auth.sso import oidc_process_callback, sign_state

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        provider_session = AsyncMock(spec=AsyncSession)
        begin_cm = AsyncMock()
        begin_cm.__aenter__.return_value = None
        begin_cm.__aexit__.return_value = False
        provider_session.begin.return_value = begin_cm
        org_id = uuid.uuid4()
        signed = sign_state("okta:raw-state", settings.secret_key)

        with (
            patch("modulo.auth.sso.list_oidc_providers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.auth.sso.count_oidc_providers", new_callable=AsyncMock, return_value=1),
            pytest.raises(ValueError, match="not found"),
        ):
            await oidc_process_callback(
                "auth-code",
                signed,
                settings,
                session,
                "http://localhost/callback",
                org_id=org_id,
                provider_session=provider_session,
            )

    async def test_group_mapping_lookup_runs_on_provider_session(self) -> None:
        """The group-mapping lookup must run on the BYPASSRLS provider session,
        not the RLS-blocked DI session — otherwise ``apply_group_mappings``
        silently never fires."""
        from modulo.auth.sso import oidc_process_callback, sign_state

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        provider_session = AsyncMock(spec=AsyncSession)
        begin_cm = AsyncMock()
        begin_cm.__aenter__.return_value = None
        begin_cm.__aexit__.return_value = False
        provider_session.begin.return_value = begin_cm
        org_id = uuid.uuid4()
        signed = sign_state("okta:raw-state", settings.secret_key)

        db_row = MagicMock()
        db_row.group_mappings = [{"idp_group": "admins", "team_id": str(uuid.uuid4()), "team_role": "admin"}]

        with (
            patch("modulo.auth.sso.list_oidc_providers", new_callable=AsyncMock) as mock_list,
            patch("modulo.auth.sso.count_oidc_providers", new_callable=AsyncMock) as mock_count,
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_mappings,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_list.return_value = [_OKTA_DB_PROVIDER]
            mock_count.return_value = 1
            mock_disc.return_value = {
                "token_endpoint": "https://okta.example.com/oauth2/v1/token",
                "jwks_uri": "https://okta.example.com/oauth2/v1/keys",
                "issuer": "https://okta.example.com",
            }
            mock_ex.return_value = {"id_token": _id_token()}
            mock_verify.return_value = {
                "email": "user@example.com",
                "name": "Test User",
                "sub": "abc123",
                "groups": ["admins"],
            }
            mock_jit.return_value = (MagicMock(), org_id, "runner")
            mock_lookup.return_value = db_row
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            await oidc_process_callback(
                "auth-code",
                signed,
                settings,
                session,
                "http://localhost/callback",
                org_id=org_id,
                provider_session=provider_session,
            )

        # The group-mapping lookup runs on the BYPASSRLS provider session.
        mock_lookup.assert_awaited_once()
        lookup_args, _ = mock_lookup.await_args
        assert lookup_args[0] is provider_session
        # Because the DB provider exists (with mappings), group mappings fire.
        mock_mappings.assert_awaited_once()


class TestApplyGroupMappingsRlsContext:
    async def test_sets_rls_org_on_di_session_before_write(self) -> None:
        """The group-mapping write must run with RLS org context on the DI session.

        ``team_memberships`` has the strict ``rls_org_isolation`` policy (no
        null-context escape), so the ``add_team_member`` INSERT violates WITH
        CHECK — and the whole callback 503s — unless ``set_rls_org`` has run on
        the DI session first. ``set_rls_org`` must be awaited BEFORE the write.
        """
        from modulo.auth.sso import apply_group_mappings

        session = AsyncMock(spec=AsyncSession)
        account = MagicMock()
        account.id = uuid.uuid4()
        org_id = uuid.uuid4()
        team_id = uuid.uuid4()
        mappings = [{"idp_group": "admins", "team_id": str(team_id), "team_role": "admin"}]

        order: list[str] = []

        async def _record_rls(*args: object, **kwargs: object) -> None:
            order.append("set_rls_org")

        async def _record_add(*args: object, **kwargs: object) -> None:
            order.append("add_team_member")

        with (
            patch("modulo.auth.sso.set_rls_org", new=AsyncMock(side_effect=_record_rls)) as mock_rls,
            patch("modulo.auth.sso.get_membership_by_team_and_account", new_callable=AsyncMock, return_value=None),
            patch("modulo.auth.sso.add_team_member", new=AsyncMock(side_effect=_record_add)) as mock_add,
        ):
            await apply_group_mappings(session, account, org_id, ["admins"], mappings)

        # RLS org context is set on the DI session for the resolved org.
        mock_rls.assert_awaited_once_with(session, org_id)
        # ...and it happens BEFORE the team_memberships write.
        assert order == ["set_rls_org", "add_team_member"]
        add_kwargs = mock_add.await_args.kwargs
        assert add_kwargs["org_id"] == org_id
        assert mock_add.await_args.args[0] is session

    async def test_skips_rls_setup_when_no_matching_groups(self) -> None:
        """With no matching IDP groups, no membership is written and the RLS
        context is still set — the org-scoped write never fires."""
        from modulo.auth.sso import apply_group_mappings

        session = AsyncMock(spec=AsyncSession)
        account = MagicMock()
        account.id = uuid.uuid4()
        org_id = uuid.uuid4()
        mappings = [{"idp_group": "admins", "team_id": str(uuid.uuid4()), "team_role": "admin"}]

        with (
            patch("modulo.auth.sso.set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch("modulo.auth.sso.get_membership_by_team_and_account", new_callable=AsyncMock) as mock_get,
            patch("modulo.auth.sso.add_team_member", new_callable=AsyncMock) as mock_add,
        ):
            await apply_group_mappings(session, account, org_id, ["engineering"], mappings)

        mock_rls.assert_awaited_once_with(session, org_id)
        mock_get.assert_not_awaited()
        mock_add.assert_not_awaited()


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

        settings = _override(modulo_license_key="", modulo_saml_enabled=True)
        with pytest.raises(ValueError, match="requires a license"):
            await saml_get_auth_url(settings, "http://localhost/acs")

    async def test_raises_when_no_metadata_source(self) -> None:
        from modulo.auth.sso import saml_get_auth_url

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
        )
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
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

        settings = _override(modulo_license_key="", modulo_saml_enabled=True)
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
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            with pytest.raises(ValueError, match="SAML response validation failed"):
                await saml_process_response(empty_response, settings, session)

    async def test_full_success_flow(self) -> None:
        from modulo.auth.saml_handler import ModuloSamlAuth
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
        )
        session = AsyncMock(spec=AsyncSession)

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

            result = await saml_process_response(encoded, settings, session)

            assert result["access_token"] == "at-saml"
            mock_jit.assert_awaited_once_with(
                session,
                settings,
                "user@example.com",
                "Test User",
                "saml",
                "saml:https://idp.example.com:user@example.com",
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
        session = AsyncMock(spec=AsyncSession)

        xml = self.SAML_RESPONSE_XML.replace(
            "<samlp:Response",
            '<samlp:Response Destination="https://evil.example.com/acs"',
        )
        encoded = base64.b64encode(xml.encode()).decode()

        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            with pytest.raises(ValueError, match="Destination does not match"):
                await saml_process_response(encoded, settings, session)


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
            patch(
                "modulo.api.routes.sso._new_system_session_factory",
                return_value=_fake_system_factory(AsyncMock(spec=AsyncSession)),
            ),
            patch("modulo.api.routes.sso.resolve_oidc_provider_org", new_callable=AsyncMock) as mock_resolve,
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_resolve.return_value = None
            mock_disc.return_value = {
                "token_endpoint": "https://oauth2.googleapis.com/token",
                "jwks_uri": "https://oauth2.googleapis.com/certs",
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
# Encrypted secret decryption — edge cases
# ---------------------------------------------------------------------------


class TestDecryptProviderSecret:
    def test_returns_empty_when_stored_none(self) -> None:
        from modulo.auth.sso import _decrypt_provider_secret

        assert _decrypt_provider_secret(None, _FERNET_KEY) == ""

    def test_returns_empty_when_no_fernet_key(self) -> None:
        from modulo.auth.sso import _decrypt_provider_secret

        # Legacy plaintext bytes can never decrypt without a key; the login flow
        # must not crash, so it falls back to an empty client_secret.
        assert _decrypt_provider_secret(b"some-bytes", None) == ""

    def test_returns_empty_on_decrypt_failure(self) -> None:
        """A corrupt/undeserialisable stored secret must not crash login — it
        falls back to an empty client_secret rather than raising."""
        from modulo.auth.secret_storage import encrypt_stored_secret
        from modulo.auth.sso import _decrypt_provider_secret

        token = encrypt_stored_secret("real-secret", _FERNET_KEY)
        wrong_key = base64.urlsafe_b64encode(b"b" * 32).decode()
        assert _decrypt_provider_secret(token, wrong_key) == ""


# ---------------------------------------------------------------------------
# count_oidc_providers — real body (excluded-disabled awareness)
# ---------------------------------------------------------------------------


class TestCountOidcProviders:
    async def test_counts_rows_org_agnostic(self) -> None:
        from modulo.auth.sso import count_oidc_providers

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = 3
        session.execute.return_value = result

        assert await count_oidc_providers(session, org_id=None) == 3

    async def test_counts_rows_org_scoped(self) -> None:
        from modulo.auth.sso import count_oidc_providers

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = 1
        session.execute.return_value = result
        org_id = uuid.uuid4()

        assert await count_oidc_providers(session, org_id=org_id) == 1

    async def test_returns_zero_when_scalar_none(self) -> None:
        from modulo.auth.sso import count_oidc_providers

        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = None
        session.execute.return_value = result

        assert await count_oidc_providers(session, org_id=None) == 0


# ---------------------------------------------------------------------------
# SAML group-mapping lookup — provider_session vs DI session
# ---------------------------------------------------------------------------


class TestSamlGroupMapping:
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
      <saml:Attribute Name="groups">
        <saml:AttributeValue>admins</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""

    async def test_group_mapping_lookup_runs_on_provider_session(self) -> None:
        """The group-mapping lookup must run on the BYPASSRLS provider session,
        not the RLS-blocked DI session — otherwise ``apply_group_mappings``
        silently never fires for the pre-auth ACS route."""
        from modulo.auth.saml_handler import ModuloSamlAuth
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
            modulo_public_url="https://app.example.com",
        )
        session = AsyncMock(spec=AsyncSession)
        provider_session = AsyncMock(spec=AsyncSession)
        begin_cm = AsyncMock()
        begin_cm.__aenter__.return_value = None
        begin_cm.__aexit__.return_value = False
        provider_session.begin.return_value = begin_cm

        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
        db_row = MagicMock()
        db_row.group_mappings = [{"idp_group": "admins", "team_id": str(uuid.uuid4()), "team_role": "admin"}]

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch.object(
                ModuloSamlAuth,
                "process_response",
                return_value={
                    "name_id": "user@example.com",
                    "attributes": {
                        "email": ["user@example.com"],
                        "displayName": ["Test User"],
                        "groups": ["admins"],
                    },
                },
            ),
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_mappings,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_lookup.return_value = db_row
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            await saml_process_response(encoded, settings, session, provider_session=provider_session)

        mock_lookup.assert_awaited_once()
        lookup_args, _ = mock_lookup.await_args
        assert lookup_args[0] is provider_session
        mock_mappings.assert_awaited_once()

    async def test_group_mapping_lookup_runs_on_session_when_no_provider_session(self) -> None:
        """When no provider session is supplied the group-mapping lookup falls
        back to the DI session (post-auth admin flows)."""
        from modulo.auth.saml_handler import ModuloSamlAuth
        from modulo.auth.sso import saml_process_response

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=self.SAMPLE_IDP_METADATA,
            modulo_public_url="https://app.example.com",
        )
        session = AsyncMock(spec=AsyncSession)

        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
        db_row = MagicMock()
        db_row.group_mappings = [{"idp_group": "admins", "team_id": str(uuid.uuid4()), "team_role": "admin"}]

        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
            patch.object(
                ModuloSamlAuth,
                "process_response",
                return_value={
                    "name_id": "user@example.com",
                    "attributes": {
                        "email": ["user@example.com"],
                        "displayName": ["Test User"],
                        "groups": ["admins"],
                    },
                },
            ),
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_mappings,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_fetch.return_value = self.SAMPLE_IDP_METADATA
            mock_jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            mock_lookup.return_value = db_row
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            await saml_process_response(encoded, settings, session)

        mock_lookup.assert_awaited_once()
        lookup_args, _ = mock_lookup.await_args
        assert lookup_args[0] is session
        mock_mappings.assert_awaited_once()


# ---------------------------------------------------------------------------
# OIDC callback group-mapping lookup — provider_session vs DI session (else branch)
# ---------------------------------------------------------------------------


class TestOidcCallbackGroupMappingNoProviderSession:
    async def test_group_mapping_lookup_runs_on_session_when_no_provider_session(self) -> None:
        """The provider_session branch is exercised elsewhere; this covers the
        fallback where no provider session is supplied (post-auth admin flow)
        and the group-mapping lookup runs on the DI session."""
        from modulo.auth.sso import oidc_process_callback, sign_state

        settings = _override()
        session = AsyncMock(spec=AsyncSession)
        org_id = uuid.uuid4()
        signed = sign_state("okta:raw-state", settings.secret_key)

        db_row = MagicMock()
        db_row.group_mappings = [{"idp_group": "admins", "team_id": str(uuid.uuid4()), "team_role": "admin"}]

        with (
            patch("modulo.auth.sso.list_oidc_providers", new_callable=AsyncMock) as mock_list,
            patch("modulo.auth.sso.count_oidc_providers", new_callable=AsyncMock) as mock_count,
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as mock_disc,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as mock_ex,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as mock_verify,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
            patch("modulo.auth.sso._lookup_provider_by_client_id", new_callable=AsyncMock) as mock_lookup,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_mappings,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
        ):
            mock_list.return_value = [_OKTA_DB_PROVIDER]
            mock_count.return_value = 1
            mock_disc.return_value = {
                "token_endpoint": "https://okta.example.com/oauth2/v1/token",
                "jwks_uri": "https://okta.example.com/oauth2/v1/keys",
                "issuer": "https://okta.example.com",
            }
            mock_ex.return_value = {"id_token": _id_token()}
            mock_verify.return_value = {
                "email": "user@example.com",
                "name": "Test User",
                "sub": "abc123",
                "groups": ["admins"],
            }
            mock_jit.return_value = (MagicMock(), org_id, "runner")
            mock_lookup.return_value = db_row
            mock_tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}

            await oidc_process_callback(
                "auth-code",
                signed,
                settings,
                session,
                "http://localhost/callback",
                org_id=org_id,
            )

        mock_lookup.assert_awaited_once()
        lookup_args, _ = mock_lookup.await_args
        assert lookup_args[0] is session
        mock_mappings.assert_awaited_once()
