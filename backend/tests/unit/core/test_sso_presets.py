"""Tests for the SSO preset registry (FAR-853)."""

import pytest

from modulo.core.sso_presets import (
    PRESETS,
    get_preset,
    list_presets,
    resolve_preset,
)


class TestGetPreset:
    def test_custom_preset(self) -> None:
        p = get_preset("custom")
        assert p is not None
        assert p.id == "custom"
        assert p.requires_tenant is False
        assert p.discovery_url_template is None
        assert not p.scopes

    def test_google_preset(self) -> None:
        p = get_preset("google")
        assert p is not None
        assert p.label == "Google"
        assert p.requires_tenant is False
        assert p.discovery_url_template == "https://accounts.google.com/.well-known/openid-configuration"
        assert p.scopes == ["openid", "email", "profile"]

    def test_auth0_preset(self) -> None:
        p = get_preset("auth0")
        assert p is not None
        assert p.requires_tenant is True
        assert p.tenant_label == "Auth0 domain"

    def test_okta_preset(self) -> None:
        p = get_preset("okta")
        assert p is not None
        assert p.requires_tenant is True
        assert p.tenant_label == "Okta domain"

    def test_azure_ad_preset(self) -> None:
        p = get_preset("azure-ad")
        assert p is not None
        assert p.requires_tenant is True
        assert p.tenant_label == "Directory (tenant) ID"

    def test_onelogin_preset(self) -> None:
        p = get_preset("onelogin")
        assert p is not None
        assert p.requires_tenant is True
        assert p.tenant_label == "OneLogin subdomain"

    def test_unknown_preset_returns_none(self) -> None:
        assert get_preset("nonexistent") is None


class TestListPresets:
    def test_returns_all_presets(self) -> None:
        presets = list_presets()
        assert len(presets) == len(PRESETS)
        ids = {p["id"] for p in presets}
        assert ids == set(PRESETS.keys())

    def test_metadata_fields(self) -> None:
        presets = list_presets()
        for p in presets:
            assert "id" in p
            assert "label" in p
            assert "requires_tenant" in p
            assert "tenant_label" in p


class TestResolvePreset:
    def test_custom_returns_none(self) -> None:
        url, scopes = resolve_preset("custom")
        assert url is None
        assert scopes is None

    def test_google_derives_url_and_scopes(self) -> None:
        url, scopes = resolve_preset("google")
        assert url == "https://accounts.google.com/.well-known/openid-configuration"
        assert scopes == ["openid", "email", "profile"]

    def test_auth0_with_tenant(self) -> None:
        url, scopes = resolve_preset("auth0", "mycompany.auth0.com")
        assert url == "https://mycompany.auth0.com/.well-known/openid-configuration"
        assert scopes == ["openid", "profile", "email"]

    def test_okta_with_tenant(self) -> None:
        url, _scopes = resolve_preset("okta", "mycompany.okta.com")
        assert url == "https://mycompany.okta.com/.well-known/openid-configuration"

    def test_azure_ad_with_tenant(self) -> None:
        url, _scopes = resolve_preset("azure-ad", "my-tenant-id")
        assert url == "https://login.microsoftonline.com/my-tenant-id/v2.0/.well-known/openid-configuration"

    def test_onelogin_with_tenant(self) -> None:
        url, _scopes = resolve_preset("onelogin", "mycompany")
        assert url == "https://mycompany.onelogin.com/oidc/2/.well-known/openid-configuration"

    def test_unknown_preset_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown SSO preset"):
            resolve_preset("nonexistent")

    def test_tenant_preset_without_tenant_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a tenant_domain"):
            resolve_preset("auth0")

    def test_tenant_preset_with_empty_tenant_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a tenant_domain"):
            resolve_preset("auth0", "  ")

    def test_google_ignores_tenant(self) -> None:
        """Non-tenant presets ignore the tenant_domain argument."""
        url, scopes = resolve_preset("google", "ignored.example.com")
        assert url == "https://accounts.google.com/.well-known/openid-configuration"
        assert scopes == ["openid", "email", "profile"]
