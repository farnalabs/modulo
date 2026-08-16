"""Real-DB integration coverage for the team-management UI API surface (FAR-245).

Exercises ``GET /api/v1/teams/my`` (real wire shape + membership RLS) and the
optimistic-lock 409 (including the ``updated_at`` bump) against a migrated
Postgres with RLS enforced — no mocked sessions or TestClient overrides.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    """Create an account + org membership, or reuse an existing account by email.

    Fixed-looking emails are suffixed with a uuid to stay unique per run while
    keeping seeding idempotent under pytest-xdist against a shared Postgres.
    """
    async with db_engine.connect() as conn, conn.begin():
        existing = await conn.execute(text("SELECT id FROM accounts WHERE email = :email"), {"email": email})
        row = existing.first()
        if row is not None:
            account_id = uuid.UUID(str(row[0]))
        else:
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, "
                    "auth_provider, active, password_hash) "
                    "VALUES (:id, :email, :name, 'local', true, 'hash')",
                ),
                {"id": str(account_id), "email": email, "name": email.split("@", maxsplit=1)[0]},
            )
        membership = await conn.execute(
            text(
                "SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid",
            ),
            {"aid": str(account_id), "oid": str(org_id)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'admin')",
                ),
                {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
            )
    return account_id


async def _seed_team_with_members(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    name: str,
    members: list[tuple[uuid.UUID, str]],
) -> uuid.UUID:
    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        team = await create_team(session, org_id=org_id, name=name, account_id=owner_user_id)
        for member_id, role in members:
            await add_team_member(session, org_id=org_id, team_id=team.id, account_id=member_id, role=role)
    return team.id


@pytest_asyncio.fixture
async def api_client(
    db_url: str,
    app_engine: AsyncEngine,
) -> AsyncClient:
    """FastAPI app wired to the real DB with RLS enforced (non-superuser role)."""
    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    class _TeamPlan:
        """Plan stub that grants team_rbac so the gated routes are reachable."""

        def feature_enabled(self, _name: str) -> bool:
            return True

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


def _token(org_id: uuid.UUID, user_id: uuid.UUID, role: str = "admin") -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role=role,
    )


async def test_my_teams_wire_shape_and_membership_rls(
    api_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """GET /api/v1/teams/my returns only the caller's org-scoped memberships."""
    org = await _seed_org(db_engine, "MyTeamsOrg")
    alice = await _seed_user(db_engine, org, f"alice-{uuid.uuid4().hex[:6]}@mteams.local")
    bob = await _seed_user(db_engine, org, f"bob-{uuid.uuid4().hex[:6]}@mteams.local")
    team = await _seed_team_with_members(
        db_engine,
        org,
        alice,
        "Wire Team",
        [(alice, "operator"), (bob, "viewer")],
    )

    other_org = await _seed_org(db_engine, "MyTeamsOtherOrg")
    carol = await _seed_user(db_engine, other_org, f"carol-{uuid.uuid4().hex[:6]}@mteams.local")
    await _seed_team_with_members(db_engine, other_org, carol, "Foreign Team", [(carol, "operator")])

    resp = await api_client.get(
        "/api/v1/teams/my",
        headers={"Authorization": f"Bearer {_token(org, alice)}"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert isinstance(payload, list)
    assert len(payload) == 1, "alice must see only her own org's memberships (RLS)"
    entry = payload[0]
    assert set(entry) == {"team_id", "team_name", "role"}
    assert entry["team_id"] == str(team)
    assert entry["team_name"] == "Wire Team"
    assert entry["role"] == "operator"

    bob_resp = await api_client.get(
        "/api/v1/teams/my",
        headers={"Authorization": f"Bearer {_token(org, bob)}"},
    )
    assert bob_resp.status_code == 200
    bob_payload = bob_resp.json()
    assert len(bob_payload) == 1
    assert bob_payload[0]["role"] == "viewer"
    assert bob_payload[0]["team_name"] == "Wire Team"


async def test_optimistic_lock_round_trip_and_updated_at_bump(
    api_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """The optimistic-lock 409 rejects stale expected_updated_at and bumps on success."""
    org = await _seed_org(db_engine, "LockOrg")
    admin = await _seed_user(db_engine, org, f"lock-{uuid.uuid4().hex[:6]}@lock.local")
    team_id = await _seed_team_with_members(db_engine, org, admin, "Lock Team", [(admin, "operator")])

    headers = {"Authorization": f"Bearer {_token(org, admin)}"}

    list_resp = await api_client.get("/api/v1/admin/teams", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    items = {t["id"]: t for t in list_resp.json()["items"]}
    assert str(team_id) in items
    original_updated_at = items[str(team_id)]["updated_at"]
    assert original_updated_at

    # Matching expected_updated_at succeeds and bumps updated_at (onupdate).
    ok_resp = await api_client.patch(
        f"/api/v1/teams/{team_id}",
        headers=headers,
        json={"name": "Lock Team Renamed", "expected_updated_at": original_updated_at},
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.json()["name"] == "Lock Team Renamed"

    list_after = await api_client.get("/api/v1/admin/teams", headers=headers)
    after_items = {t["id"]: t for t in list_after.json()["items"]}
    bumped_updated_at = after_items[str(team_id)]["updated_at"]
    assert bumped_updated_at != original_updated_at, "updated_at must be bumped by a successful rename"

    # Stale expected_updated_at returns 409 and does NOT overwrite the rename.
    stale_resp = await api_client.patch(
        f"/api/v1/teams/{team_id}",
        headers=headers,
        json={"name": "Sneaky Rename", "expected_updated_at": original_updated_at},
    )
    assert stale_resp.status_code == 409, stale_resp.text
    assert "optimistic lock" in stale_resp.json()["detail"].lower()

    list_final = await api_client.get("/api/v1/admin/teams", headers=headers)
    final_items = {t["id"]: t for t in list_final.json()["items"]}
    assert final_items[str(team_id)]["name"] == "Lock Team Renamed"
    assert final_items[str(team_id)]["updated_at"] == bumped_updated_at
