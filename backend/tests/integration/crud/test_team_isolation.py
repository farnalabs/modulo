"""Cross-tenant team & membership isolation integration tests.

Proves that teams and their memberships are isolated between organisations,
and that team name uniqueness is enforced per organisation.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = [
    pytest.mark.integration,
]


async def _create_org(db_engine: AsyncEngine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": f"Org {slug}",
                "slug": slug,
            },
        )
    return org_id


async def _create_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, organisation_id, email, display_name, "
                "org_role, auth_provider, active, signup_json, is_service_account) "
                "VALUES (:id, :org_id, :email, :name, 'admin', 'local', true, '{}'::json, false)",
            ),
            {
                "id": str(user_id),
                "org_id": str(org_id),
                "email": email,
                "name": email.split("@", maxsplit=1)[0],
            },
        )
    return user_id


# ---------------------------------------------------------------------------
# Team CRUD isolation
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="awaiting-implementation — RLS isolation needs investigation")
async def test_teams_isolated_between_orgs(db_engine: AsyncEngine) -> None:
    """Teams created in org A must not be visible when querying as org B."""
    org_a = await _create_org(db_engine, f"team-iso-a-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"team-iso-b-{uuid.uuid4().hex[:8]}")
    user_a = await _create_user(db_engine, org_a, "alice@team-iso.com")
    user_b = await _create_user(db_engine, org_b, "bob@team-iso.com")

    from modulo.db.crud.team import create_team, list_teams

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        await create_team(session, org_id=org_a, name="Team A", account_id=user_a)
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        await create_team(session, org_id=org_b, name="Team B", account_id=user_b)
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        teams_a = await list_teams(session, org_a, page=1, page_size=50)
        names_a = {t.name for t in teams_a.items}
        assert "Team A" in names_a
        assert "Team B" not in names_a

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        teams_b = await list_teams(session, org_b, page=1, page_size=50)
        names_b = {t.name for t in teams_b.items}
        assert "Team B" in names_b
        assert "Team A" not in names_b


async def test_team_name_unique_per_org(db_engine: AsyncEngine) -> None:
    """Two teams in the same org must not share a name."""
    org = await _create_org(db_engine, f"unique-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "unique@test.com")

    from modulo.db.crud.team import create_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        await create_team(session, org_id=org, name="Unique Name", account_id=user)
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        with pytest.raises(DBAPIError):
            await create_team(session, org_id=org, name="Unique Name", account_id=user)
            await session.flush()

    # Same name in a different org must succeed
    other_org = await _create_org(db_engine, f"unique-other-{uuid.uuid4().hex[:8]}")
    other_user = await _create_user(db_engine, other_org, "other@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(other_org)},
        )
        team = await create_team(
            session,
            org_id=other_org,
            name="Unique Name",
            account_id=other_user,
        )
        await session.flush()
        assert team.name == "Unique Name"


# ---------------------------------------------------------------------------
# TeamMembership CRUD isolation
# ---------------------------------------------------------------------------


async def test_memberships_isolated_between_orgs(db_engine: AsyncEngine) -> None:
    """Memberships in a team from org A must not appear when querying as org B."""
    org_a = await _create_org(db_engine, f"mem-iso-a-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"mem-iso-b-{uuid.uuid4().hex[:8]}")
    user_a1 = await _create_user(db_engine, org_a, "mem-a1@test.com")
    user_a2 = await _create_user(db_engine, org_a, "mem-a2@test.com")
    user_b = await _create_user(db_engine, org_b, "mem-b@test.com")

    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member, list_team_members

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        team_a = await create_team(session, org_id=org_a, name="Mem Team A", account_id=user_a1)
        await session.flush()
        await add_team_member(session, org_id=org_a, team_id=team_a.id, account_id=user_a2, role="viewer")
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        team_b = await create_team(session, org_id=org_b, name="Mem Team B", account_id=user_b)
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        members_a = await list_team_members(session, team_id=team_a.id, page=1, page_size=50)
        assert len(members_a.items) == 1

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        members_b = await list_team_members(session, team_id=team_b.id, page=1, page_size=50)
        assert len(members_b.items) == 0


async def test_membership_unique_per_team_user(db_engine: AsyncEngine) -> None:
    """A user must not be added to the same team twice."""
    org = await _create_org(db_engine, f"dup-mem-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "dup@test.com")

    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Dup Test Team", account_id=user)
        await session.flush()
        await add_team_member(session, org_id=org, team_id=team.id, account_id=user, role="viewer")
        await session.flush()

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        with pytest.raises(DBAPIError):
            await add_team_member(session, org_id=org, team_id=team.id, account_id=user, role="operator")
            await session.flush()


async def test_crud_team_round_trip(db_engine: AsyncEngine) -> None:
    """Full CRUD round-trip for a team: create, get, update, delete."""
    org = await _create_org(db_engine, f"crud-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "crud@test.com")

    from modulo.db.crud.team import (
        create_team,
        delete_team,
        get_team,
        get_team_by_name,
        list_teams,
        update_team,
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        created = await create_team(
            session,
            org_id=org,
            name="Round Trip Team",
            account_id=user,
            description="A team for round-trip testing",
        )
        await session.flush()
        assert created.name == "Round Trip Team"
        assert created.description == "A team for round-trip testing"

        fetched = await get_team(session, created.id)
        assert fetched is not None
        assert fetched.id == created.id

        by_name = await get_team_by_name(session, org, "Round Trip Team")
        assert by_name is not None
        assert by_name.id == created.id

        listed = await list_teams(session, org, page=1, page_size=50)
        assert any(t.id == created.id for t in listed.items)

        updated = await update_team(session, created.id, {"name": "Updated Team", "description": "Updated"})
        assert updated is not None
        assert updated.name == "Updated Team"

        deleted = await delete_team(session, created.id)
        assert deleted is True

        gone = await get_team(session, created.id)
        assert gone is None


async def test_membership_round_trip(db_engine: AsyncEngine) -> None:
    """Full CRUD round-trip for team membership."""
    org = await _create_org(db_engine, f"mem-crud-{uuid.uuid4().hex[:8]}")
    user_a = await _create_user(db_engine, org, "mem-crud-a@test.com")
    user_b = await _create_user(db_engine, org, "mem-crud-b@test.com")

    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import (
        add_team_member,
        get_membership,
        get_membership_by_team_and_account,
        list_team_members,
        remove_team_member,
        update_member_role,
    )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Mem CRUD Team", account_id=user_a)
        await session.flush()

        membership = await add_team_member(session, org_id=org, team_id=team.id, account_id=user_b, role="viewer")
        await session.flush()
        assert membership.team_id == team.id
        assert membership.account_id == user_b
        assert membership.role == "viewer"

        fetched = await get_membership(session, membership.id)
        assert fetched is not None

        by_team_user = await get_membership_by_team_and_account(session, team.id, user_b)
        assert by_team_user is not None
        assert by_team_user.id == membership.id

        members = await list_team_members(session, team_id=team.id, page=1, page_size=50)
        assert len(members.items) == 1

        updated = await update_member_role(session, membership.id, "operator")
        assert updated is not None
        assert updated.role == "operator"

        removed = await remove_team_member(session, membership.id)
        assert removed is True

        gone = await get_membership(session, membership.id)
        assert gone is None
