"""Native SSO provider presets (FAR-853).

Each preset derives ``discovery_url`` and ``scopes`` server-side so the admin
never has to hand-type them for well-known IdPs.  Tenant-presets require a
``tenant_domain`` value to interpolate into the discovery URL.

The registry is intentionally flat and data-only — no I/O, no DB access.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SsoPreset:
    """Metadata for a single SSO provider preset."""

    id: str
    label: str
    requires_tenant: bool
    tenant_label: str
    discovery_url_template: str | None
    scopes: list[str]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PRESETS: dict[str, SsoPreset] = {
    "custom": SsoPreset(
        id="custom",
        label="Custom",
        requires_tenant=False,
        tenant_label="",
        discovery_url_template=None,
        scopes=[],
    ),
    "google": SsoPreset(
        id="google",
        label="Google",
        requires_tenant=False,
        tenant_label="",
        discovery_url_template="https://accounts.google.com/.well-known/openid-configuration",
        scopes=["openid", "email", "profile"],
    ),
    "auth0": SsoPreset(
        id="auth0",
        label="Auth0",
        requires_tenant=True,
        tenant_label="Auth0 domain",
        discovery_url_template="https://{tenant}/.well-known/openid-configuration",
        scopes=["openid", "profile", "email"],
    ),
    "okta": SsoPreset(
        id="okta",
        label="Okta",
        requires_tenant=True,
        tenant_label="Okta domain",
        discovery_url_template="https://{tenant}/.well-known/openid-configuration",
        scopes=["openid", "profile", "email"],
    ),
    "azure-ad": SsoPreset(
        id="azure-ad",
        label="Microsoft",
        requires_tenant=True,
        tenant_label="Directory (tenant) ID",
        discovery_url_template="https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
        scopes=["openid", "profile", "email"],
    ),
    "onelogin": SsoPreset(
        id="onelogin",
        label="OneLogin",
        requires_tenant=True,
        tenant_label="OneLogin subdomain",
        discovery_url_template="https://{tenant}.onelogin.com/oidc/2/.well-known/openid-configuration",
        scopes=["openid", "profile", "email"],
    ),
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_preset(preset_id: str) -> SsoPreset | None:
    """Return the preset for *preset_id*, or ``None`` if unknown."""
    return PRESETS.get(preset_id)


def list_presets() -> list[dict[str, str | bool]]:
    """Return registry metadata safe for the admin API response."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "requires_tenant": p.requires_tenant,
            "tenant_label": p.tenant_label,
        }
        for p in PRESETS.values()
    ]


def resolve_preset(preset_id: str, tenant_domain: str | None = None) -> tuple[str | None, list[str] | None]:
    """Resolve ``discovery_url`` and ``scopes`` for a preset.

    Returns ``(discovery_url, scopes)``.  For ``custom`` both are ``None``
    (the admin supplies them).  Raises ``ValueError`` when:
    - *preset_id* is unknown
    - a tenant preset is used without *tenant_domain*
    """
    preset = get_preset(preset_id)
    if preset is None:
        msg = f"Unknown SSO preset: {preset_id!r}"
        raise ValueError(msg)

    if preset.id == "custom":
        return None, None

    if preset.requires_tenant:
        if not tenant_domain or not tenant_domain.strip():
            msg = f"Preset {preset.id!r} requires a tenant_domain"
            raise ValueError(msg)
        template = preset.discovery_url_template
        assert template is not None
        discovery_url = template.format(tenant=tenant_domain.strip())
    else:
        assert preset.discovery_url_template is not None
        discovery_url = preset.discovery_url_template

    return discovery_url, list(preset.scopes)
