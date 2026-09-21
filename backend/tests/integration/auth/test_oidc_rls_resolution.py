"""Real-Postgres RLS behaviour for per-provider OIDC resolution (FAR-1060).

The SAML per-provider surface got a real-DB (RLS) multi-org regression in
``test_saml_rls_resolution.py`` (FAR-1058); the OIDC per-provider routes
(``/api/v1/auth/oidc/{provider}/login`` and ``/callback``) never got the
equivalent. The product map tracked the gap in the ``feat-sso`` deferrals
("OIDC SSO flows ... are covered by unit tests only; equivalent multi-org
real-DB (RLS) integration regression not yet written") — this file closes it.

The OIDC pre-auth resolver (``modulo.auth.sso._resolve_oidc_provider``) reads
provider config through the same two-leg resolution the SAML routes use:

- the ``modulo_system`` role (BYPASSRLS) resolves the slug instance-globally —
  a provider owned by ANY org, not just the first;
- the app fallback (``modulo_app``, NOBYPASSRLS) scopes to the FIRST org —
  single-org self-hosted behaviour, deliberately fail-closed for sibling orgs.

Why integration (mocked sessions CANNOT catch this defect class): the
FAR-1058 SAML bug was ``set_rls_org`` running OUTSIDE an active transaction —
with ``autobegin=False`` session factories it raised ``RuntimeError`` → HTTP
500, an anti-enumeration oracle. The OIDC resolver had the same latent defect
(opening the app fallback without a scoped transaction); a mocked session
never raises from ``set_config`` and a mocked provider list ignores live RLS
filtering. Only a real Postgres with the real ``modulo_app`` (NOBYPASSRLS) /
``modulo_system`` (BYPASSRLS) roles exercises the contract.

Proven here:
1. The system leg resolves an OIDC provider owned by the SECOND org.
2. Unknown/absent OIDC slugs fail closed (all-None resolution, not
   ``RuntimeError``→500).
3. The app fallback (system role unprovisioned) resolves ONLY the first org's
   provider — a sibling org's slug fails closed.
4. The pre-auth app fallback runs inside a scoped transaction (FAR-1058
   parity), leaving the caller's session transaction-less afterwards.
5. An unbound app session sees ZERO OIDC providers (fail-closed baseline).
6. ``GET /api/v1/auth/oidc/{provider}/login`` resolves cross-org via the real
   system role and 307-redirects to the IdP authorization endpoint; an unknown
   slug is a 400, never a 500.

Both non-DB seams are mocked narrowly: the SSRF discovery-URL validation
(which performs real DNS) and the discovery-document HTTPS fetch stay out of
the RLS story being proven — exactly as the SAML regression mocks only the
XML-signature step.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.auth.sso import _resolve_oidc_provider
from modulo.db.crud.sso_provider import (
    get_provider_by_provider_id,
    list_enabled_oidc_providers,
)
from modulo.settings import Settings

pytestmark = pytest.mark.integration

_DISCOVERY_URL = "https://idp.example.com/.well-known/openid-configuration"
_AUTH_ENDPOINT = "https://idp.example.com/oauth/authorize"


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
    org_id = uuid.uuid4()
    slug = f"far1060-{label}-{org_id.hex[:8]}"
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


async def _create_oidc_provider(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
) -> str:
    """Create an ENABLED OIDC provider; the globally unique slug is returned."""
    slug = f"oidc-{uuid.uuid4().hex[:12]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, client_id, "
                "discovery_url, enabled, auto_provision, allowed_domains, "
                "default_role, group_mappings, preset) "
                "VALUES (:id, :oid, 'oidc', :name, :pid, :cid, "
                ":disc, true, false, CAST('[]' AS json), 'runner', '[]'::json, 'custom')"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "name": f"OIDC {slug}",
                "pid": slug,
                "cid": f"client-{slug}",
                "disc": _DISCOVERY_URL,
            },
        )
    return slug


# Two committed orgs: org_a created a DECADE earlier (it is reliably the
# "first org" for _set_default_rls_org, even after conftest's own seeded
# orgs); each org owns one enabled OIDC provider.
@pytest_asyncio.fixture(scope="module")
async def two_orgs(db_engine: AsyncEngine) -> dict[str, str]:
    org_a_id, org_a_slug = await _create_org(db_engine, "a", created_at_offset_seconds=315_360_000)
    org_b_id, org_b_slug = await _create_org(db_engine, "b", created_at_offset_seconds=0)
    oidc_a = await _create_oidc_provider(db_engine, org_id=org_a_id)
    oidc_b = await _create_oidc_provider(db_engine, org_id=org_b_id)
    return {
        "org_a_id": str(org_a_id),
        "org_a_slug": org_a_slug,
        "org_b_id": str(org_b_id),
        "org_b_slug": org_b_slug,
        "oidc_a": oidc_a,
        "oidc_b": oidc_b,
    }


# ---------------------------------------------------------------------------
# Real-role session fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def system_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine on the real ``modulo_system`` role (BYPASSRLS)."""
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


def _settings() -> Settings:
    """Minimal settings for ``_resolve_oidc_provider``.

    ``fernet_key`` is only used to decrypt a stored client_secret (NULL in the
    seeded rows), and ``modulo_oidc_providers`` defaults to no env-var
    providers so the resolution falls through cleanly when the DB lookup finds
    nothing.
    """
    return Settings(
        database_url="postgresql+asyncpg://localhost/modulo_integration",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        redis_url="",
        modulo_admin_password="",
    )


# ---------------------------------------------------------------------------
# DB-level resolution tests (real roles; discovery-fetch + SSRF-DNS mocked out)
# ---------------------------------------------------------------------------


class TestSystemLegResolution:
    """Per-provider OIDC resolution through the ``modulo_system`` role."""

    @pytest.mark.asyncio
    async def test_resolves_provider_owned_by_non_first_org(
        self,
        system_session: AsyncSession,
        app_session_factory: async_sessionmaker[AsyncSession],
        two_orgs: dict[str, str],
    ) -> None:
        """The system leg (BYPASSRLS) resolves an OIDC provider owned by ANY
        org — here the SECOND-created org, which a first-org-bound app session
        could never see. The app session must come out transaction-less (its
        own scoped fallback never opened)."""
        with patch("modulo.auth.sso.validate_outbound_url_async", AsyncMock()):
            async with app_session_factory() as app_session:
                resolved = await _resolve_oidc_provider(two_orgs["oidc_b"], system_session, app_session, _settings())
        client_id, _secret, discovery_url, _scopes, db_provider = resolved
        assert client_id == f"client-{two_orgs['oidc_b']}"
        assert discovery_url == _DISCOVERY_URL
        assert db_provider is not None
        assert str(db_provider.organisation_id) == two_orgs["org_b_id"]
        assert not app_session.in_transaction()

    @pytest.mark.asyncio
    async def test_unknown_slug_fails_closed_with_all_none(
        self,
        system_session: AsyncSession,
        app_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An unknown OIDC slug resolves to an all-None tuple — fail-closed,
        never a ``RuntimeError``→500 (the FAR-1058 anti-enumeration defect
        class)."""
        async with app_session_factory() as app_session:
            resolved = await _resolve_oidc_provider(
                f"no-such-{uuid.uuid4().hex[:8]}", system_session, app_session, _settings()
            )
            assert resolved == (None, None, None, None, None)
            assert not app_session.in_transaction()

    @pytest.mark.asyncio
    async def test_app_fallback_without_system_role_resolves_first_org_only(
        self,
        app_session_factory: async_sessionmaker[AsyncSession],
        two_orgs: dict[str, str],
    ) -> None:
        """``system_session=None`` (system role unprovisioned) → app fallback
        scoped to the FIRST org: org A's provider resolves, org B's fails
        closed (all-None). The scoped-transaction fix (FAR-1058 parity) makes
        this work on a transaction-less autobegin=False app session."""
        with patch("modulo.auth.sso.validate_outbound_url_async", AsyncMock()):
            async with app_session_factory() as app_session:
                resolved_a = await _resolve_oidc_provider(two_orgs["oidc_a"], None, app_session, _settings())
                assert not app_session.in_transaction()
                resolved_b = await _resolve_oidc_provider(two_orgs["oidc_b"], None, app_session, _settings())
        assert resolved_a[0] == f"client-{two_orgs['oidc_a']}"
        assert resolved_a[4] is not None
        assert str(resolved_a[4].organisation_id) == two_orgs["org_a_id"]
        assert resolved_b == (None, None, None, None, None)


class TestRlsFailClosedBaseline:
    """Pre-auth app session without an RLS org context sees ZERO OIDC providers."""

    @pytest.mark.asyncio
    async def test_unbound_app_session_sees_zero_oidc_providers(
        self,
        app_session_factory: async_sessionmaker[AsyncSession],
        two_orgs: dict[str, str],
    ) -> None:
        async with app_session_factory() as session:
            async with session.begin():
                provider = await get_provider_by_provider_id(session, two_orgs["oidc_a"])
                providers = await list_enabled_oidc_providers(session)
            assert provider is None
            assert not providers


# ---------------------------------------------------------------------------
# HTTP-level: per-provider OIDC login via the real app engine + RLS
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
    """ASGI client (mirrors ``test_saml_rls_resolution.multi_org_client``):
    - ``modulo_multi_org_enabled=True`` (org-login routes otherwise 404 per ADR 005),
    - a REAL system-session override (exercises the modulo_system leg),
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
async def test_per_provider_oidc_login_resolves_cross_org_and_redirects_to_idp(
    multi_org_client: AsyncClient,
    two_orgs: dict[str, str],
) -> None:
    """GET /api/v1/auth/oidc/{provider}/login resolves a provider owned by the
    NON-first org through the REAL system role + RLS and 307-redirects to the
    IdP authorization endpoint. Only the discovery-document HTTPS fetch is
    mocked (the network path the unit suite already covers)."""
    discovery = AsyncMock()
    discovery.return_value = {
        "authorization_endpoint": _AUTH_ENDPOINT,
        "issuer": "https://idp.example.com",
    }
    with (
        patch("modulo.auth.sso._fetch_discovery_pinned", discovery),
        patch("modulo.auth.sso.validate_outbound_url_async", AsyncMock()),
    ):
        resp = await multi_org_client.get(f"/api/v1/auth/oidc/{two_orgs['oidc_b']}/login", follow_redirects=False)
    assert resp.status_code == 307, resp.text
    location = resp.headers["location"]
    assert "idp.example.com/oauth/authorize" in location
    assert f"client_id=client-{two_orgs['oidc_b']}" in location
    assert f"redirect_uri=https://staging.example.com/api/v1/auth/oidc/{two_orgs['oidc_b']}/callback" in location


@pytest.mark.asyncio
async def test_per_provider_oidc_login_unknown_slug_is_400_not_500(
    multi_org_client: AsyncClient,
) -> None:
    """An unknown OIDC slug fails CLOSED on the login route with a 400 (the
    TypeError→ValueError mapping surfaces "not configured"), never the
    RuntimeError→500 the FAR-1058 class produced."""
    resp = await multi_org_client.get(f"/api/v1/auth/oidc/no-such-{uuid.uuid4().hex[:8]}/login", follow_redirects=False)
    assert resp.status_code == 400, resp.text
