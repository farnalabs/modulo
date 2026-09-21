"""FAR-1006 review: RLS behaviour of SSO provider reads on app sessions.

Verified regression risk on the pre-auth SSO surface (per-provider SAML and
org-login), which runs via the ``modulo_app`` role (NOBYPASSRLS) while the
identity bootstrap stage holds NO ``app.organisation_id`` RLS context:

1. ``sso_providers`` has STRICT row-level security
   (``organisation_id = NULLIF(current_setting('app.organisation_id', true), '')::uuid``),
   so an app session that has NOT set an org sees ZERO provider rows —
   fail-closed, not leaky.
2. Once ``set_rls_org`` is applied for the resolved org, exactly that org's
   enabled providers become visible and a sibling org's provider stays
   invisible (cross-tenant isolation holds when RLS is correctly bound).
3. ``organisations`` lookups used to bootstrap login (by slug) are NOT
   RLS-scoped, so the pre-auth org-login flow can resolve the org first and
   then bind RLS before reading providers.

These tests pin the real SQL semantics against a real Postgres (testcontainers),
not mocks, because mock-level assertions cannot exercise the actual RLS policy.
"""

import uuid

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.integration


async def _seed_two_orgs_with_providers(
    db_engine: AsyncEngine,
) -> tuple[uuid.UUID, uuid.UUID, str, str, str, str]:
    """Committed org A (OIDC provider) and org B (SAML provider).

    Slugs AND provider slugs are unique per call so repeated seeds across
    tests never collide on the global unique constraints.
    Returns ``(org_a_id, org_b_id, slug_a, slug_b, provider_oidc_a, provider_saml_b)``.
    """
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    slug_a = f"int-rls-a-{org_a.hex[:8]}"
    slug_b = f"int-rls-b-{org_b.hex[:8]}"
    provider_oidc_a = f"int-rls-{org_a.hex[:10]}-oidc"
    provider_saml_b = f"int-rls-{org_b.hex[:10]}-saml"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            sql_text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:aid, 'RLS Org A', :aslug, '{}'::json), "
                "(:bid, 'RLS Org B', :bslug, '{}'::json)"
            ),
            {"aid": str(org_a), "aslug": slug_a, "bid": str(org_b), "bslug": slug_b},
        )
        await conn.execute(
            sql_text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, enabled) VALUES "
                "(:pa, :a, 'oidc', 'Org A OIDC', :pid_a, true), "
                "(:pb, :b, 'saml', 'Org B SAML', :pid_b, true)"
            ),
            {
                "pa": str(uuid.uuid4()),
                "a": str(org_a),
                "pid_a": provider_oidc_a,
                "pb": str(uuid.uuid4()),
                "b": str(org_b),
                "pid_b": provider_saml_b,
            },
        )
    return org_a, org_b, slug_a, slug_b, provider_oidc_a, provider_saml_b


async def _org_id_by_slug(app_engine: AsyncEngine, slug: str) -> uuid.UUID:
    """Pre-auth org resolution: the app session can read organisations by slug."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        resolved = (
            await session.execute(sql_text("SELECT id FROM organisations WHERE slug = :slug"), {"slug": slug})
        ).scalar_one_or_none()
        assert resolved is not None, "pre-auth org bootstrap lookup must work without RLS org context"
        return uuid.UUID(str(resolved))


async def test_sso_provider_reads_fail_closed_without_rls_org(app_engine: AsyncEngine, db_engine: AsyncEngine) -> None:
    """An app (modulo_app) session with NO RLS org sees ZERO sso_providers rows."""
    await _seed_two_orgs_with_providers(db_engine)

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        rows = (await session.execute(sql_text("SELECT provider_id FROM sso_providers"))).all()
        assert not rows, "STRICT RLS must hide all providers when no org context is set"


async def test_set_rls_org_bounds_visibility_and_cross_org_isolation_holds(
    app_engine: AsyncEngine, db_engine: AsyncEngine
) -> None:
    """With RLS scoped per-org, exactly that org's providers are visible.

    This is the semantics ``set_rls_org(session, resolved_org_id)`` must buy the
    per-provider SSO surface: readable providers limited to the resolved org,
    the sibling org's provider invisible.
    """
    _org_a, _org_b, slug_a, _slug_b, provider_oidc_a, provider_saml_b = await _seed_two_orgs_with_providers(db_engine)
    from modulo.db.rls import set_rls_org

    org_a = await _org_id_by_slug(app_engine, slug_a)
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_a)
        rows = (await session.execute(sql_text("SELECT provider_id FROM sso_providers"))).all()
        visible = {r[0] for r in rows}
        assert provider_oidc_a in visible, "own-org provider must be visible once the RLS org is set"
        assert provider_saml_b not in visible, "sibling org's provider must remain invisible"


async def test_org_bootstrap_works_then_binding_reveals_only_own_provider(
    app_engine: AsyncEngine, db_engine: AsyncEngine
) -> None:
    """The exact pre-auth org-login shape: org resolves, providers do not (yet).

    Pins the verified trap as an executable regression: any pre-auth SSO read
    that forgets to bind the RLS org before querying ``sso_providers``
    silently sees an EMPTY provider list (broken UX surface that still fails
    closed — not a leak). The correct pattern is to resolve the org by slug
    first (organisations is not RLS-scoped), then ``set_rls_org``, then read
    the provider — asserted end-to-end here.
    """
    _org_a, _org_b, _slug_a, slug_b, _oidc_a, provider_saml_b = await _seed_two_orgs_with_providers(db_engine)
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        providers = (await session.execute(sql_text("SELECT provider_id FROM sso_providers"))).all()
        assert not providers, "pre-binding app session must see zero providers"
        org_b = await _org_id_by_slug(app_engine, slug_b)
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_b)
        saml_row = (
            await session.execute(
                sql_text(
                    "SELECT provider_id FROM sso_providers "
                    "WHERE provider_id = :pid AND provider_type = 'saml' AND enabled"
                ),
                {"pid": provider_saml_b},
            )
        ).scalar_one_or_none()
        assert saml_row == provider_saml_b
