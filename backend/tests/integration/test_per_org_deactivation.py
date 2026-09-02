"""Integration tests for per-org user deactivation (FAR-533 / gh-1794).

A single-org admin deactivating a user shared across tenants must affect
ONLY the caller's org: the org-membership ``deactivated_at`` tombstone is the
deactivation signal and ``accounts.active`` stays true (the operator /
break-glass branch keeps the global flip by design).

Proven against a real Postgres (testcontainers) through both the SECURITY
DEFINER and the admin API routes:

* org-A admin deactivates a shared user -> org-A tombstoned, org-B untouched,
  ``accounts.active`` still true, login mints org-B (org-A blocked).
* org-A admin reactivates -> org-A tombstone cleared, org-B unaffected, login
  mints org-A again.
* org-B admin CANNOT clear the org-A tombstone (their reactivate is a no-op
  scoped to their own org).
* operator (modulo_breakglass) deactivation remains GLOBAL: accounts.active
  flips, every membership tombstoned, login denied outright.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token, decode_principal
from modulo.auth.passwords import hash_password
from modulo.db.crud.org_membership import resolve_role_from_membership
from modulo.settings import Settings, get_settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_PASSWORD = "per-org-password-123"


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _create_org(engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name.lower()}-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_account(
    engine: AsyncEngine,
    *,
    email: str,
    password: str | None = None,
    active: bool = True,
) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, :hash, 'local', :active)"
            ),
            {
                "id": str(acc_id),
                "email": email,
                "name": f"PO User {acc_id.hex[:8]}",
                "hash": hash_password(password) if password else "hash",
                "active": active,
            },
        )
    return acc_id


async def _create_membership(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    role: str = "admin",
    joined_at: datetime | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role, joined_at) "
                "VALUES (:id, :aid, :oid, :role, :joined)"
            ),
            {
                "id": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(org_id),
                "role": role,
                "joined": joined_at or datetime.now(UTC),
            },
        )


async def _call_deactivate(
    engine: AsyncEngine,
    caller: uuid.UUID,
    target: uuid.UUID,
    *,
    force: bool = False,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT public.deactivate_break_glass(:caller, :target, :force)"),
            {"caller": str(caller), "target": str(target), "force": force},
        )


async def _tombstone_set(engine: AsyncEngine, account_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    async with engine.connect() as conn:
        return bool(
            (
                await conn.execute(
                    text(
                        "SELECT deactivated_at IS NOT NULL FROM org_memberships "
                        "WHERE account_id = :aid AND organisation_id = :oid"
                    ),
                    {"aid": str(account_id), "oid": str(org_id)},
                )
            ).scalar_one()
        )


async def _account_active(engine: AsyncEngine, account_id: uuid.UUID) -> bool:
    async with engine.connect() as conn:
        return bool(
            (
                await conn.execute(text("SELECT active FROM accounts WHERE id = :id"), {"id": str(account_id)})
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# HTTP client fixture â€” FastAPI app wired to the testcontainer database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as async_client:
        yield async_client

    app.dependency_overrides.clear()


def _admin_token(org_id: uuid.UUID, account_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role="admin",
        is_system_admin=False,
    )


def _login_org_of(access_token: str) -> uuid.UUID:
    """Decode a minted access token and return its organisation claim."""
    principal = decode_principal(access_token, _VALID_32)
    assert principal.organisation_id, "login must mint an org-scoped token"
    return uuid.UUID(str(principal.organisation_id))


# ---------------------------------------------------------------------------
# Per-org deactivation semantics
# ---------------------------------------------------------------------------


async def test_org_a_deactivation_leaves_org_b_and_account_active(
    db_engine: AsyncEngine, app_engine: AsyncEngine, client: AsyncClient
) -> None:
    org_a = await _create_org(db_engine, "PerOrgA")
    org_b = await _create_org(db_engine, "PerOrgB")
    admin_a = await _create_account(db_engine, email=f"poa-{uuid.uuid4().hex[:12]}@example.com")
    admin_b = await _create_account(db_engine, email=f"pob-{uuid.uuid4().hex[:12]}@example.com")
    shared_email = f"poshared-{uuid.uuid4().hex[:12]}@example.com"
    shared = await _create_account(db_engine, email=shared_email, password=_PASSWORD)
    # joined_at ordering is deterministic: org-A membership joins FIRST. The
    # per-org admin seeds keep the M2020 last-admin guard satisfied (the guard
    # protects EVERY org the target belongs to, not just the caller's).
    await _create_membership(db_engine, org_id=org_a, account_id=admin_a)
    await _create_membership(db_engine, org_id=org_b, account_id=admin_b)
    await _create_membership(
        db_engine, org_id=org_a, account_id=shared, role="runner", joined_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    await _create_membership(db_engine, org_id=org_b, account_id=shared, role="runner", joined_at=datetime.now(UTC))

    resp = await client.post(
        f"/api/v1/admin/users/{shared}/deactivate",
        headers={"Authorization": f"Bearer {_admin_token(org_a, admin_a)}"},
    )
    assert resp.status_code == 200, resp.text
    # The route's is_active is the CALLER'S-ORG view: tombstoned in org-A.
    assert resp.json()["is_active"] is False

    assert await _tombstone_set(db_engine, shared, org_a) is True
    assert await _tombstone_set(db_engine, shared, org_b) is False
    assert await _account_active(db_engine, shared) is True  # NOT globally flipped

    # Login resolves its org from ACTIVE memberships only -> org-B.
    login = await client.post("/api/v1/auth/login", json={"email": shared_email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    assert _login_org_of(login.json()["access_token"]) == org_b

    # Role resolution honours the tombstone per-org (ADR 017 read path).
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_a)})
        role_a = await resolve_role_from_membership(session, str(shared), str(org_a))
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_b)})
        role_b = await resolve_role_from_membership(session, str(shared), str(org_b))
    assert role_a is None
    assert role_b == "runner"


async def test_reactivate_restores_callers_org_only(
    db_engine: AsyncEngine, app_engine: AsyncEngine, client: AsyncClient
) -> None:
    org_a = await _create_org(db_engine, "PerOrgReA")
    org_b = await _create_org(db_engine, "PerOrgReB")
    admin_a = await _create_account(db_engine, email=f"pora-{uuid.uuid4().hex[:12]}@example.com")
    admin_b = await _create_account(db_engine, email=f"porb-{uuid.uuid4().hex[:12]}@example.com")
    shared_email = f"porshared-{uuid.uuid4().hex[:12]}@example.com"
    shared = await _create_account(db_engine, email=shared_email, password=_PASSWORD)
    await _create_membership(db_engine, org_id=org_a, account_id=admin_a)
    await _create_membership(db_engine, org_id=org_b, account_id=admin_b)
    await _create_membership(
        db_engine, org_id=org_a, account_id=shared, role="runner", joined_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    await _create_membership(db_engine, org_id=org_b, account_id=shared, role="runner", joined_at=datetime.now(UTC))

    # Org-A admin deactivates via the SECURITY DEFINER (non-operator branch).
    await _call_deactivate(app_engine, admin_a, shared)
    assert await _tombstone_set(db_engine, shared, org_a) is True

    # While org-A-tombstoned, login still succeeds into org-B.
    login = await client.post("/api/v1/auth/login", json={"email": shared_email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    assert _login_org_of(login.json()["access_token"]) == org_b

    resp = await client.post(
        f"/api/v1/admin/users/{shared}/reactivate",
        headers={"Authorization": f"Bearer {_admin_token(org_a, admin_a)}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True

    assert await _tombstone_set(db_engine, shared, org_a) is False
    assert await _tombstone_set(db_engine, shared, org_b) is False
    assert await _account_active(db_engine, shared) is True

    # Login resolves org-A again (earlier joined_at among ACTIVE memberships).
    login = await client.post("/api/v1/auth/login", json={"email": shared_email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    assert _login_org_of(login.json()["access_token"]) == org_a


async def test_org_b_admin_cannot_clear_org_a_tombstone(
    db_engine: AsyncEngine, app_engine: AsyncEngine, client: AsyncClient
) -> None:
    org_a = await _create_org(db_engine, "PerOrgAuthA")
    org_b = await _create_org(db_engine, "PerOrgAuthB")
    admin_a = await _create_account(db_engine, email=f"poaa-{uuid.uuid4().hex[:12]}@example.com")
    admin_b = await _create_account(db_engine, email=f"poab-{uuid.uuid4().hex[:12]}@example.com")
    shared_email = f"posharedauth-{uuid.uuid4().hex[:12]}@example.com"
    shared = await _create_account(db_engine, email=shared_email, password=_PASSWORD)
    await _create_membership(db_engine, org_id=org_a, account_id=admin_a)
    await _create_membership(db_engine, org_id=org_b, account_id=admin_b)
    await _create_membership(
        db_engine, org_id=org_a, account_id=shared, role="runner", joined_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    await _create_membership(db_engine, org_id=org_b, account_id=shared, role="runner", joined_at=datetime.now(UTC))

    await _call_deactivate(app_engine, admin_a, shared)
    assert await _tombstone_set(db_engine, shared, org_a) is True

    # Org-B admin's reactivate is scoped to THEIR org (a no-op here) â€” the
    # org-A tombstone must survive untouched.
    resp = await client.post(
        f"/api/v1/admin/users/{shared}/reactivate",
        headers={"Authorization": f"Bearer {_admin_token(org_b, admin_b)}"},
    )
    assert resp.status_code == 200, resp.text

    assert await _tombstone_set(db_engine, shared, org_a) is True
    assert await _account_active(db_engine, shared) is True

    # Org-A access is still revoked: login keeps resolving org-B.
    login = await client.post("/api/v1/auth/login", json={"email": shared_email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    assert _login_org_of(login.json()["access_token"]) == org_b


async def test_operator_deactivation_remains_global(
    breakglass_engine: AsyncEngine, db_engine: AsyncEngine, client: AsyncClient
) -> None:
    org_a = await _create_org(db_engine, "PerOrgOpA")
    org_b = await _create_org(db_engine, "PerOrgOpB")
    other_admin_a = await _create_account(db_engine, email=f"pooa-{uuid.uuid4().hex[:12]}@example.com")
    other_admin_b = await _create_account(db_engine, email=f"poob-{uuid.uuid4().hex[:12]}@example.com")
    caller = await _create_account(db_engine, email=f"pooc-{uuid.uuid4().hex[:12]}@example.com")
    shared_email = f"poopshared-{uuid.uuid4().hex[:12]}@example.com"
    shared = await _create_account(db_engine, email=shared_email, password=_PASSWORD)
    await _create_membership(db_engine, org_id=org_a, account_id=other_admin_a)
    await _create_membership(db_engine, org_id=org_b, account_id=other_admin_b)
    await _create_membership(
        db_engine, org_id=org_a, account_id=shared, role="runner", joined_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    await _create_membership(db_engine, org_id=org_b, account_id=shared, role="runner", joined_at=datetime.now(UTC))

    # Operator (session_user = modulo_breakglass): the break-glass branch â€”
    # global ban by design, no force needed (shared is not a last admin).
    await _call_deactivate(breakglass_engine, caller, shared)

    assert await _account_active(db_engine, shared) is False
    assert await _tombstone_set(db_engine, shared, org_a) is True
    assert await _tombstone_set(db_engine, shared, org_b) is True

    login = await client.post("/api/v1/auth/login", json={"email": shared_email, "password": _PASSWORD})
    assert login.status_code == 401
