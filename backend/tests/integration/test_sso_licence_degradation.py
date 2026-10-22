"""Integration proof for the SSO licence posture (FAR-2059 / FAR-2060).

Two behaviours, both driven at the REAL HTTP boundary over real Postgres and
the real anonymous plan resolver (no ``get_anonymous_plan_context`` override —
that would bypass the code under test):

FAR-2059 — degrade, never strand. A single-org instance whose users sign in
only through SSO must keep advertising its provider and keep reaching the login
leg when its licence has EXPIRED. Entitlement then degrades post-auth: the
org's plan resolves to Community, so Team features (including the admin SSO
surface) are off — but nobody is locked out.

FAR-2060 — tenant scope. A Team key installed at the ORGANISATION level must
be recognised by the pre-auth resolver: the org is the trusted tenant (the
single login-active org), so discovery lists the provider and the login leg is
not 402, and the org's post-auth plan is Team with ``sso`` enabled.

What is NOT exercised: a real IdP round-trip (the callback leg needs a live or
stubbed IdP). The login leg is driven only far enough to prove the GATE passed;
its own discovery fetch is pointed at a closed local port, so the handler's
configuration error (400) is the expected terminal state, never a 402.
"""

import base64
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import _get_engine, get_db_session, get_settings, get_system_db_session
from modulo.api.main import app
from modulo.core.feature_flags import resolve_plan_context
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.settings import Settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
# A closed local port: the login leg must fail fast on its OWN config check
# (Failed to fetch discovery document -> 400), never on the licence gate.
_UNREACHABLE_DISCOVERY = "http://127.0.0.1:1/.well-known/openid-configuration"

_TEST_KP = generate_keypair()


def _sign_licence(*, tier: str, features: list[str], expires_at: datetime, org_id: str) -> str:
    payload = {
        "tier": tier,
        "features": features,
        "expires_at": expires_at.isoformat(),
        "org_id": org_id,
    }
    sig_bytes = bytes.fromhex(sign_primitive(payload, _TEST_KP["private_key"]))
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _settings(*, env_licence: str = "", multi_org: bool = False) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/modulo_unused",
        secret_key=_VALID_32,
        fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        modulo_license_key=env_licence,
        modulo_multi_org_enabled=multi_org,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )


@pytest.fixture(autouse=True)
def _licence_keypair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign with the test keypair for the duration of each test.

    FAR-2059 review (MINOR-7): save AND restore the globals this changes — a
    teardown that stamps the built-in key over a caller's configured key leaks
    state into every later test in the session.
    """
    from modulo.core import license as license_mod

    monkeypatch.setattr(license_mod, "_configured_public_key_hex", _TEST_KP["public_key"])
    monkeypatch.setattr(license_mod, "_current_license", None)


@pytest_asyncio.fixture(autouse=True)
async def _restore_org_statuses(app_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Restore every organisation's status after each test (MINOR-7).

    ``_make_only_login_active_org`` suspends every other org; without a
    restore, later tests inherit a machine-dependent active-org set.
    """
    async with app_engine.begin() as conn:
        before = (await conn.execute(text("SELECT id, status FROM organisations"))).all()
    yield
    async with app_engine.begin() as conn:
        for org_id, status in before:
            await conn.execute(
                text("UPDATE organisations SET status = :status WHERE id = :id"),
                {"status": status, "id": org_id},
            )


async def _create_org(
    engine: AsyncEngine,
    *,
    settings_json: dict[str, Any],
    org_id: uuid.UUID | None = None,
) -> uuid.UUID:
    org_id = org_id or uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, CAST(:settings AS json))"
            ),
            {
                "id": str(org_id),
                "name": f"SSO Licence {org_id.hex[:8]}",
                "slug": f"sso-lic-{org_id.hex[:8]}",
                "settings": json.dumps(settings_json),
            },
        )
    return org_id


async def _make_only_login_active_org(engine: AsyncEngine, org_id: uuid.UUID) -> None:
    """Suspend every other org so THIS one is the single login-active tenant.

    The pre-auth tenant signal is "exactly one login-active org"; the shared
    integration database accumulates orgs from earlier tests, so the predicate
    must be narrowed to the org under test. Suspension is non-destructive and
    does not touch another test's rows beyond the ``status`` column.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE organisations SET status = 'suspended' WHERE id <> :id AND status = 'active'"),
            {"id": str(org_id)},
        )


async def _create_oidc_provider(engine: AsyncEngine, *, org_id: uuid.UUID, provider_id: str) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sso_providers "
                "(id, organisation_id, provider_type, name, provider_id, client_id, "
                "client_secret, discovery_url, enabled, auto_provision, allowed_domains, "
                "default_role, group_mappings, preset) "
                "VALUES (:id, :oid, 'oidc', 'Stub OIDC', :pid, 'cid', NULL, :disc, true, "
                "false, '[]'::json, 'viewer', '[]'::json, 'custom')"
            ),
            {
                "id": str(row_id),
                "oid": str(org_id),
                "pid": provider_id,
                "disc": _UNREACHABLE_DISCOVERY,
            },
        )
    return row_id


@asynccontextmanager
async def _sso_client_for(app_engine: AsyncEngine, settings: Settings) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client on the REAL app + real anonymous resolver, over real Postgres."""
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine

    factory = async_sessionmaker(app_engine, expire_on_commit=False)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://api.example.com", timeout=30.0) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sso_client(app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """Single-org (default) client over real Postgres."""
    async with _sso_client_for(app_engine, _settings()) as client:
        yield client


@pytest_asyncio.fixture
async def multi_org_sso_client(app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """Multi-org client: no single trusted pre-auth tenant (system scope)."""
    async with _sso_client_for(app_engine, _settings(multi_org=True)) as client:
        yield client


async def _provider_ids(client: AsyncClient) -> set[str]:
    resp = await client.get("/api/v1/auth/sso/providers")
    assert resp.status_code == 200, resp.text
    return {p["provider_id"] for p in resp.json()["oidc"]}


async def _assert_login_leg_not_gated(client: AsyncClient, provider_id: str) -> None:
    """The login leg is reachable: it fails on its IdP config, never on a 402."""
    resp = await client.get(f"/api/v1/auth/oidc/{provider_id}/login", follow_redirects=False)
    assert resp.status_code != 402, f"SSO login was licence-gated: {resp.status_code} {resp.text}"
    assert resp.status_code == 400, resp.text


async def _post_auth_plan(app_engine: AsyncEngine, org_id: uuid.UUID) -> Any:
    """Resolve the org's plan through the SAME shared ladder the API uses."""
    from modulo.db.crud.organisation import get_organisation

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, org_id)
        org = await get_organisation(session, org_id)
        assert org is not None
        return await resolve_plan_context(_settings(), session, org=org)


# ---------------------------------------------------------------------------
# FAR-2059 — expired licence degrades instead of locking out
# ---------------------------------------------------------------------------


async def test_expired_org_licence_still_lets_sso_users_reach_sign_in(
    sso_client: AsyncClient, db_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """An EXPIRED org-scoped Team key must not hide or gate the SSO entry point."""
    org_id = uuid.uuid4()
    expired_key = _sign_licence(
        tier="team",
        features=["sso"],
        expires_at=datetime.now(UTC) - timedelta(days=1),
        org_id=str(org_id),
    )
    await _create_org(db_engine, org_id=org_id, settings_json={"license_key": expired_key})
    provider_id = f"oidc-{org_id.hex[:8]}"
    await _create_oidc_provider(db_engine, org_id=org_id, provider_id=provider_id)
    await _make_only_login_active_org(db_engine, org_id)

    # The login page still advertises the only sign-in method…
    assert provider_id in await _provider_ids(sso_client)
    # …and the login leg is not licence-gated.
    await _assert_login_leg_not_gated(sso_client, provider_id)

    # Post-auth entitlement degrades to Community: Team features stay off.
    plan = await _post_auth_plan(app_engine, org_id)
    assert plan.tier() == "community"
    assert plan.feature_enabled("sso") is False


async def test_absent_licence_still_lets_sso_users_reach_sign_in(
    sso_client: AsyncClient, db_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """No licence of any scope: same degraded posture as an expired one."""
    org_id = await _create_org(db_engine, settings_json={})
    provider_id = f"oidc-{org_id.hex[:8]}"
    await _create_oidc_provider(db_engine, org_id=org_id, provider_id=provider_id)
    await _make_only_login_active_org(db_engine, org_id)

    resp = await sso_client.get("/api/v1/auth/sso/providers")
    assert resp.status_code == 200, resp.text
    entries = [p for p in resp.json()["oidc"] if p["provider_id"] == provider_id]
    assert entries, resp.text
    # FAR-2059 review (MINOR-2): on the degraded path the admin's free-text
    # display_name must NOT be exposed to an anonymous caller.
    assert entries[0]["display_name"] == "Single sign-on"

    await _assert_login_leg_not_gated(sso_client, provider_id)
    plan = await _post_auth_plan(app_engine, org_id)
    assert plan.tier() == "community"


# ---------------------------------------------------------------------------
# FAR-2060 — an org-scoped key enables SSO for its organisation
# ---------------------------------------------------------------------------


async def test_org_scoped_team_key_enables_sso_for_its_org(
    sso_client: AsyncClient, db_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The paid org-level key is recognised pre-auth, without any system key."""
    org_id = uuid.uuid4()
    valid_key = _sign_licence(
        tier="team",
        features=["sso", "team_rbac"],
        expires_at=datetime.now(UTC) + timedelta(days=30),
        org_id=str(org_id),
    )
    await _create_org(db_engine, org_id=org_id, settings_json={"license_key": valid_key})
    provider_id = f"oidc-{org_id.hex[:8]}"
    await _create_oidc_provider(db_engine, org_id=org_id, provider_id=provider_id)
    await _make_only_login_active_org(db_engine, org_id)

    assert provider_id in await _provider_ids(sso_client)
    await _assert_login_leg_not_gated(sso_client, provider_id)

    plan = await _post_auth_plan(app_engine, org_id)
    assert plan.tier() == "team"
    assert plan.has_license_key() is True
    assert plan.feature_enabled("sso") is True


# ---------------------------------------------------------------------------
# MAJOR-2 / MAJOR-5 — the canonical tenant predicate, at the HTTP boundary
# ---------------------------------------------------------------------------


async def test_multi_org_instance_uses_system_scope_not_a_stray_org_key(
    multi_org_sso_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """A genuine multi-org instance has no single trusted tenant.

    The former resolver returned ``org=None`` for >1 login-active org but the
    discovery helper used its own count, and the fallback was only ever
    unit-tested. Here the DB holds two active orgs and only ONE carries an
    org-scoped Team key: it must NOT be applied pre-auth (that would let one
    tenant's key license the whole instance).
    """
    paying_org = uuid.uuid4()
    team_key = _sign_licence(
        tier="team",
        features=["sso"],
        expires_at=datetime.now(UTC) + timedelta(days=30),
        org_id=str(paying_org),
    )
    await _create_org(db_engine, org_id=paying_org, settings_json={"license_key": team_key})
    provider_id = f"oidc-{paying_org.hex[:8]}"
    await _create_oidc_provider(db_engine, org_id=paying_org, provider_id=provider_id)
    # A second login-active org makes the instance genuinely multi-org.
    await _create_org(db_engine, settings_json={})

    ctx = await multi_org_sso_client.get("/api/v1/auth/login-context")
    assert ctx.status_code == 200, ctx.text
    assert ctx.json()["multi_org"] is True

    # System scope -> no DB provider is advertised (the multi-org path lists
    # environment configuration only), i.e. the org-scoped key was NOT applied.
    assert provider_id not in await _provider_ids(multi_org_sso_client)


async def test_caller_supplied_org_cannot_redirect_the_tenant(sso_client: AsyncClient, db_engine: AsyncEngine) -> None:
    """The trusted tenant is DERIVED, never taken from query/header input."""
    org_id = uuid.uuid4()
    team_key = _sign_licence(
        tier="team",
        features=["sso"],
        expires_at=datetime.now(UTC) + timedelta(days=30),
        org_id=str(org_id),
    )
    await _create_org(db_engine, org_id=org_id, settings_json={"license_key": team_key})
    provider_id = f"oidc-{org_id.hex[:8]}"
    await _create_oidc_provider(db_engine, org_id=org_id, provider_id=provider_id)
    await _make_only_login_active_org(db_engine, org_id)

    attacker_org = str(uuid.uuid4())
    variants = [
        ({"org_id": attacker_org, "slug": "attacker"}, {}),
        ({}, {"X-Org-Id": attacker_org, "X-Organisation-Id": attacker_org, "X-Organisation-Slug": "attacker"}),
        ({"org_id": attacker_org}, {"X-Org-Id": attacker_org}),
    ]
    for params, headers in variants:
        resp = await sso_client.get("/api/v1/auth/sso/providers", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        # Still the DB-derived org's provider. If caller input were honoured the
        # attacker slug/UUID has no provider -> empty list.
        assert provider_id in {p["provider_id"] for p in resp.json()["oidc"]}, resp.text


async def test_single_org_with_ambiguous_tenant_fails_closed(sso_client: AsyncClient, db_engine: AsyncEngine) -> None:
    """Single-org mode with a stray second active org must deny, not narrow.

    ``sso_client`` is the default (single-org) client. Two login-active orgs on
    such an instance make the trusted tenant unidentifiable; resolving over
    system scope instead would widen the entitlement lookup, so the pre-auth
    surface must 5xx rather than list providers.
    """
    # Deterministic two-org state: suspend everything, then add exactly two.
    await _make_only_login_active_org(db_engine, uuid.uuid4())
    await _create_org(db_engine, settings_json={})
    await _create_org(db_engine, settings_json={})

    resp = await sso_client.get("/api/v1/auth/sso/providers")
    assert resp.status_code == 503, resp.text
