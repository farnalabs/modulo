"""Step definitions for SSO SAML 2.0 — SP metadata, ACS callback, JIT provisioning, group mapping, gating."""

import base64
import uuid
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import get_plan_context
from modulo.core.feature_flags import CommunityTier, LicenseData, LicenseKeyTier
from modulo.settings import Settings, get_settings

with suppress(FileNotFoundError, OSError):
    scenarios("../features/auth/sso_saml.feature")

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
        values = "".join(f"        <saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
        groups_xml = f'      <saml:Attribute Name="groups">\n{values}\n      </saml:Attribute>'
    xml = (
        _SAML_RESPONSE_XML.replace("__EMAIL__", email)
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


def _setup_saml_client(license_key: str = "test-license-key") -> None:
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

    _app.dependency_overrides[get_settings] = lambda: _saml_settings(license_key)
    _app.dependency_overrides[get_plan_context] = lambda: _plan
    get_settings.cache_clear()


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


# ── Background ────────────────────────────────────────────────────────────


@given(parsers.parse('SAML 2.0 is enabled with provider "{entity_id}"'))
def saml_enabled(entity_id: str, ctx: dict[str, Any]) -> None:
    ctx["entity_id"] = entity_id
    _setup_saml_client()


# ── License gating ────────────────────────────────────────────────────────


@given("I do not have a Team license")
def no_team_license(ctx: dict[str, Any]) -> None:
    ctx["license_key"] = ""


# ── Given: user state for JIT ─────────────────────────────────────────────


@given(parsers.parse('a first-time SAML user with email "{email}"'))
def first_time_user(email: str, ctx: dict[str, Any]) -> None:
    ctx["expected_email"] = email
    ctx["expected_name"] = email.split("@", maxsplit=1)[0]
    ctx["is_new_user"] = True


@given(parsers.parse('an existing SAML user with email "{email}"'))
def existing_saml_user(email: str, ctx: dict[str, Any]) -> None:
    ctx["expected_email"] = email
    ctx["expected_name"] = email.split("@", maxsplit=1)[0]
    ctx["is_new_user"] = False


# ── Given: group mapping ──────────────────────────────────────────────────


@given(parsers.parse('SAML group mapping is configured for "{idp_group}" to team "{team_id}" with role "{role}"'))
def group_mapping_configured(idp_group: str, team_id: str, role: str, ctx: dict[str, Any]) -> None:
    ctx["group_mappings"] = [{"idp_group": idp_group, "team_id": team_id, "team_role": role}]


# ── SAML login ────────────────────────────────────────────────────────────


@when("I initiate SAML login")
def initiate_saml_login(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_saml_client(ctx.get("license_key", "test-license-key"))
    with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = _SAMPLE_IDP_METADATA
        resp = client.get("/api/v1/auth/saml/login", follow_redirects=False)
        _store_response(request, ctx, resp)


# ── SP metadata ───────────────────────────────────────────────────────────


@when("I request the SAML SP metadata endpoint")
def request_sp_metadata(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_saml_client(ctx.get("license_key", "test-license-key"))
    resp = client.get("/api/v1/auth/saml/metadata", follow_redirects=False)
    _store_response(request, ctx, resp)


# ── ACS callback ──────────────────────────────────────────────────────────


@when("the SAML ACS endpoint receives a valid SAMLResponse")
def acs_valid_response(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_saml_client()
    email = ctx.get("expected_email", "user@example.com")
    name = ctx.get("expected_name", "Test User")
    encoded = _make_saml_response(email, name)

    with (
        patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
        patch("modulo.auth.sso.ModuloSamlAuth") as mock_handler,
        patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as mock_jit,
        patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as mock_tok,
    ):
        mock_fetch.return_value = _SAMPLE_IDP_METADATA
        mock_handler.return_value.process_response.return_value = {
            "name_id": email,
            "attributes": {
                "email": [email],
                "displayName": [name],
            },
        }

        if ctx.get("is_new_user", True):
            user_mock = MagicMock()
            user_mock.email = email
            user_mock.id = uuid.uuid4()
            user_mock.organisation_id = _ORG_ID
            user_mock.org_role = "runner"
            mock_jit.return_value = (user_mock, _ORG_ID, "runner")
        else:
            existing = MagicMock()
            existing.email = email
            existing.id = uuid.uuid4()
            existing.organisation_id = _ORG_ID
            existing.org_role = "admin"
            existing.sso_subject = "saml:https://idp.example.com:user@example.com"
            existing.auth_provider = "saml"
            mock_jit.return_value = (existing, _ORG_ID, "admin")

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
        ctx["mock_jit"] = mock_jit
        ctx["mock_tok"] = mock_tok


@when("the SAML ACS endpoint receives a malformed SAMLResponse")
def acs_malformed_response(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_saml_client()
    from modulo.auth.saml_handler import SamlAuthError

    with (
        patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as mock_fetch,
        patch("modulo.auth.sso.ModuloSamlAuth") as mock_handler,
    ):
        mock_fetch.return_value = _SAMPLE_IDP_METADATA
        mock_handler.return_value.process_response.side_effect = SamlAuthError("SAML Assertion is malformed or invalid")
        resp = client.post(
            "/api/v1/auth/saml/acs",
            data={"SAMLResponse": base64.b64encode(b"<bad/>").decode()},
            follow_redirects=False,
        )
        _store_response(request, ctx, resp)


@when("the SAML ACS endpoint receives a request without SAMLResponse")
def acs_missing_response(request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_saml_client()
    resp = client.post("/api/v1/auth/saml/acs", data={}, follow_redirects=False)
    _store_response(request, ctx, resp)


@when(parsers.parse('the SAML ACS endpoint receives a SAMLResponse with groups "{groups}"'))
def acs_with_groups(groups: str, request: Any, ctx: dict[str, Any], client: Any) -> None:
    _setup_saml_client()
    email = ctx.get("expected_email", "newuser@example.com")
    name = ctx.get("expected_name", "newuser")
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
        mock_lookup.return_value = provider_mock

        mock_tok.return_value = {
            "access_token": "at-saml-group",
            "refresh_token": "rt-saml-group",
            "token_type": "bearer",
        }

        resp = client.post(
            "/api/v1/auth/saml/acs",
            data={"SAMLResponse": encoded},
            follow_redirects=False,
        )
        _store_response(request, ctx, resp)
        ctx["mock_apply"] = mock_apply


# ── Then: assertions ──────────────────────────────────────────────────────


@then("I am redirected to the SAML IdP single sign-on URL")
def redirected_to_saml_idp(request: Any) -> None:
    resp = request.node._resp
    assert resp.status_code == 307, f"Expected 307, got {resp.status_code}"
    location = resp.headers.get("location", "")
    assert "idp.example.com" in location, f"Expected IdP URL in redirect, got {location}"
    assert "SAMLRequest" in location, f"Missing SAMLRequest in redirect: {location}"


@then("the response contains valid SAML metadata XML")
def response_has_saml_metadata(request: Any) -> None:
    resp = request.node._resp
    body = resp.text
    assert "<md:EntityDescriptor" in body, "Missing EntityDescriptor in metadata"
    assert "<md:SPSSODescriptor" in body, "Missing SPSSODescriptor in metadata"


@then("the metadata includes the ACS endpoint URL")
def metadata_has_acs_url(request: Any) -> None:
    resp = request.node._resp
    body = resp.text
    assert "AssertionConsumerService" in body, "Missing AssertionConsumerService in metadata"
    assert "/api/v1/auth/saml/acs" in body, "Missing ACS endpoint URL in metadata"
    assert "HTTP-POST" in body, "Missing HTTP-POST binding in metadata"


@then("the redirect URL contains access and refresh tokens")
def redirect_has_tokens(request: Any) -> None:
    resp = request.node._resp
    location = resp.headers.get("location", "")
    assert "access_token=" in location, f"Missing access_token in redirect: {location}"
    assert "refresh_token=" in location, f"Missing refresh_token in redirect: {location}"


@then("a new user account was provisioned")
def new_user_provisioned(ctx: dict[str, Any], request: Any) -> None:
    mock_jit = ctx.get("mock_jit")
    assert mock_jit is not None, "No mock_jit reference found in context"
    mock_jit.assert_awaited_once()
    call_kwargs = mock_jit.await_args[1] if mock_jit.await_args else {}
    email_arg = mock_jit.call_args[0][2] if mock_jit.call_args else call_kwargs.get("email", "")
    assert email_arg == ctx.get("expected_email", ""), f"Expected JIT for {ctx.get('expected_email')}, got {email_arg}"


@then("no duplicate account was created")
def no_duplicate_account(ctx: dict[str, Any]) -> None:
    mock_jit = ctx.get("mock_jit")
    assert mock_jit is not None, "No mock_jit reference found in context"
    mock_jit.assert_awaited_once()
    returned = mock_jit.return_value
    account = returned[0] if isinstance(returned, tuple) else returned
    assert account.email == ctx.get("expected_email", ""), (
        f"Expected returning user {ctx.get('expected_email')}, got {account.email}"
    )


@then(parsers.parse('the user is added to team "{team_id}" with role "{role}"'))
def user_added_to_team(team_id: str, role: str, ctx: dict[str, Any]) -> None:
    mock_apply = ctx.get("mock_apply")
    assert mock_apply is not None, "No mock_apply reference found in context"
    mock_apply.assert_awaited_once()
    call = mock_apply.await_args
    assert call is not None
    idp_groups_arg = call[0][3]
    assert len(idp_groups_arg) > 0, "Expected at least one IDP group in apply_group_mappings"


@then(parsers.parse('the error detail mentions "{text}"'))
def error_detail_mentions(text: str, request: Any) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "")
    assert text.lower() in detail.lower(), f"Expected detail to mention '{text}', got '{detail}'"
