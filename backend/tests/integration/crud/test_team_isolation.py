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
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, "
                "auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')",
            ),
            {
                "id": str(account_id),
                "email": email,
                "name": email.split("@", maxsplit=1)[0],
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {
                "mid": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(org_id),
            },
        )
    return account_id


# ---------------------------------------------------------------------------
# Team CRUD isolation
# ---------------------------------------------------------------------------


async def test_teams_isolated_between_orgs(db_engine: AsyncEngine) -> None:
    """Teams created in org A must not be visible when querying as org B."""
    org_a = await _create_org(db_engine, f"team-iso-a-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"team-iso-b-{uuid.uuid4().hex[:8]}")
    user_a = await _create_user(db_engine, org_a, "alice@team-iso.com")
    user_b = await _create_user(db_engine, org_b, "bob@team-iso.com")

    from modulo.db.crud.team import create_team, list_teams

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        await create_team(session, org_id=org_a, name="Team A", account_id=user_a)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        await create_team(session, org_id=org_b, name="Team B", account_id=user_b)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        teams_a = await list_teams(session, org_a, page=1, page_size=50)
        names_a = {t.name for t in teams_a.items}
        assert "Team A" in names_a
        assert "Team B" not in names_a

    async with factory() as session, session.begin():
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

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        await create_team(session, org_id=org, name="Unique Name", account_id=user)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        with pytest.raises(DBAPIError):
            await create_team(session, org_id=org, name="Unique Name", account_id=user)

    # Same name in a different org must succeed
    other_org = await _create_org(db_engine, f"unique-other-{uuid.uuid4().hex[:8]}")
    other_user = await _create_user(db_engine, other_org, "other@test.com")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
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

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_a)},
        )
        team_a = await create_team(session, org_id=org_a, name="Mem Team A", account_id=user_a1)
        await add_team_member(session, org_id=org_a, team_id=team_a.id, account_id=user_a2, role="viewer")

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_b)},
        )
        team_b = await create_team(session, org_id=org_b, name="Mem Team B", account_id=user_b)

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
        assert not members_b.items


async def test_membership_unique_per_team_user(db_engine: AsyncEngine) -> None:
    """A user must not be added to the same team twice."""
    org = await _create_org(db_engine, f"dup-mem-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "dup@test.com")

    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Dup Test Team", account_id=user)
        await add_team_member(session, org_id=org, team_id=team.id, account_id=user, role="viewer")

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        with pytest.raises(DBAPIError):
            await add_team_member(session, org_id=org, team_id=team.id, account_id=user, role="operator")


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

    async with factory() as session, session.begin():
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


async def test_soft_deleted_team_hidden_but_row_persists(db_engine: AsyncEngine) -> None:
    """A soft-deleted team disappears from lists/lookups but the row persists with deleted_at set."""
    org = await _create_org(db_engine, f"softdel-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "softdel@test.com")

    from modulo.db.crud.team import create_team, delete_team, get_team, get_team_by_name, list_teams

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        created = await create_team(session, org_id=org, name="Soft Delete Team", account_id=user)

        deleted = await delete_team(session, created.id)
        assert deleted is True

        assert await get_team(session, created.id) is None
        assert await get_team_by_name(session, org, "Soft Delete Team") is None
        listed = await list_teams(session, org, page=1, page_size=50)
        assert not any(t.id == created.id for t in listed.items)

    # The row still exists in the DB with deleted_at set (raw query with org context).
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org)})
        row = (
            await conn.execute(
                text("SELECT deleted_at FROM teams WHERE id = :tid"),
                {"tid": str(created.id)},
            )
        ).scalar_one_or_none()
    assert row is not None


async def test_team_name_reusable_after_soft_delete(db_engine: AsyncEngine) -> None:
    """A soft-deleted team's name can be reused (partial unique index on non-deleted rows)."""
    org = await _create_org(db_engine, f"reuse-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "reuse@test.com")

    from modulo.db.crud.team import create_team, delete_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Reusable Name", account_id=user)
        assert await delete_team(session, team.id) is True

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        new_team = await create_team(session, org_id=org, name="Reusable Name", account_id=user)
        assert new_team.name == "Reusable Name"


async def test_scim_list_groups_excludes_soft_deleted_teams(db_engine: AsyncEngine) -> None:
    """SCIM group listing must not surface soft-deleted teams (FAR-95 review)."""
    org = await _create_org(db_engine, f"scim-softdel-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "scim-softdel@test.com")

    from modulo.db.crud.scim import scim_list_groups
    from modulo.db.crud.team import create_team, delete_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        active = await create_team(session, org_id=org, name="SCIM Active", account_id=user)
        removed = await create_team(session, org_id=org, name="SCIM Removed", account_id=user)
        assert await delete_team(session, removed.id) is True

        items, total = await scim_list_groups(session, org)
        assert total == 1
        ids = {t.id for t in items}
        assert active.id in ids
        assert removed.id not in ids

        items_filtered, total_filtered = await scim_list_groups(session, org, filter_str="Removed")
        assert total_filtered == 0
        assert items_filtered == []


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

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="Mem CRUD Team", account_id=user_a)

        membership = await add_team_member(session, org_id=org, team_id=team.id, account_id=user_b, role="viewer")
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


async def test_count_owned_resources_reflects_resource_ownership(db_engine: AsyncEngine) -> None:
    """count_owned_resources sums the 4-way delete-blocking resource set per team."""
    org = await _create_org(db_engine, f"owned-cnt-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "owned-cnt@test.com")

    from modulo.db.crud.team import count_owned_resources, create_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team_a = await create_team(session, org_id=org, name="Owned A", account_id=user)
        team_b = await create_team(session, org_id=org, name="Owned B", account_id=user)

        await session.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json, 'team', :team_id)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org),
                "name": "owned-pipeline-1",
                "uid": str(user),
                "team_id": str(team_a.id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json, 'team', :team_id)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org),
                "name": "owned-pipeline-2",
                "uid": str(user),
                "team_id": str(team_a.id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO connector_instances "
                "(id, organisation_id, name, connector_type_id, status, owner_team_id, "
                "visibility, account_id, credentials_ciphertext, config_json, allowed_operations) "
                "VALUES (:id, :oid, :name, :type_id, 'active', :team_id, 'team', "
                ":uid, '\\x00'::bytea, '{}'::json, '[]'::json)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org),
                "name": "owned-connector",
                "type_id": "filesystem",
                "uid": str(user),
                "team_id": str(team_a.id),
            },
        )
        await session.execute(
            text(
                "INSERT INTO model_backends "
                "(id, organisation_id, name, display_name, provider, model_id, owner_team_id, "
                "visibility, credentials_ciphertext, default_params, account_id) "
                "VALUES (:id, :oid, :name, :display, :provider, :model, :team_id, 'team', "
                "'\\x00'::bytea, '{}'::json, :uid)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org),
                "name": "owned-backend",
                "display": "Owned Backend",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "uid": str(user),
                "team_id": str(team_a.id),
            },
        )

        counts = await count_owned_resources(session, team_ids=[team_a.id, team_b.id])
        assert counts[team_a.id] == 4  # 2 pipelines + 1 connector + 1 model backend
        assert team_b.id not in counts or counts[team_b.id] == 0


async def test_notification_endpoints_persist_after_team_soft_delete(db_engine: AsyncEngine) -> None:
    """Notification endpoints referencing a team survive a team soft-delete.

    Team deletion is a soft delete (``deleted_at`` set, row retained), so the
    ``notification_endpoints.team_id`` FK's ``ondelete=CASCADE`` never fires —
    concurrent notification CRUD against a deleted team's endpoints keeps
    working. This documents the intended soft-delete semantics.
    """
    org = await _create_org(db_engine, f"ne-softdel-{uuid.uuid4().hex[:8]}")
    user = await _create_user(db_engine, org, "ne-softdel@test.com")

    from modulo.db.crud.team import create_team, delete_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        team = await create_team(session, org_id=org, name="NE Team", account_id=user)
        ep_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO notification_endpoints "
                "(id, organisation_id, url, events, team_id, created_at, updated_at, "
                "consecutive_dead_letter_count) "
                "VALUES (:id, :oid, :url, '[\"run.completed\"]'::json, :team_id, "
                "current_timestamp, current_timestamp, 0)"
            ),
            {
                "id": ep_id,
                "oid": str(org),
                "url": "https://example.invalid/hook",
                "team_id": str(team.id),
            },
        )

        assert await delete_team(session, team.id) is True

    async with factory() as session:
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org)},
        )
        row = (
            await session.execute(
                text("SELECT team_id FROM notification_endpoints WHERE id = :eid"),
                {"eid": ep_id},
            )
        ).scalar_one_or_none()
    assert row is not None
    assert str(row) == str(team.id)
