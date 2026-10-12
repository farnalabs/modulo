"""FAR-1126: the OIDC SSO HTTP flow against a REAL Keycloak container.

Tier 1b (light): a real IdP container answers on HTTPS, the REAL OIDC code
path under test runs its own discovery/token/ID-token fetches against it,
and the browser walk goes through the real login UI.

The suite proves what the httpx-stub coverage in
``test_oidc_callback_http_flow`` cannot:

- the discovery document on the wire is Keycloak's own and is fetched by the
  real ``_fetch_discovery_pinned`` code path (not a hand-written stub);
- the code exchange, JWT RS256 signature check and JWKS fetch run against
  REAL Keycloak-produced tokens — no synthesized JWT fixture;
- the authorize round-trip goes through the REAL Keycloak authorize endpoint
  (a driven login-form walk), so ``sign_state``/``verify_state`` is proven
  end-to-end including what a real IdP parameterises;
- ``auto_provision`` + ``allowed_domains`` + ``default_role`` JIT behavior is
  proven against the provision path's real IdP identity claim.
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
    KC_USER_EMAIL_DOMAIN,
    KC_USER_PASSWORD,
    IdpBrowserDriver,
    walk_oidc_redirect_params,
)

pytestmark = pytest.mark.integration

_PUBLIC_BASE = HARNESS_PUBLIC_BASE
_FRONTEND_BASE = HARNESS_FRONTEND_BASE
# Deliberately ordered so the FIRST CORS origin is the WRONG frontend base: if
# the FAR-837 bug (frontend URL picked from ``cors_origins.split(",")[0]``)
# returned, the redirect would go to the marketing site, not the SPA.
_CORS_WITH_WRONG_FIRST = f"https://marketing.example.com,{_PUBLIC_BASE}"
_SECRET_KEY = "a" * 32
# A valid Fernet key (matches the integration conftest's env default); the
# provider row's client_secret is encrypted at rest with this same key and
# decrypted by the real code path under test.
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_ALLOWLISTED_DOMAIN = KC_USER_EMAIL_DOMAIN


# ---------------------------------------------------------------------------
# Seed helpers (session engine — bypasses RLS for setup). Mirrors the existing
# OIDC stub suite's seeding minus the stub-only bits.
# ---------------------------------------------------------------------------


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"KC OIDC {org_id.hex[:8]}", "slug": f"kcodec-{org_id.hex[:8]}"},
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


async def _create_oidc_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    provider_id: str,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    allowed_domains: list[str],
    default_role: str = "viewer",
) -> uuid.UUID:
    import json

    from modulo.auth.secret_storage import encrypt_stored_secret

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, client_id, "
                "client_secret, discovery_url, enabled, auto_provision, "
                "allowed_domains, default_role, group_mappings, preset) "
                "VALUES (:id, :oid, 'oidc', 'Real Keycloak OIDC', :pid, :cid, "
                ":secret, :disc, true, true, CAST(:domains AS json), :role, '[]'::json, 'custom') "
                "RETURNING id"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "pid": provider_id,
                "cid": client_id,
                # Real encrypted-at-rest secret for the REAL resolve path to
                # decrypt using the harness Fernet key. MUST be the tenant's
                # real Keycloak client secret — a placeholder makes the token
                # exchange 401 against Keycloak.
                "secret": encrypt_stored_secret(client_secret, _FERNET_KEY),
                "disc": discovery_url,
                "domains": json.dumps(allowed_domains),
                "role": default_role,
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


# ---------------------------------------------------------------------------
# A plan context that allows EVERY feature (the SSO routes are feature-gated).
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


def _make_oidc_client(app: Any, app_engine: AsyncEngine) -> httpx.AsyncClient:
    """ASGI client on the real app with production-shape overrides."""
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
async def oidc_seed(
    keycloak_tenants: tuple[Any, Any],
    db_engine: AsyncEngine,
) -> AsyncIterator[dict[str, Any]]:
    """Seed the org, inviter and per-provider DB row for the OIDC flow."""
    from modulo.api.main import app

    oidc_tenant = keycloak_tenants[0]
    org_id = await _create_org(db_engine)
    await _create_inviter(db_engine, org_id)
    # Use the TENANT's provider_id — the realm/client/redirect-uri were all
    # registered under exactly this pid, so the app's redirect_uri matches.
    provider_id = oidc_tenant.provider_id
    await _create_oidc_provider(
        db_engine,
        org_id=org_id,
        provider_id=provider_id,
        discovery_url=oidc_tenant.discovery_url,
        client_id=oidc_tenant.client_id,
        client_secret=oidc_tenant.client_secret,
        allowed_domains=[_ALLOWLISTED_DOMAIN],
    )
    try:
        yield {
            "org_id": org_id,
            "provider_id": provider_id,
            "oidc_tenant": oidc_tenant,
        }
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def keycloak_driver(keycloak_cert_dir: Any, keycloak_world: Any) -> IdpBrowserDriver:
    """A browser-shaped walk driver pointing at the live Keycloak."""
    return IdpBrowserDriver(idp_base=keycloak_world.idp_base, ca_file=keycloak_cert_dir / "ca.pem")


@pytest.mark.real_ssrf_dns
class TestRealKeycloakOidcHttpFlow:
    """Real-IdP OIDC SSO coverage on the REAL HTTP path.

    Proves end-to-end:
    1. the per-provider login route hands the browser a REAL Keycloak
       authorize URL;
    2. the driven browser walk lands on the app redirect with code + state;
    3. the callback path does the REAL token exchange, verifies the RS256
       ID token against the real JWKS and JIT-provisions the account and
       membership, and delivers the tokens with the FAR-1197 shape.
    """

    @pytest.mark.asyncio
    async def test_real_keycloak_oidc_redirect_carries_real_authorize_url(
        self,
        oidc_seed: dict[str, Any],
        app_engine: AsyncEngine,
    ) -> None:
        from modulo.api.main import app

        provider_id = oidc_seed["provider_id"]
        client = _make_oidc_client(app, app_engine)
        try:
            resp = await client.get(f"/api/v1/auth/oidc/{provider_id}/login")
            assert resp.status_code == 307, resp.text
            authorize_url = resp.headers["location"]
            assert authorize_url.startswith(oidc_seed["oidc_tenant"].issuer), authorize_url
            assert "client_id=" in authorize_url, authorize_url
            assert "state=" in authorize_url, "the authorize URL must carry a signed state"
            assert f"redirect_uri={_PUBLIC_BASE}" in authorize_url.replace("%3A", ":").replace("%2F", "/"), (
                f"authorize URL must target the per-provider redirect_uri, got {authorize_url}"
            )
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_real_keycloak_oidc_callback_flow(
        self,
        oidc_seed: dict[str, Any],
        app_engine: AsyncEngine,
        keycloak_driver: IdpBrowserDriver,
    ) -> None:
        from modulo.api.main import app

        provider_id = oidc_seed["provider_id"]
        tenant = oidc_seed["oidc_tenant"]
        org_id = oidc_seed["org_id"]
        client = _make_oidc_client(app, app_engine)
        try:
            login_resp = await client.get(f"/api/v1/auth/oidc/{provider_id}/login")
            assert login_resp.status_code == 307, login_resp.text

            authorize_url = login_resp.headers["location"]
            # 2. The REAL browser walk drives the real Keycloak login UI.
            captured = walk_oidc_redirect_params(
                keycloak_driver,
                authorize_url,
                username=tenant.user_email,
                password=KC_USER_PASSWORD,
            )
            assert captured.get("code"), "the OIDC walk must capture a code"
            assert captured.get("state"), "the OIDC walk must capture a signed state"

            # 3. The REAL callback path: token exchange + RS256 ID-token
            # verification against the REAL JWKS + JIT provisioning.
            callback_resp = await client.get(
                f"/api/v1/auth/oidc/{provider_id}/callback",
                params={"code": captured["code"], "state": captured["state"]},
            )
            assert callback_resp.status_code == 307, callback_resp.text
            location = callback_resp.headers["location"]
            assert location.startswith(_FRONTEND_BASE + "/auth/callback#"), (
                f"redirect must target the SPA callback fragment, got {location}"
            )
            assert "access_token=" in location, location
            assert "refresh_token=" not in location, "FAR-1197: the refresh token must never leak into the redirect URL"
            cookie_names = {c.name for c in callback_resp.cookies.jar}
            assert "modulo_refresh" in cookie_names, (
                f"the refresh token is delivered via the httpOnly modulo_refresh cookie; got {cookie_names}"
            )
        finally:
            await client.aclose()

        # 4. The REAL DB provisioning path created the account.
        account = await _get_account_by_email(app_engine, tenant.user_email)
        assert account is not None, "the REAL OIDC flow must provision a REAL account with the IdP email"
        assert account["auth_provider"] == "oidc"

        role = await _get_membership_role(app_engine, account["id"], org_id)
        assert role == "viewer", f"the provisioned member must inherit the provider's default_role (viewer), got {role}"
