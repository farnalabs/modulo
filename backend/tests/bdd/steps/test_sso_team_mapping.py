"""Step definitions for SSO Group-to-Team Mapping — admin config, JIT application, role assignment."""

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
    scenarios("../features/auth/sso_team_mapping.feature")

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
        modulo_oidc_providers=json.dumps(
            [
                {
                    "provider_id": "google",
                    "client_id": "google-client-id",
                    "client_secret": "google-client-secret",
                    "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                },
            ]
        ),
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
        values = "".join(f"        <saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
        groups_xml = f'      <saml:Attribute Name="groups">\n{values}\n      </saml:Attribute>'
    xml = (
        _SAML_RESPONSE_XML.replace("__EMAIL__", email)
        .replace("__DISPLAY_NAME__", display_name)
        .replace("__GROUPS_XML__", groups_xml)
    )
    return base64.b64encode(xml.encode()).decode()


def _make_id_token(email: str, name: str, groups: list[str] | None = None, sub: str = "abc123") -> str:
    claims = {"email": email, "name": name, "sub": sub}
    if groups:
        claims["groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.signature"


def _sign_state(provider_id: str, secret_key: str = _VALID_32) -> str:
    from modulo.auth.sso import sign_state

    return sign_state(f"{provider_id}:{uuid.uuid4().hex}", secret_key)


def _setup_plan(license_key: str = "test-license-key") -> None:
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
    _app.dependency_overrides[get_plan_context] = lambda: _plan


def _setup_oidc_client(license_key: str = "test-license-key") -> None:
    from modulo.api.main import app as _app

    _app.dependency_overrides[get_settings] = lambda: _oidc_settings(license_key)
    _setup_plan(license_key)
    get_settings.cache_clear()


def _setup_saml_client(license_key: str = "test-license-key") -> None:
    from modulo.api.main import app as _app

    _app.dependency_overrides[get_settings] = lambda: _saml_settings(license_key)
    _setup_plan(license_key)
    get_settings.cache_clear()


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


# ── Background ────────────────────────────────────────────────────────────


@given('I am authenticated as an admin in org "org-1"')
def _bdd_auth_admin() -> None:
    """No-op — the ``client`` fixture already provides an admin principal."""


# ── Given: SSO provider existence ─────────────────────────────────────────


@given(parsers.parse('an SSO provider exists with id "{provider_id}"'))
def sso_provider_exists(provider_id: str, ctx: dict[str, Any]) -> None:
    ctx["provider_id"] = provider_id


@given(parsers.parse('an SSO provider with id "{provider_id}" has group mappings configured'))
def sso_provider_has_mappings(provider_id: str, ctx: dict[str, Any]) -> None:
    ctx["provider_id"] = provider_id


# ── Given: OIDC / SAML setup ──────────────────────────────────────────────


@given("OIDC providers are configured")
def oidc_providers_configured(ctx: dict[str, Any]) -> None:
    _setup_oidc_client()
    ctx["auth_type"] = "oidc"


@given(parsers.parse('SAML 2.0 is enabled with provider "{entity_id}"'))
def saml_enabled(entity_id: str, ctx: dict[str, Any]) -> None:
    ctx["entity_id"] = entity_id
    _setup_saml_client()
    ctx["auth_type"] = "saml"


# ── Given: group mapping config ────────────────────────────────────────────


@given(parsers.parse('group mapping is configured for "{idp_group}" to team "{team_id}" with role "{role}"'))
def group_mapping_configured(idp_group: str, team_id: str, role: str, ctx: dict[str, Any]) -> None:
    mappings = ctx.get("group_mappings", [])
    mappings.append({"idp_group": idp_group, "team_id": team_id, "team_role": role})
    ctx["group_mappings"] = mappings


# ── Given: user state for JIT ──────────────────────────────────────────────


@given(parsers.parse('a first-time OIDC user with email "{email}"'))
def first_time_oidc_user(email: str, ctx: dict[str, Any]) -> None:
    ctx["expected_email"] = email
    ctx["expected_name"] = email.split("@", maxsplit=1)[0]
    ctx["is_new_user"] = True
    ctx["auth_type"] = "oidc"


@given(parsers.parse('a first-time SAML user with email "{email}"'))
def first_time_saml_user(email: str, ctx: dict[str, Any]) -> None:
    ctx["expected_email"] = email
    ctx["expected_name"] = email.split("@", maxsplit=1)[0]
    ctx["is_new_user"] = True
    ctx["auth_type"] = "saml"


# ── When: admin configures group mapping ───────────────────────────────────


@when(parsers.parse('I set group mappings for provider "{provider_id}"'))
def set_group_mappings(provider_id: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    ctx["provider_id"] = provider_id
    pid = _PROVIDER_UUID if provider_id == "prov-1" else provider_id

    mappings = ctx.get("group_mappings", [])
    if not mappings:
        mappings = [{"idp_group": "engineering", "team_id": "team-1", "team_role": "operator"}]

    mock_provider = MagicMock()
    mock_provider.group_mappings = mappings

    with patch("modulo.api.routes.admin_sso.set_group_mappings", new_callable=AsyncMock) as mock_set:
        mock_set.return_value = mock_provider
        resp = client.put(
            f"/api/v1/admin/sso/providers/{pid}/group-mappings",
            json={"mappings": mappings},
        )
        _store_response(request, ctx, resp)
        ctx["mock_set"] = mock_set
        ctx["stored_mappings"] = mappings


@when(parsers.parse('I GET group mappings for provider "{provider_id}"'))
def get_group_mappings(provider_id: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    ctx["provider_id"] = provider_id
    pid = _PROVIDER_UUID if provider_id == "prov-1" else provider_id

    stored = [
        {"idp_group": "engineering", "team_id": "team-1", "team_role": "operator"},
    ]
    mock_provider = MagicMock()
    mock_provider.group_mappings = stored

    with patch("modulo.api.routes.admin_sso.get_provider", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_provider
        resp = client.get(f"/api/v1/admin/sso/providers/{pid}/group-mappings")
        _store_response(request, ctx, resp)
        ctx["stored_mappings"] = stored


# ── When: OIDC callback with IdP groups ────────────────────────────────────


@when(
    parsers.parse('the OIDC callback returns a valid code with IdP groups "{groups}"'),
)
def oidc_callback_with_groups(groups: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_oidc_client()
    settings = _oidc_settings()
    signed = _sign_state("google", settings.secret_key)
    email = ctx.get("expected_email", "newuser@example.com")
    name = ctx.get("expected_name", email.split("@")[0])
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    id_token = _make_id_token(email, name, groups=group_list)

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
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            "issuer": "https://accounts.google.com",
        }
        mock_ex.return_value = {"id_token": id_token}
        mock_verify.return_value = {
            "email": email,
            "name": name,
            "sub": "abc123",
            "groups": group_list,
        }

        user_mock = MagicMock()
        user_mock.email = email
        user_mock.id = uuid.uuid4()
        user_mock.organisation_id = _ORG_ID
        user_mock.org_role = "runner"
        mock_jit.return_value = (user_mock, _ORG_ID, "runner")

        provider_mock = MagicMock()
        provider_mock.group_mappings = ctx.get("group_mappings", [])
        should_lookup = bool(ctx.get("group_mappings"))
        mock_lookup.return_value = provider_mock if should_lookup else None

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
        ctx["mock_apply"] = mock_apply
        ctx["mock_jit"] = mock_jit
        ctx["input_groups"] = group_list


# ── When: SAML ACS with IdP groups ────────────────────────────────────────


@when(
    parsers.parse('the SAML ACS endpoint receives a SAMLResponse with groups "{groups}"'),
)
def saml_acs_with_groups(groups: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_saml_client()
    email = ctx.get("expected_email", "newuser@example.com")
    name = ctx.get("expected_name", email.split("@")[0])
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    encoded = _make_saml_response(email, name, groups=group_list)

    with (
        patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
        patch("modulo.auth.sso.ModuloSamlAuth") as mock_handler,
        patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
        patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as mock_lookup,
        patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as mock_apply,
        patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
    ):
        mock_fetch.return_value = _SAMPLE_IDP_METADATA
        mock_handler.return_value.process_response.return_value = {
            "name_id": email,
            "attributes": {
                "email": [email],
                "displayName": [name],
                "groups": group_list,
            },
        }

        user_mock = MagicMock()
        user_mock.email = email
        user_mock.id = uuid.uuid4()
        user_mock.organisation_id = _ORG_ID
        user_mock.org_role = "runner"
        mock_jit.return_value = (user_mock, _ORG_ID, "runner")

        provider_mock = MagicMock()
        provider_mock.group_mappings = ctx.get("group_mappings", [])
        should_lookup = bool(ctx.get("group_mappings"))
        mock_lookup.return_value = provider_mock if should_lookup else None

        mock_tok.return_value = {
            "access_token": "at-saml-test",
            "refresh_token": "rt-saml-test",
            "token_type": "bearer",
        }

        resp = client.post(
            "/api/v1/auth/saml/acs",
            data={"SAMLResponse": encoded},
            follow_redirects=False,
        )
        _store_response(request, ctx, resp)
        ctx["mock_apply"] = mock_apply
        ctx["mock_jit"] = mock_jit
        ctx["input_groups"] = group_list


# ── Then: assertions ──────────────────────────────────────────────────────


@then("the group mappings are persisted")
def group_mappings_persisted(ctx: dict[str, Any]) -> None:
    mock_set = ctx.get("mock_set")
    assert mock_set is not None, "No mock_set reference found in context"
    mock_set.assert_awaited_once()


@then(parsers.parse("the response contains {count:d} mapping entry"))
def response_contains_mapping_entry(count: int, request: Any) -> None:
    resp = request.node._resp
    data = resp.json()
    assert len(data["mappings"]) == count, f"Expected {count} mapping entries, got {len(data['mappings'])}"


@then("apply_group_mappings was called")
def apply_group_mappings_called(ctx: dict[str, Any]) -> None:
    mock_apply = ctx.get("mock_apply")
    assert mock_apply is not None, "No mock_apply reference found in context"
    mock_apply.assert_awaited_once()


@then(parsers.parse('apply_group_mappings was called with groups "{groups}"'))
def apply_group_mappings_called_with(groups: str, ctx: dict[str, Any]) -> None:
    mock_apply = ctx.get("mock_apply")
    assert mock_apply is not None, "No mock_apply reference found in context"
    mock_apply.assert_awaited_once()
    call = mock_apply.await_args
    assert call is not None
    input_groups = [g.strip() for g in groups.split(",") if g.strip()]
    actual_groups = call[0][3]
    assert actual_groups == input_groups, f"Expected groups {input_groups}, got {actual_groups}"


@then(
    parsers.parse('the mapping assigns role "{expected_role}" for the matched group'),
)
def mapping_assigns_role(expected_role: str, ctx: dict[str, Any]) -> None:
    mock_apply = ctx.get("mock_apply")
    assert mock_apply is not None, "No mock_apply reference found in context"
    mock_apply.assert_awaited_once()
    call = mock_apply.await_args
    assert call is not None
    group_mappings = call[0][4]
    assert len(group_mappings) > 0, "Expected at least one mapping"
    assert group_mappings[0]["team_role"] == expected_role, (
        f"Expected role {expected_role}, got {group_mappings[0]['team_role']}"
    )


@then("apply_group_mappings had no matching groups")
def apply_group_mappings_no_matches(ctx: dict[str, Any]) -> None:
    mock_apply = ctx.get("mock_apply")
    assert mock_apply is not None, "No mock_apply reference found in context"
    mock_apply.assert_awaited_once()
    call = mock_apply.await_args
    assert call is not None
    idp_groups = call[0][3]
    group_mappings = call[0][4]
    matched = any(m["idp_group"] in idp_groups for m in group_mappings)
    assert not matched, f"Expected no matching groups, but found match for {idp_groups} in {group_mappings}"
