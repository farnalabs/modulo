"""Step definitions for the SSO provider admin CRUD BDD scenarios (PRD 9.4).

Wires the scenarios in ``sso_admin_crud.feature`` to the real
``/api/v1/admin/sso`` routes (``modulo/api/routes/admin_sso.py``) through the
shared TestClient pattern of the BDD conftest. Only the DB CRUD, RLS and
outbound-network seams are patched at their route use-site: the routing,
``require_feature("sso")`` plan gate (through the real ``get_plan_context``
dependency), ``require_permission("sso.manage")`` org-role gate, break-glass
mint deny, request validation, the FAR-855 unrestricted-provisioning guard,
OIDC discovery parsing and SAML metadata parsing all run for real — so the
scenarios assert the actual API contract for the SSO provider lifecycle: 200
list with type badges, 201 OIDC/SAML create (client secret never echoed in the
clear, OIDC callback URL computed), 422 empty-allowed-domains / invalid-type
rejects, 409 duplicate name, 200 update / toggle, 400 empty update, 204 delete,
404 on a missing provider, 200 OIDC discovery + SAML metadata connection tests,
the group-mapping set/get surface, and 403 for a non-admin caller.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_OIDC_DISCOVERY = {
    "issuer": "https://accounts.example.com",
    "authorization_endpoint": "https://accounts.example.com/authorize",
    "token_endpoint": "https://accounts.example.com/token",
    "userinfo_endpoint": "https://accounts.example.com/userinfo",
    "jwks_uri": "https://accounts.example.com/jwks",
    "scopes_supported": ["openid", "profile", "email"],
}

_SAML_ENTITY_ID = "https://idp.okta.example.com"
_SAML_METADATA_XML = (
    '<?xml version="1.0"?>'
    f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{_SAML_ENTITY_ID}">'
    '  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
    '    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="https://idp.okta.example.com/sso"/>'
    '    <md:KeyDescriptor use="signing">'
    '      <md:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
    "        <ds:X509Data><ds:X509Certificate>MIIDazCCAlMCFQCu8R7F9ABC123def456ghi789jkl012"
    "        ABC</ds:X509Certificate></ds:X509Data>"
    "      </md:KeyInfo>"
    "    </md:KeyDescriptor>"
    "  </md:IDPSSODescriptor>"
    "</md:EntityDescriptor>"
)

scenarios("../features/auth/sso_admin_crud.feature")


def _provider_uuid(provider_id: str) -> uuid.UUID:
    """Deterministically map a feature-file provider id onto a UUID."""
    return uuid.uuid5(_ORG_ID, provider_id)


def _make_provider(**overrides: object) -> MagicMock:
    """SsoProvider-shaped mock matching ``SsoProviderResponse``'s validators.

    ``client_secret`` must be a real string (the response masker normalises it
    rather than raising on a generic mock), ``scopes`` a JSON-encodable value,
    and ``created_at`` / ``updated_at`` real datetimes so Pydantic round-trips.
    """
    p = MagicMock()
    p.id = overrides.get("id", _provider_uuid("prov-1"))
    p.provider_type = overrides.get("provider_type", "oidc")
    p.provider_id = overrides.get("provider_id", "google")
    p.name = overrides.get("name", "Google Workspace")
    p.client_id = overrides.get("client_id", "google-client-id")
    p.client_secret = overrides.get("client_secret", "configured")
    p.discovery_url = overrides.get(
        "discovery_url",
        "https://accounts.example.com/.well-known/openid-configuration",
    )
    p.metadata_url = overrides.get("metadata_url")
    p.metadata_xml = overrides.get("metadata_xml")
    p.entity_id = overrides.get("entity_id")
    p.scopes = overrides.get("scopes", json.dumps(["openid", "email", "profile"]))
    p.enabled = overrides.get("enabled", True)
    p.auto_provision = overrides.get("auto_provision", True)
    p.default_role = overrides.get("default_role", "runner")
    p.allowed_domains = overrides.get("allowed_domains", [])
    p.group_mappings = overrides.get("group_mappings", [])
    p.preset = overrides.get("preset", "custom")
    p.tenant_domain = None
    p.callback_url = None
    p.created_at = datetime(2025, 6, 1, tzinfo=UTC)
    p.updated_at = datetime(2025, 6, 1, tzinfo=UTC)
    return p


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {"providers": {}, "standard_patches": []}
    return cast("dict[str, Any]", request.node._ctx)


def _client(request: Any) -> Any:
    """Active principal client (set by the shared auth Given steps)."""
    stored = getattr(request.node, "_client", None)
    if stored is not None:
        return stored
    return request.getfixturevalue("client")


def _dispatch(request: Any, method: str, url: str, *, json: dict[str, Any] | None = None) -> None:
    ctx = _ctx(request)
    with ExitStack() as stack:
        for patcher in ctx.get("standard_patches", []):
            stack.enter_context(patcher)
        resp = _client(request).request(method, url, json=json)
    request.node._resp = resp


def _rls_patch() -> Any:
    return patch("modulo.api.routes.admin_sso.set_rls_org", new=AsyncMock())


def _body(request: Any) -> dict[str, Any]:
    resp = request.node._resp
    body = resp.json()
    assert isinstance(body, dict), f"Expected a JSON object body, got {type(body)}"
    return body


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("the SSO provider store has one OIDC provider and one SAML provider")
def _given_store_providers(request: Any) -> None:
    ctx = _ctx(request)
    ctx["providers"] = {
        "prov-1": _make_provider(id=_provider_uuid("prov-1"), name="Google Workspace", allowed_domains=["example.com"]),
        "prov-2": _make_provider(
            id=_provider_uuid("prov-2"),
            provider_type="saml",
            name="Okta",
            auto_provision=False,
            allowed_domains=["example.com"],
        ),
    }


@given(parsers.parse('an OIDC provider with id "{provider_id}" exists'))
def _given_oidc_provider(request: Any, provider_id: str) -> None:
    _ctx(request)["providers"][provider_id] = _make_provider(
        id=_provider_uuid(provider_id),
        name="Google Workspace",
        allowed_domains=["example.com"],
    )


@given(parsers.parse('a SAML provider with id "{provider_id}" and local metadata XML exists'))
def _given_saml_provider(request: Any, provider_id: str) -> None:
    _ctx(request)["providers"][provider_id] = _make_provider(
        id=_provider_uuid(provider_id),
        provider_type="saml",
        name="Okta SAML",
        metadata_xml=_SAML_METADATA_XML,
        auto_provision=False,
        allowed_domains=["example.com"],
    )


@given("the SSO provider store accepts a new provider")
def _given_store_accepts_provider(request: Any) -> None:
    ctx = _ctx(request)
    ctx["unrestricted"] = False

    async def _create(*_args: object, **_kwargs: object) -> MagicMock:
        return _make_provider(
            id=_provider_uuid("prov-new"),
            provider_type=str(_kwargs.get("provider_type", "oidc")),
            name=str(_kwargs.get("name", "Google Workspace")),
            provider_id=("google" if _kwargs.get("provider_type") == "oidc" else None),
            discovery_url=_kwargs.get("discovery_url"),
            metadata_url=_kwargs.get("metadata_url"),
            auto_provision=bool(_kwargs.get("auto_provision", False)),
            allowed_domains=_kwargs.get("allowed_domains") or [],
        )

    ctx["create_provider"] = AsyncMock(side_effect=_create)


@given("the SSO provider store accepts an unrestricted provider")
def _given_store_accepts_unrestricted(request: Any) -> None:
    ctx = _ctx(request)
    ctx["unrestricted"] = True

    async def _create(*_args: object, **_kwargs: object) -> MagicMock:
        return _make_provider(
            id=_provider_uuid("prov-new"),
            provider_type="oidc",
            name=str(_kwargs.get("name", "Open SSO")),
            provider_id="google",
            auto_provision=True,
            allowed_domains=[],
        )

    ctx["create_provider"] = AsyncMock(side_effect=_create)


@given("the unrestricted provisioning flag is off")
def _given_flag_off(request: Any) -> None:
    _ctx(request)["flag_off"] = True


@given("the SSO provider store rejects a duplicate provider name")
def _given_store_rejects_duplicate(request: Any) -> None:
    _ctx(request)["create_provider"] = AsyncMock(
        side_effect=ValueError("A provider with this name already exists"),
    )


@given("the OIDC discovery endpoint responds with a valid discovery document")
def _given_discovery_document(request: Any) -> None:
    _ctx(request)["discovery"] = _OIDC_DISCOVERY


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("I list the configured SSO providers")
def _when_list_providers(request: Any) -> None:
    ctx = _ctx(request)
    providers = list(ctx.get("providers", {}).values())
    ctx["standard_patches"] = [
        _rls_patch(),
        patch("modulo.api.routes.admin_sso.list_providers", new=AsyncMock(return_value=providers)),
    ]
    _dispatch(request, "GET", "/api/v1/admin/sso/providers")


@when(parsers.parse('I create the OIDC provider "{name}" with discovery URL "{discovery_url}"'))
def _when_create_oidc(request: Any, name: str, discovery_url: str) -> None:
    ctx = _ctx(request)
    payload: dict[str, Any] = {
        "provider_type": "oidc",
        "name": name,
        "client_id": "google-client-id",
        "client_secret": "google-client-secret",
        "discovery_url": discovery_url,
        "scopes": ["openid", "email", "profile"],
        "auto_provision": True,
        "default_role": "runner",
    }
    if not ctx.get("unrestricted"):
        payload["allowed_domains"] = ["example.com"]
    patches = [_rls_patch(), patch("modulo.api.routes.admin_sso.create_provider", new=ctx["create_provider"])]
    if "flag_off" in ctx:
        patches.append(
            patch(
                "modulo.api.routes.admin_sso.resolve_sso_unrestricted_provisioning",
                new=AsyncMock(return_value=not ctx["flag_off"]),
            )
        )
    ctx["standard_patches"] = patches
    _dispatch(request, "POST", "/api/v1/admin/sso/providers", json=payload)


@when(parsers.parse('I create the SAML provider "{name}" with metadata URL "{metadata_url}"'))
def _when_create_saml(request: Any, name: str, metadata_url: str) -> None:
    ctx = _ctx(request)
    payload = {
        "provider_type": "saml",
        "name": name,
        "metadata_url": metadata_url,
        "entity_id": "modulo",
        "auto_provision": False,
        "default_role": "runner",
    }
    if not ctx.get("unrestricted"):
        payload["allowed_domains"] = ["example.com"]
    ctx["standard_patches"] = [
        _rls_patch(),
        patch("modulo.api.routes.admin_sso.create_provider", new=ctx["create_provider"]),
    ]
    _dispatch(request, "POST", "/api/v1/admin/sso/providers", json=payload)


@when("I create an SSO provider with an invalid provider type")
def _when_create_invalid_type(request: Any) -> None:
    _ctx(request)["standard_patches"] = []
    _dispatch(
        request,
        "POST",
        "/api/v1/admin/sso/providers",
        json={"provider_type": "scim", "name": "Bad Type"},
    )


@when(parsers.parse('I rename the provider "{provider_id}" to "{new_name}"'))
def _when_rename_provider(request: Any, provider_id: str, new_name: str) -> None:
    ctx = _ctx(request)
    exists = provider_id in ctx.get("providers", {})
    returned = None
    if exists:
        returned = _make_provider(
            id=_provider_uuid(provider_id),
            name=new_name,
            auto_provision=False,
            allowed_domains=["example.com"],
        )
    ctx["standard_patches"] = [
        _rls_patch(),
        patch("modulo.api.routes.admin_sso.update_provider", new=AsyncMock(return_value=returned)),
    ]
    _dispatch(request, "PUT", f"/api/v1/admin/sso/providers/{_provider_uuid(provider_id)}", json={"name": new_name})


@when(parsers.parse('I update the provider "{provider_id}" with an empty body'))
def _when_update_empty(request: Any, provider_id: str) -> None:
    _ctx(request)["standard_patches"] = []
    _dispatch(request, "PUT", f"/api/v1/admin/sso/providers/{_provider_uuid(provider_id)}", json={})


@when(parsers.parse('I toggle the provider "{provider_id}"'))
def _when_toggle_provider(request: Any, provider_id: str) -> None:
    ctx = _ctx(request)
    ctx["standard_patches"] = [
        _rls_patch(),
        patch(
            "modulo.api.routes.admin_sso.toggle_provider",
            new=AsyncMock(
                return_value=_make_provider(
                    id=_provider_uuid(provider_id),
                    name="Google Workspace",
                    enabled=False,
                    allowed_domains=["example.com"],
                )
            ),
        ),
    ]
    _dispatch(request, "PUT", f"/api/v1/admin/sso/providers/{_provider_uuid(provider_id)}/toggle")


@when(parsers.parse('I delete the provider "{provider_id}"'))
def _when_delete_provider(request: Any, provider_id: str) -> None:
    ctx = _ctx(request)
    deleted = provider_id in ctx.get("providers", {})
    ctx["standard_patches"] = [
        _rls_patch(),
        patch("modulo.api.routes.admin_sso.delete_provider", new=AsyncMock(return_value=deleted)),
    ]
    _dispatch(request, "DELETE", f"/api/v1/admin/sso/providers/{_provider_uuid(provider_id)}")


@when(parsers.parse('I test the connection for the provider "{provider_id}"'))
def _when_test_connection(request: Any, provider_id: str) -> None:
    ctx = _ctx(request)
    provider = ctx["providers"][provider_id]
    patches = [
        _rls_patch(),
        patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=provider)),
    ]
    if provider.provider_type == "oidc":
        discovery = ctx.get("discovery", _OIDC_DISCOVERY)
        patches.extend(
            [
                patch("modulo.api.routes.admin_sso.validate_outbound_url_async", new=AsyncMock()),
                patch(
                    "modulo.api.routes.admin_sso.pinned_async_client",
                    new=AsyncMock(return_value=_make_pinned_discovery(discovery)),
                ),
            ]
        )
    ctx["standard_patches"] = patches
    _dispatch(request, "POST", f"/api/v1/admin/sso/providers/{_provider_uuid(provider_id)}/test")


@when(parsers.parse('I set the group-to-team mappings for the provider "{provider_id}"'))
def _when_set_group_mappings(request: Any, provider_id: str) -> None:
    ctx = _ctx(request)
    mappings = [
        {
            "idp_group": "engineering",
            "team_id": "00000000-0000-0000-0000-000000000020",
            "team_role": "operator",
        },
        {"idp_group": "viewers", "team_id": "00000000-0000-0000-0000-000000000030", "team_role": "viewer"},
    ]
    provider = _make_provider(
        id=_provider_uuid(provider_id),
        name="Google Workspace",
        group_mappings=mappings,
        allowed_domains=["example.com"],
    )
    ctx["provider"] = provider
    ctx["mappings"] = mappings
    ctx["standard_patches"] = [
        _rls_patch(),
        patch("modulo.api.routes.admin_sso.set_group_mappings", new=AsyncMock(return_value=provider)),
    ]
    _dispatch(
        request,
        "PUT",
        f"/api/v1/admin/sso/providers/{_provider_uuid(provider_id)}/group-mappings",
        json={"mappings": mappings},
    )


@when(parsers.parse('I retrieve the group-to-team mappings for the provider "{provider_id}"'))
def _when_get_group_mappings(request: Any, provider_id: str) -> None:
    ctx = _ctx(request)
    provider = ctx.get("provider") or _make_provider(
        id=_provider_uuid(provider_id),
        name="Google Workspace",
        group_mappings=ctx.get("mappings", []),
        allowed_domains=["example.com"],
    )
    ctx["standard_patches"] = [
        _rls_patch(),
        patch("modulo.api.routes.admin_sso.get_provider", new=AsyncMock(return_value=provider)),
    ]
    _dispatch(request, "GET", f"/api/v1/admin/sso/providers/{_provider_uuid(provider_id)}/group-mappings")


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the provider list contains the OIDC provider and the SAML provider")
def _then_list_contains(request: Any) -> None:
    data = request.node._resp.json()
    entries = {item["name"]: item for item in data}
    assert set(entries) == {"Google Workspace", "Okta"}, f"unexpected provider list {list(entries)}"
    assert entries["Google Workspace"]["provider_type"] == "oidc"
    assert entries["Okta"]["provider_type"] == "saml"


@then("the created provider is an OIDC provider with a callback URL")
def _then_created_oidc(request: Any) -> None:
    data = _body(request)
    assert data["provider_type"] == "oidc"
    callback = data.get("callback_url") or ""
    assert callback.endswith("/api/v1/auth/oidc/google/callback"), f"unexpected callback URL {callback!r}"


@then("the created provider does not echo the client secret")
def _then_no_secret_echo(request: Any) -> None:
    assert "google-client-secret" not in request.node._resp.text


@then("the created provider is a SAML provider")
def _then_created_saml(request: Any) -> None:
    assert _body(request)["provider_type"] == "saml"


@then("the provider reports enabled false")
def _then_provider_disabled(request: Any) -> None:
    assert _body(request)["enabled"] is False


@then("the connection test reports success")
def _then_connection_success(request: Any) -> None:
    assert _body(request)["success"] is True


@then("the discovered OIDC endpoints include an authorization endpoint")
def _then_discovery_endpoint(request: Any) -> None:
    info = _body(request)["provider_info"]
    assert "authorization_endpoint" in info
    assert "token_endpoint" in info


@then("the parsed SAML metadata exposes an entity id")
def _then_saml_metadata(request: Any) -> None:
    info = _body(request)["provider_info"]
    assert info["entity_id"] == _SAML_ENTITY_ID
    assert info["sso_url"] == "https://idp.okta.example.com/sso"


@then("the mapping response contains the group-to-team mappings")
def _then_mappings(request: Any) -> None:
    mappings = _body(request)["mappings"]
    assert len(mappings) == 2, f"expected two mappings, got {mappings}"
    assert mappings[0]["idp_group"] == "engineering"
    assert mappings[0]["team_role"] == "operator"


def _make_pinned_discovery(discovery: dict[str, Any]) -> MagicMock:
    """Fake ``pinned_async_client`` context manager serving a discovery doc."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=discovery)
    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=http_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm
