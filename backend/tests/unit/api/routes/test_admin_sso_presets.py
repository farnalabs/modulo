"""Tests for SSO preset admin routes (FAR-853).

Covers:
- GET /api/v1/admin/sso/presets returns the registry
- Response includes callback_url for OIDC providers
- Provider response contains preset fields
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from modulo.api.routes.admin_sso import (
    _provider_response,
    list_presets_endpoint,
)
from modulo.auth.jwt import TenantPrincipal

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _admin_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_ADMIN_ID,
        org_role="admin",
    )


class _AutobeginAwareSession:
    """Fake session whose execute() requires an explicit begin() first."""

    def __init__(self, *, scalars_all: list[object] | None = None) -> None:
        self._in_tx = False
        self._scalars_all = scalars_all if scalars_all is not None else []
        self.info: dict[object, object] = {}

    def begin(self) -> _BeginCtx:
        return _BeginCtx(self)

    def in_transaction(self) -> bool:
        return self._in_tx

    async def execute(self, stmt: object, *args: object) -> MagicMock:
        assert self._in_tx, "execute() ran outside session.begin() (autobegin=False)"
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.scalars.return_value.all.return_value = self._scalars_all
        return result


class _BeginCtx:
    def __init__(self, session: _AutobeginAwareSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        self._session._in_tx = True

    async def __aexit__(self, *_exc: object) -> bool:
        self._session._in_tx = False
        return False


def _make_provider_model(
    *,
    provider_id: str = "google",
    name: str = "Google OIDC",
    provider_type: str = "oidc",
    preset: str = "custom",
    tenant_domain: str | None = None,
    discovery_url: str | None = None,
    scopes: list[str] | None = None,
    client_id: str = "client-123",
    client_secret: bytes | None = b"secret",
    auto_provision: bool = False,
    default_role: str = "runner",
    allowed_domains: list[str] | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.provider_id = provider_id
    p.name = name
    p.provider_type = provider_type
    p.enabled = True
    p.organisation_id = _ORG_ID
    p.preset = preset
    p.tenant_domain = tenant_domain
    p.discovery_url = discovery_url
    p.scopes = scopes
    p.client_id = client_id
    p.client_secret = client_secret
    p.auto_provision = auto_provision
    p.default_role = default_role
    p.allowed_domains = allowed_domains or []
    p.metadata_url = None
    p.metadata_xml = None
    p.entity_id = None
    p.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    p.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    p.group_mappings = []
    # Prevent MagicMock auto-attribute from producing a non-None callback_url
    p.callback_url = None
    return p


# ---------------------------------------------------------------------------
# list_presets_endpoint
# ---------------------------------------------------------------------------


class TestListPresetsEndpoint:
    async def test_returns_all_presets(self) -> None:
        result = await list_presets_endpoint(_=None, current_user=_admin_principal())
        ids = {p.id for p in result}
        assert "custom" in ids
        assert "google" in ids
        assert "auth0" in ids
        assert "okta" in ids
        assert "azure-ad" in ids
        assert "onelogin" in ids

    async def test_tenant_presets_marked(self) -> None:
        result = await list_presets_endpoint(_=None, current_user=_admin_principal())
        by_id = {p.id: p for p in result}
        assert by_id["google"].requires_tenant is False
        assert by_id["auth0"].requires_tenant is True
        assert by_id["okta"].requires_tenant is True
        assert by_id["azure-ad"].requires_tenant is True
        assert by_id["onelogin"].requires_tenant is True
        assert by_id["custom"].requires_tenant is False


# ---------------------------------------------------------------------------
# _provider_response helper
# ---------------------------------------------------------------------------


class TestProviderResponseHelper:
    def test_oidc_response_callback_url(self) -> None:
        provider = _make_provider_model(provider_id="my-sso", preset="custom")
        resp = _provider_response(provider)
        assert resp.callback_url is not None
        assert "my-sso" in resp.callback_url

    def test_saml_no_callback_url(self) -> None:
        provider = _make_provider_model(provider_type="saml", provider_id=None, preset="custom")
        resp = _provider_response(provider)
        assert resp.callback_url is None

    def test_preset_fields_in_response(self) -> None:
        provider = _make_provider_model(preset="okta", tenant_domain="example.okta.com")
        resp = _provider_response(provider)
        assert resp.preset == "okta"
        assert resp.tenant_domain == "example.okta.com"
        assert resp.callback_url is not None
