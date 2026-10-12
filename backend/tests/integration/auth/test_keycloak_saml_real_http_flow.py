"""FAR-1126: the SAML SSO HTTP flow against a REAL Keycloak container.

Tier 1b (light): the REAL per-provider SAML code path drives a REAL
Keycloak-issued assertion round-trip, including the SP-initiated redirect,
login-form walk and the ACS form postback. The seeded provider row exercises
the ``metadata_url`` branch ``_saml_config_from_db`` takes (the IdP metadata
is fetched live over HTTPS with the SSRF loopback consent) — not the
already-downloaded ``metadata_xml`` shortcut the stub suites cover.

The suite REPLACES the httpx/xmlsec-stub coverage for:

- the IdP metadata the SP binds to is Keycloak's own descriptor fetched on
  the wire;
- the assertion is a REAL ``saml:Response`` signed by Keycloak with real
  x509 key material (python3-saml + xmlsec verify against it, not a test
  fixture's synthesized XML);
- the RelayState token handed to the IdP is the REAL
  ``sign_saml_relay_state`` HMAC and is verified on the way back through
  ``saml_acs_provider``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import async_sessionmaker as _asm

# ── module-scoped SSRF opt-in for the REAL pinned-transport code path ──────
from tests.helpers.tier1b_backends import (  # noqa: F401 — imported fixture, autouse via module namespace
    ssrf_loopback_consent,
)
from tests.integration.auth._keycloak_harness import (
    HARNESS_FRONTEND_BASE,
    HARNESS_PUBLIC_BASE,
    KC_USER_PASSWORD,
    IdpBrowserDriver,
    walk_saml_postback,
)

pytestmark = pytest.mark.integration

_PUBLIC_BASE = HARNESS_PUBLIC_BASE
_FRONTEND_BASE = HARNESS_FRONTEND_BASE
_CORS_WITH_WRONG_FIRST = f"https://marketing.example.com,{_PUBLIC_BASE}"
_SECRET_KEY = "a" * 32
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


# ---------------------------------------------------------------------------
# Seed helpers (session engine — bypasses RLS for setup).
# ---------------------------------------------------------------------------


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"KC SAML {org_id.hex[:8]}", "slug": f"kcsaml-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_inviter(engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(acc_id), "email": f"owner-{acc_id.hex[:8]}@example.com", "name": "Owner"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(acc_id), "oid": str(org_id)},
        )
    return acc_id


async def _create_saml_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    provider_id: str,
    entity_id: str,
    metadata_url: str,
    default_role: str = "viewer",
) -> uuid.UUID:
    """Seed a SAML provider row WITHOUT ``metadata_xml``, forcing the REAL
    runtime IdP-descriptor fetch (metadata_url -> SSRF-validated HTTPS fetch
    inside ``_saml_config_from_db``)."""
    import datetime as dt
    import json

    created_at = dt.datetime.now(dt.UTC)
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, entity_id, "
                "metadata_url, enabled, auto_provision, "
                "allowed_domains, default_role, group_mappings, preset, created_at) "
                "VALUES (:id, :oid, 'saml', 'Real Keycloak SAML', :pid, :eid, "
                ":murl, true, true, CAST(:domains AS json), :role, '[]'::json, 'custom', :created_at) "
                "RETURNING id"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "pid": provider_id,
                "eid": entity_id,
                "murl": metadata_url,
                "domains": json.dumps(["idp-test.local"]),
                "role": default_role,
                "created_at": created_at,
            },
        )
    return uuid.UUID(str(result.scalar_one()))


async def _get_account_by_email(engine: AsyncEngine, email: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT id, email, display_name, auth_provider FROM accounts WHERE email = :email"),
                    {"email": email},
                )
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


async def _get_membership_role(
    engine: AsyncEngine,
    account_id: Any,
    org_id: uuid.UUID,
) -> str | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT role FROM org_memberships WHERE account_id = :aid "
                        "AND organisation_id = :oid AND deactivated_at IS NULL"
                    ),
                    {"aid": str(account_id), "oid": str(org_id)},
                )
            )
            .mappings()
            .first()
        )
    return str(row["role"]) if row else None


async def _get_sso_subject(engine: AsyncEngine, account_id: Any) -> dict[str, Any] | None:
    """Read the SSO subject binding the ACS JIT-provision wrote to the account.

    The real storage is ``accounts.sso_subject`` + ``accounts.auth_provider``
    (see ``modulo.auth.sso._upsert_sso_identity``) — there is no separate
    identities table.
    """
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT sso_subject, auth_provider FROM accounts WHERE id = :aid"),
                    {"aid": str(account_id)},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# App overrides + client (mirrors the OIDC module's shape)
# ---------------------------------------------------------------------------


class _AllFeaturesPlan:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list[Any]:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


async def _all_features_ctx() -> Any:
    return _AllFeaturesPlan()


def _build_settings() -> Any:
    from modulo.settings import Settings

    return Settings(
        database_url="postgresql+asyncpg://localhost/modulo_unused",
        secret_key=_SECRET_KEY,
        fernet_key=_FERNET_KEY,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
        modulo_public_url=_PUBLIC_BASE,
        modulo_frontend_url=_FRONTEND_BASE,
        cors_origins=_CORS_WITH_WRONG_FIRST,
    )


def _make_saml_client(app: Any, app_engine: AsyncEngine) -> httpx.AsyncClient:
    from modulo.api.dependencies import _get_engine, get_anonymous_plan_context, get_db_session
    from modulo.settings import get_settings

    app.dependency_overrides[get_settings] = lambda: _build_settings()
    app.dependency_overrides[_get_engine] = lambda: app_engine

    factory = _asm(app_engine, expire_on_commit=False)

    async def override_session() -> AsyncGenerator[Any, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_anonymous_plan_context] = _all_features_ctx

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url=_PUBLIC_BASE, timeout=30.0)


# ---------------------------------------------------------------------------
# Session-scoped seeding (runs once; the flow under test is deterministic).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def saml_seed(
    keycloak_tenants: tuple[Any, Any],
    db_engine: AsyncEngine,
) -> AsyncIterator[dict[str, Any]]:
    """Seed the org, inviter and per-provider DB row for the SAML flow."""
    from modulo.api.main import app

    saml_tenant = keycloak_tenants[1]
    org_id = await _create_org(db_engine)
    await _create_inviter(db_engine, org_id)
    # Use the TENANT's provider_id — the Impl-side SP registration derived
    # sp_entity_id/acs_url under exactly this pid, so the route URLs match.
    provider_id = saml_tenant.provider_id
    await _create_saml_provider(
        db_engine,
        org_id=org_id,
        provider_id=provider_id,
        # Explicit SP entity_id — matches the IdP-side SAML client we created
        # in the harness's bootstrap_saml_realm.
        entity_id=saml_tenant.sp_entity_id,
        # Forces the REAL metadata_url fetch path in _saml_config_from_db.
        metadata_url=saml_tenant.descriptor_url,
    )
    try:
        yield {
            "org_id": org_id,
            "provider_id": provider_id,
            "saml_tenant": saml_tenant,
        }
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def keycloak_driver(keycloak_cert_dir: Any, keycloak_world: Any) -> IdpBrowserDriver:
    return IdpBrowserDriver(idp_base=keycloak_world.idp_base, ca_file=keycloak_cert_dir / "ca.pem")


@pytest.mark.real_ssrf_dns
class TestRealKeycloakSamlHttpFlow:
    """Real-IdP SAML SSO coverage on the REAL HTTP path.

    Proves end-to-end:
    1. the per-provider SAML login route hands the browser a REAL
       Keycloak SSO redirect (AuthnRequest over the metadata we fetched);
    2. the driven browser walk returns a REAL signed SAMLResponse;
    3. the ACS route verifies the assertion (real x509) + the HMAC RelayState,
       JIT-provisions the account and the viewer membership, and delivers the
       tokens with the FAR-1197 shape (refresh in httpOnly cookie only).
    """

    @pytest.mark.asyncio
    async def test_real_keycloak_saml_acs_flow(
        self,
        saml_seed: dict[str, Any],
        app_engine: AsyncEngine,
        keycloak_driver: IdpBrowserDriver,
    ) -> None:
        from modulo.api.main import app

        provider_id = saml_seed["provider_id"]
        tenant = saml_seed["saml_tenant"]
        org_id = saml_seed["org_id"]

        client = _make_saml_client(app, app_engine)
        try:
            # 1. The per-provider login route redirects to the REAL IdP.
            login_resp = await client.get(f"/api/v1/auth/saml/{provider_id}/login")
            assert login_resp.status_code == 307, login_resp.text
            sso_url = login_resp.headers["location"]
            assert sso_url.startswith(tenant.idp_base), sso_url

            # 2. The REAL browser walk drives the real Keycloak login UI and
            # captures the IdP's signed SAMLResponse + the RelayState handed
            # back through the auto-submitting postback form.
            postback = walk_saml_postback(
                keycloak_driver,
                sso_url,
                username=tenant.user_email,
                password=KC_USER_PASSWORD,
            )
            assert postback.get("SAMLResponse"), "the SAML walk must capture a SAMLResponse"
            assert postback.get("RelayState"), "the walk must round-trip the signed RelayState back"

            # 3. The REAL ACS route verifies the assertion against the
            # harness-fetched descriptor, the bound per-provider EntityID and
            # the RelayState HMAC; and JIT-provisions the SSO subject.
            acs_resp = await client.post(
                f"/api/v1/auth/saml/acs/{provider_id}",
                data={
                    "SAMLResponse": postback["SAMLResponse"],
                    "RelayState": postback["RelayState"],
                },
            )
            assert acs_resp.status_code == 307, acs_resp.text
            location = acs_resp.headers["location"]
            assert location.startswith(_FRONTEND_BASE + "/auth/callback#"), (
                f"redirect must target the SPA callback fragment, got {location}"
            )
            assert "access_token=" in location, location
            assert "refresh_token=" not in location, "FAR-1197: the refresh token must never leak into the redirect URL"
            cookie_names = {c.name for c in acs_resp.cookies.jar}
            assert "modulo_refresh" in cookie_names, (
                f"the refresh token is delivered via the httpOnly modulo_refresh cookie; got {cookie_names}"
            )
        finally:
            await client.aclose()

        # 4. The REAL DB provisioning path created the account.
        account = await _get_account_by_email(app_engine, tenant.user_email)
        assert account is not None, "the REAL SAML flow must provision a REAL account with the IdP email"
        assert account["auth_provider"] == "saml"

        role = await _get_membership_role(app_engine, account["id"], org_id)
        assert role == "viewer", f"the provisioned member must inherit the provider's default_role (viewer), got {role}"

        subject_row = await _get_sso_subject(app_engine, account["id"])
        assert subject_row is not None, "the provisioned account must carry an SSO identity subject"
        subject = subject_row.get("sso_subject")
        assert subject, f"accounts.sso_subject must be set, got {subject_row}"
        assert tenant.idp_entity_id in subject or tenant.realm in subject, (
            f"the SSO subject must reference the realm's IdP entity, got {subject}"
        )
