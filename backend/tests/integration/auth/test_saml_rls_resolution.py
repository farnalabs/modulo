"""Real-Postgres RLS behaviour for SAML pre-auth resolution (FAR-1058).

Three per-provider SAML pre-auth routes and the org-login route resolve
providers/orgs BEFORE any user claim exists:

- ``POST /api/v1/auth/saml/acs/{provider_id}``
- ``GET  /api/v1/auth/saml/{provider_id}/login``
- ``GET  /api/v1/auth/saml/{provider_id}/metadata``
- ``GET  /api/v1/auth/org-login/{slug}``

Why integration (mocked sessions CANNOT catch this defect class):
``set_rls_org`` raises ``RuntimeError`` when the AsyncSession has no active
transaction (``autobegin=False`` session factories), and the strict RLS
policy on ``sso_providers`` returns ZERO rows for a session with no
``app.organisation_id`` context. Unit tests mock the session (or assert CRUD
helpers called with whatever session they were handed) so both failure
shapes are invisible: a MagicMock never raises from ``set_config``, and a
mocked provider list ignores live RLS filtering. Only a real Postgres with
the real ``modulo_app`` (NOBYPASSRLS) / ``modulo_system`` (BYPASSRLS) roles
exercises the actual contract.

Proven here:
1. An unbound pre-auth app session sees ZERO providers (fail-closed baseline).
2. Per-provider SAML resolution works cross-org through the system role.
3. An unknown slug raises the uniform 404 — NOT the RuntimeError→500 the
   pre-fix code produced (FAR-1058 D1 fix).
4. The first-org fallback (system role unprovisioned) resolves with the RLS
   binding running inside a live transaction.
5. The org-login HTTP route returns ONLY the resolved org's providers
   (sibling org invisible), through the real app engine and RLS.
6. The per-provider SAML login HTTP route resolves via the system role and
   issues the IdP redirect (307) end-to-end.

What is NOT covered: the full SAML ACS HTTP flow (a signed IdP Response) —
same documented gap as ``test_sso_invite_consume``; signature round-trips
cannot be produced on Windows (see ``test_saml_audience_containment``).
"""

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.api.routes.sso import _resolve_saml_for_route
from modulo.db.crud.sso_provider import (
    get_provider_by_provider_id,
    list_enabled_saml_providers,
)
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_IDP_METADATA = (
    '<?xml version="1.0"?>'
    '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"'
    ' entityID="https://idp.example.com">'
    '  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
    "    <md:SingleSignOnService"
    '     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"'
    '     Location="https://idp.example.com/sso"/>'
    "  </md:IDPSSODescriptor>"
    "</md:EntityDescriptor>"
)


# ---------------------------------------------------------------------------
# Seed helpers (superuser engine — bypasses RLS for setup)
# ---------------------------------------------------------------------------


async def _create_org(
    engine: AsyncEngine,
    label: str,
    *,
    created_at_offset_seconds: int = 0,
) -> tuple[uuid.UUID, str]:
    """Create a login-active org; slug returned for URL use."""
    import uuid as _uuid

    org_id = _uuid.uuid4()
    slug = f"far1058-{label}-{org_id.hex[:8]}"
    created_at = datetime.now(UTC) - timedelta(seconds=created_at_offset_seconds)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, created_at) "
                "VALUES (:id, :name, :slug, '{}'::json, :created_at)"
            ),
            {"id": str(org_id), "name": f"Org {label}", "slug": slug, "created_at": created_at},
        )
    return org_id, slug


async def _create_saml_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
) -> str:
    """Create an ENABLED SAML provider; the globally unique slug is returned."""
    import uuid as _uuid

    slug = f"saml-{_uuid.uuid4().hex[:12]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, entity_id, "
                "metadata_xml, enabled, auto_provision, allowed_domains, "
                "default_role, group_mappings, preset) "
                "VALUES (:id, :oid, 'saml', :name, :pid, :eid, :meta, true, false, "
                "CAST('[]' AS json), 'runner', '[]'::json, 'custom')"
            ),
            {
                "id": str(_uuid.uuid4()),
                "oid": str(org_id),
                "name": f"SAML {slug}",
                "pid": slug,
                "eid": f"urn:modulo:sp:{org_id.hex[:8]}",
                "meta": _IDP_METADATA,
            },
        )
    return slug


async def _create_oidc_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
) -> str:
    import uuid as _uuid

    slug = f"oidc-{_uuid.uuid4().hex[:12]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, client_id, "
                "discovery_url, enabled, auto_provision, allowed_domains, "
                "default_role, group_mappings, preset) "
                "VALUES (:id, :oid, 'oidc', :name, :pid, 'cid', "
                "'https://example.com/.well-known/openid-configuration', true, false, "
                "CAST('[]' AS json), 'runner', '[]'::json, 'custom')"
            ),
            {
                "id": str(_uuid.uuid4()),
                "oid": str(org_id),
                "name": f"OIDC {slug}",
                "pid": slug,
            },
        )
    return slug


# ---------------------------------------------------------------------------
# Real-role session fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def system_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine on the real ``modulo_system`` role (BYPASSRLS).

    The integration conftest provisions the role and exports
    ``MODULO_SYSTEM_DATABASE_URL`` exactly like production settings do.
    """
    engine = create_async_engine(
        os.environ["MODULO_SYSTEM_DATABASE_URL"],
        echo=False,
        poolclass=NullPool,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def system_session(system_engine: AsyncEngine) -> AsyncSession:
    """Session on the ``modulo_system`` role — mirrors ``get_system_db_session``."""
    return AsyncSession(bind=system_engine, autobegin=False, expire_on_commit=False)


@pytest_asyncio.fixture
async def app_session_factory(modulo_app_engine: AsyncEngine) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Session factory on the real ``modulo_app`` role (NOBYPASSRLS, RLS applies)."""
    yield async_sessionmaker(modulo_app_engine, expire_on_commit=False, autobegin=False)


# Two committed orgs: org_a created a DECADE earlier (it is reliably the
# "first org" for _set_default_rls_org, even after conftest's own seeded
# orgs); each org owns one enabled SAML provider; org_a also owns one OIDC
# provider so the org-login route's type mix is exercised.
@pytest_asyncio.fixture(scope="module")
async def two_orgs(db_engine: AsyncEngine) -> dict[str, str]:
    org_a_id, org_a_slug = await _create_org(db_engine, "a", created_at_offset_seconds=315_360_000)
    org_b_id, org_b_slug = await _create_org(db_engine, "b", created_at_offset_seconds=0)
    saml_a = await _create_saml_provider(db_engine, org_id=org_a_id)
    oidc_a = await _create_oidc_provider(db_engine, org_id=org_a_id)
    saml_b = await _create_saml_provider(db_engine, org_id=org_b_id)
    return {
        "org_a_id": str(org_a_id),
        "org_a_slug": org_a_slug,
        "org_b_id": str(org_b_id),
        "org_b_slug": org_b_slug,
        "saml_a": saml_a,
        "oidc_a": oidc_a,
        "saml_b": saml_b,
    }


# ---------------------------------------------------------------------------
# DB-level resolution tests (real roles, no HTTP)
# ---------------------------------------------------------------------------


class TestSystemLegResolution:
    """Per-provider resolution through the ``modulo_system`` role (FAR-1001)."""

    @pytest.mark.asyncio
    async def test_resolves_provider_owned_by_non_first_org(
        self,
        system_session: AsyncSession,
        app_session_factory: async_sessionmaker[AsyncSession],
        two_orgs: dict[str, str],
    ) -> None:
        """The system leg (BYPASSRLS) resolves a provider owned by ANY org —
        here the SECOND-created org, which a first-org-bound app session could
        never see. The app session must come out transaction-less (its own
        scoped ``app_session.begin()`` committed its RLS local binding, not a
        caller-level transaction)."""
        async with app_session_factory() as app_session:
            provider = await _resolve_saml_for_route(two_orgs["saml_b"], system_session, app_session)
            assert provider.provider_id == two_orgs["saml_b"]
            assert provider.organisation_id is not None
            assert not app_session.in_transaction()

    @pytest.mark.asyncio
    async def test_unknown_slug_raises_uniform_404_not_runtime_error(
        self,
        system_session: AsyncSession,
        app_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """FAR-1058 D1 regression: the pre-fix fallback called
        ``_set_default_rls_org`` outside any transaction, so every request
        reaching it raised ``RuntimeError`` (→ HTTP 500) — an enumeration
        oracle and a broken route. Now the fallback runs inside its own
        scoped transaction and the route contract holds: uniform 404."""
        async with app_session_factory() as app_session:
            with pytest.raises(HTTPException) as exc_info:
                await _resolve_saml_for_route(f"no-such-{uuid.uuid4().hex[:8]}", system_session, app_session)
            assert exc_info.value.status_code == 404
            assert not app_session.in_transaction()

    @pytest.mark.asyncio
    async def test_app_fallback_without_system_role_resolves_first_org(
        self,
        app_session_factory: async_sessionmaker[AsyncSession],
        two_orgs: dict[str, str],
    ) -> None:
        """``system_session=None`` (system role unprovisioned) → app fallback:
        the RLS binding now runs INSIDE a scoped transaction, so the first
        org's provider resolves and other orgs' providers fail closed.

        ``_set_default_rls_org`` is pinned to org A. The real helper binds the
        globally OLDEST org (``created_at`` asc, limit 1), and every
        integration module shares one Postgres, so a competing module's
        backdated org can win that single slot depending on fixture execution
        order (non-deterministic under ``pytest-xdist``). Pinning the org keeps
        the contract under test — the resolver opens a scoped transaction and
        sets an RLS binding BEFORE the provider read — deterministic, while
        still calling the REAL ``set_rls_org`` so the FAR-1058
        no-transaction ``RuntimeError`` regression is still caught if the
        scoped transaction is ever removed."""

        async def _bind_org_a(session: AsyncSession) -> None:
            await set_rls_org(session, uuid.UUID(two_orgs["org_a_id"]))

        with patch("modulo.api.routes.sso._set_default_rls_org", new=_bind_org_a):
            async with app_session_factory() as app_session:
                provider = await _resolve_saml_for_route(two_orgs["saml_a"], None, app_session)
                assert provider.provider_id == two_orgs["saml_a"]
                with pytest.raises(HTTPException) as exc_info:
                    await _resolve_saml_for_route(two_orgs["saml_b"], None, app_session)
                assert exc_info.value.status_code == 404


class TestRlsFailClosedBaseline:
    """Pre-auth app session without an RLS org context sees ZERO providers.

    This is the invariant both fixes rely on (fail-closed), and the gap the
    org-login binding fix closes WITHOUT weakening (binding happens only
    after the org resolves).
    """

    @pytest.mark.asyncio
    async def test_unbound_app_session_sees_zero_saml_providers(
        self,
        app_session_factory: async_sessionmaker[AsyncSession],
        two_orgs: dict[str, str],
    ) -> None:
        async with app_session_factory() as session:
            async with session.begin():
                provider = await get_provider_by_provider_id(session, two_orgs["saml_a"])
                providers = await list_enabled_saml_providers(session)
            assert provider is None
            assert not providers


# ---------------------------------------------------------------------------
# HTTP-level: org-login provider visibility (real app engine + RLS)
# ---------------------------------------------------------------------------


class _AllFeatures:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


@pytest_asyncio.fixture
async def multi_org_client(db_url: str, app_engine: AsyncEngine, system_session: AsyncSession) -> AsyncClient:
    """ASGI client like conftest's ``integration_client`` but with:
    - ``modulo_multi_org_enabled=True`` (org-login routes otherwise 404 per ADR 005),
    - a REAL system session override (exercises the modulo_system leg),
    - an anonymous plan-context override (pre-auth feature gate)."""
    from modulo.api.dependencies import _get_engine, get_anonymous_plan_context, get_db_session, get_system_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        modulo_multi_org_enabled=True,
        modulo_public_url="https://staging.example.com",
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = lambda: system_session
    app.dependency_overrides[get_anonymous_plan_context] = lambda: _AllFeatures()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_org_login_returns_only_resolved_org_providers(
    multi_org_client: AsyncClient,
    two_orgs: dict[str, str],
) -> None:
    """FAR-1058 org-login fix: the route binds the app session's RLS org to
    the RESOLVED org before the provider reads, so the org's own providers
    surface and sibling-org providers never do — through the REAL app engine
    (NOBYPASSRLS role), not mocks."""
    resp_a = await multi_org_client.get(f"/api/v1/auth/org-login/{two_orgs['org_a_slug']}")
    assert resp_a.status_code == 200, resp_a.text
    body_a = resp_a.json()
    assert body_a["org"]["slug"] == two_orgs["org_a_slug"]
    assert body_a["saml"] is True
    got_a = sorted(p["provider_id"] for p in body_a["providers"])
    assert got_a == sorted([two_orgs["saml_a"], two_orgs["oidc_a"]])
    types = {p["provider_id"]: p["type"] for p in body_a["providers"]}
    assert types[two_orgs["saml_a"]] == "saml"
    assert types[two_orgs["oidc_a"]] == "oidc"

    resp_b = await multi_org_client.get(f"/api/v1/auth/org-login/{two_orgs['org_b_slug']}")
    assert resp_b.status_code == 200, resp_b.text
    body_b = resp_b.json()
    got_b = [p["provider_id"] for p in body_b["providers"]]
    assert got_b == [two_orgs["saml_b"]]
    assert body_b["saml"] is True

    unknown = await multi_org_client.get(f"/api/v1/auth/org-login/no-such-{uuid.uuid4().hex[:8]}")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_per_provider_saml_login_redirects_to_idp(
    multi_org_client: AsyncClient,
    two_orgs: dict[str, str],
) -> None:
    """GET /saml/{provider_id}/login end-to-end: resolution via the REAL
    system role, AuthnRequest through real python3-saml, 307 to the IdP."""
    resp = await multi_org_client.get(f"/api/v1/auth/saml/{two_orgs['saml_b']}/login")
    assert resp.status_code == 307, resp.text
    location = resp.headers["location"]
    assert "idp.example.com" in location
    assert "SAMLRequest" in location


@pytest.mark.asyncio
async def test_per_provider_saml_metadata_renders_entity_and_acs(
    multi_org_client: AsyncClient,
    two_orgs: dict[str, str],
) -> None:
    """GET /saml/{provider_id}/metadata resolves through the shared helper and
    renders THIS provider's SP entity ID + per-provider ACS URL."""
    resp = await multi_org_client.get(f"/api/v1/auth/saml/{two_orgs['saml_a']}/metadata")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert f'entityID="urn:modulo:sp:{two_orgs["org_a_id"][:8]}"' in body
    assert f"/api/v1/auth/saml/acs/{two_orgs['saml_a']}" in body
