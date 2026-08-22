"""Cross-tenant RLS isolation integration tests.

Proves that set_config(is_local=true) is correctly scoped to the enclosing
transaction and does not leak across transactions sharing a pooled connection.
Also proves that RLS policies actually filter rows when the connection acts
as a non-superuser role.
"""

import contextlib
import uuid

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session as SASession

from modulo.db.rls import (
    _inject_tenant_filter,
    register_rls_reset_hook,
    set_rls_org,
    set_rls_user_context,
)

# ---------------------------------------------------------------------------
# SET LOCAL / set_config scoping tests
# ---------------------------------------------------------------------------


async def test_set_local_resets_after_commit(db_engine: AsyncEngine) -> None:
    """set_config(is_local=true) must revert to empty after the transaction commits."""
    org_id = uuid.uuid4()

    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            mid_tx = (await conn.execute(text("SELECT current_setting('app.organisation_id', true)"))).scalar()
            assert mid_tx == str(org_id), "org_id should be visible mid-transaction"

        post_commit = (await conn.execute(text("SELECT current_setting('app.organisation_id', true)"))).scalar()
        assert post_commit in (None, ""), f"org_id leaked after commit: {post_commit!r}"


async def test_set_local_resets_after_rollback(db_engine: AsyncEngine) -> None:
    """set_config(is_local=true) must revert to empty after the transaction rolls back."""
    org_id = uuid.uuid4()

    async with db_engine.connect() as conn:
        # Use a savepoint to force a clean rollback without catching RuntimeError
        savepoint = await conn.begin_nested()
        await conn.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        await savepoint.rollback()

        post_rollback = (await conn.execute(text("SELECT current_setting('app.organisation_id', true)"))).scalar()
        assert post_rollback in (None, ""), f"org_id leaked after rollback: {post_rollback!r}"


async def test_second_transaction_does_not_inherit_org_id(db_engine: AsyncEngine) -> None:
    """A second transaction on the same connection must start without org context."""
    org_id = uuid.uuid4()

    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )

        async with conn.begin():
            val = (await conn.execute(text("SELECT current_setting('app.organisation_id', true)"))).scalar()
            assert val in (None, ""), f"org_id leaked into second transaction: {val!r}"


# ---------------------------------------------------------------------------
# set_rls_org helper tests
# ---------------------------------------------------------------------------


async def test_set_rls_org_requires_active_transaction(db_engine: AsyncEngine) -> None:
    """set_rls_org raises if called without an active transaction."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await set_rls_org(session, uuid.uuid4())


async def test_set_rls_org_sets_correct_guc(db_engine: AsyncEngine) -> None:
    """set_rls_org must write app.organisation_id, not any other GUC."""
    org_id = uuid.uuid4()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        val = (await session.execute(text("SELECT current_setting('app.organisation_id', true)"))).scalar()
        assert val == str(org_id)


# ---------------------------------------------------------------------------
# Policy existence test (derived from schema, not hardcoded list)
# ---------------------------------------------------------------------------


async def test_rls_policies_exist_on_all_org_scoped_tables(
    db_engine: AsyncEngine,
) -> None:
    """Migration 0002 must have created rls_org_isolation on every org-scoped table.

    Expected tables are derived from information_schema (tables with an
    organisation_id column) so this test stays accurate as new tables are added.
    """
    async with db_engine.connect() as conn:
        org_scoped = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE column_name = 'organisation_id' "
                        "AND table_schema = 'public'",
                    ),
                )
            ).fetchall()
        }

        tables_with_policy = {
            row[0]
            for row in (
                await conn.execute(text("SELECT tablename FROM pg_policies WHERE policyname = 'rls_org_isolation'"))
            ).fetchall()
        }

    # organisations table has no organisation_id column — correctly excluded
    expected = org_scoped - {"organisations"}
    missing = expected - tables_with_policy
    assert not missing, f"Tables missing rls_org_isolation policy: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Actual RLS enforcement test (non-superuser role)
# ---------------------------------------------------------------------------


async def test_rls_filters_rows_for_non_superuser(db_engine: AsyncEngine) -> None:
    """RLS must make org A rows invisible when org B context is active.

    Uses SET ROLE to drop superuser privileges so that RLS policies apply,
    then inserts audit_events (minimal FK requirements) for two orgs and
    verifies that each org can only see its own rows.
    """
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    event_a = uuid.uuid4()
    event_b = uuid.uuid4()
    role = f"test_rls_{uuid.uuid4().hex[:8]}"

    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}"'))
        await conn.execute(text(f'GRANT SELECT, INSERT ON organisations, audit_events TO "{role}"'))
        await conn.execute(text("COMMIT"))

    try:
        # Seed: insert orgs and one audit_event per org (as superuser, bypasses RLS)
        async with db_engine.connect() as conn, conn.begin():
            for oid, name in [(org_a, "RLS-Org-A"), (org_b, "RLS-Org-B")]:
                await conn.execute(
                    text(
                        "INSERT INTO organisations (id, name, slug, settings_json) "
                        "VALUES (:id, :name, :slug, '{}'::json)",
                    ),
                    {"id": str(oid), "name": name, "slug": f"{name}-{oid}"},
                )
            for eid, oid in [(event_a, org_a), (event_b, org_b)]:
                await conn.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(id, organisation_id, event_type, payload_json) "
                        "VALUES (:id, :oid, 'test.rls', '{}'::json)",
                    ),
                    {"id": str(eid), "oid": str(oid)},
                )

        # Enforcement: as non-superuser with org_a context, only event_a visible
        async with db_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                await conn.execute(
                    text("SELECT set_config('app.organisation_id', :oid, true)"),
                    {"oid": str(org_a)},
                )
                visible = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text("SELECT id::text FROM audit_events WHERE id = ANY(:ids)"),
                            {"ids": [str(event_a), str(event_b)]},
                        )
                    ).fetchall()
                }
            assert visible == {str(event_a)}, f"org_a should only see its own event; got {visible}"

        # Enforcement: as non-superuser with org_b context, only event_b visible
        async with db_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                await conn.execute(
                    text("SELECT set_config('app.organisation_id', :oid, true)"),
                    {"oid": str(org_b)},
                )
                visible = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text("SELECT id::text FROM audit_events WHERE id = ANY(:ids)"),
                            {"ids": [str(event_a), str(event_b)]},
                        )
                    ).fetchall()
                }
            assert visible == {str(event_b)}, f"org_b should only see its own event; got {visible}"

    finally:
        async with db_engine.connect() as conn:
            # DROP OWNED BY revokes all privileges before the role is removed
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            await conn.execute(text("COMMIT"))


# ---------------------------------------------------------------------------
# set_rls_user_context helper tests
# ---------------------------------------------------------------------------


async def test_set_rls_user_context_requires_active_transaction(db_engine: AsyncEngine) -> None:
    """set_rls_user_context raises if called without an active transaction."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await set_rls_user_context(session, uuid.uuid4(), "admin")


async def test_set_rls_user_context_sets_gucs(db_engine: AsyncEngine) -> None:
    """set_rls_user_context must write app.user_id and app.org_role."""
    user_id = uuid.uuid4()
    org_role = "operator"
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_user_context(session, user_id, org_role)
        uid_val = (await session.execute(text("SELECT current_setting('app.user_id', true)"))).scalar()
        role_val = (await session.execute(text("SELECT current_setting('app.org_role', true)"))).scalar()
        assert uid_val == str(user_id)
        assert role_val == org_role


async def test_set_rls_user_context_resets_after_commit(db_engine: AsyncEngine) -> None:
    """set_rls_user_context GUCs must revert after transaction commit."""
    user_id = uuid.uuid4()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await set_rls_user_context(session, user_id, "admin")

        post_uid = (await session.execute(text("SELECT current_setting('app.user_id', true)"))).scalar()
        post_role = (await session.execute(text("SELECT current_setting('app.org_role', true)"))).scalar()
        assert post_uid in (None, ""), f"user_id leaked after commit: {post_uid!r}"
        assert post_role in (None, ""), f"org_role leaked after commit: {post_role!r}"


# ---------------------------------------------------------------------------
# Pool checkout reset hook test
# ---------------------------------------------------------------------------


async def test_register_rls_reset_hook_clears_gucs_on_checkout(db_engine: AsyncEngine) -> None:
    """register_rls_reset_hook must set all three GUCs to empty string on checkout.

    Sets a session-level default, registers the hook, checks out a connection,
    and verifies the GUCs are empty.
    """
    register_rls_reset_hook(db_engine)

    # Set session-level defaults (simulates stale context from a prior request)
    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', 'stale-org-id', false)"))
        await conn.execute(text("SELECT set_config('app.user_id', 'stale-user-id', false)"))
        await conn.execute(text("SELECT set_config('app.org_role', 'stale-role', false)"))
        await conn.commit()

    # On next checkout, the reset hook should clear these
    async with db_engine.connect() as conn:
        org_val = (await conn.execute(text("SELECT current_setting('app.organisation_id', true)"))).scalar()
        uid_val = (await conn.execute(text("SELECT current_setting('app.user_id', true)"))).scalar()
        role_val = (await conn.execute(text("SELECT current_setting('app.org_role', true)"))).scalar()
        assert org_val in (None, ""), f"org_id not cleared: {org_val!r}"
        assert uid_val in (None, ""), f"user_id not cleared: {uid_val!r}"
        assert role_val in (None, ""), f"org_role not cleared: {role_val!r}"


# ---------------------------------------------------------------------------
# Team-scoped RLS policy existence test
# ---------------------------------------------------------------------------


async def test_rls_team_isolation_policies_exist(db_engine: AsyncEngine) -> None:
    """Migration 0025 must have created rls_team_isolation on team-scoped tables.

    Checks the tables that should have the policy: pipelines,
    connector_instances, model_backends, library_primitives.
    """
    async with db_engine.connect() as conn:
        tables_with_policy = {
            row[0]
            for row in (
                await conn.execute(text("SELECT tablename FROM pg_policies WHERE policyname = 'rls_team_isolation'"))
            ).fetchall()
        }

    expected = {"pipelines", "connector_instances", "model_backends", "library_primitives"}
    missing = expected - tables_with_policy
    assert not missing, f"Tables missing rls_team_isolation policy: {sorted(missing)}"


async def test_team_scoped_tables_have_no_org_only_policy(db_engine: AsyncEngine) -> None:
    """The OR'd org-only RLS policy was dropped on team-scoped tables (0122).

    Regression guard for the cross-team leak: a team-scoped table must carry
    ONLY the team-visibility policy (which includes the org check), never the
    org-only policy that ORs in every org row. lifecycle_maps is org-only by
    design and must keep its org policy.

    This pins the FINAL policy state on a real Postgres (migrations applied),
    so the policy-creating migrations, ``team_scope.py``, and the migration's
    hardcoded ``_TEAM_SCOPED_TABLES`` tuple cannot drift independently without
    this test failing.
    """
    team_scoped = {
        "pipelines",
        "connector_instances",
        "model_backends",
        "environment_profiles",
        "library_primitives",
    }
    org_only_tables = {"lifecycle_maps"}

    async with db_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT tablename, policyname FROM pg_policies"))).fetchall()

    policies: dict[str, set[str]] = {}
    for table, policy in rows:
        policies.setdefault(table, set()).add(policy)

    for table in team_scoped:
        assert "rls_team_isolation" in policies.get(table, set()), f"{table} missing team policy"
        assert "rls_org_isolation" not in policies.get(table, set()), f"{table} still has org-only policy"

    for table in org_only_tables:
        assert "rls_org_isolation" in policies.get(table, set()), f"{table} missing org policy"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": name,
                "slug": f"{name}-{org_id.hex[:8]}",
            },
        )
    return org_id


async def _create_account(db_engine: AsyncEngine, email: str) -> uuid.UUID:
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
    return account_id


async def _set_rls(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.organisation_id', :oid, true)"),
        {"oid": str(org_id)},
    )


# ---------------------------------------------------------------------------
# Migration 0060 — rls_team_isolation policy correctness after column rename
# ---------------------------------------------------------------------------


async def test_team_memberships_isolated_by_org_rls(
    db_engine: AsyncEngine,
) -> None:
    """Team membership rows are correctly isolated by org-scoped RLS.

    After the users→accounts+org_memberships reconciliation (0108_schema_org_identity),
    the rls_org_isolation policy on team_memberships (org-scoped via OrgScoped)
    must use ``organisation_id`` to prevent cross-org membership leaks.
    This test creates accounts in different teams within the same org and
    verifies that membership queries are scoped to the correct team.
    """
    from modulo.db.crud.team import create_team
    from modulo.db.crud.team_membership import add_team_member, list_team_members

    org = await _create_org(db_engine, f"rls-team-membership-{uuid.uuid4().hex[:8]}")
    account_admin = await _create_account(db_engine, "admin@rls-membership.com")
    account_a = await _create_account(db_engine, "member-a@rls-membership.com")
    account_b = await _create_account(db_engine, "member-b@rls-membership.com")

    # The check_team_privilege_cap trigger requires every account to hold an
    # org_membership row in the org before it can be added to a team — without
    # it the org role resolves to NULL and the trigger raises
    # "Team role ... exceeds org role <NULL>".
    async with db_engine.connect() as conn, conn.begin():
        for account_id, role in (
            (account_admin, "admin"),
            (account_a, "viewer"),
            (account_b, "operator"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:id, :aid, :oid, :role)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "aid": str(account_id),
                    "oid": str(org),
                    "role": role,
                },
            )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        await _set_rls(session, org)
        team_a = await create_team(session, org_id=org, name="RLS Team A", account_id=account_admin)
        await add_team_member(session, org_id=org, team_id=team_a.id, account_id=account_a, role="viewer")

    async with factory() as session, session.begin():
        await _set_rls(session, org)
        team_b = await create_team(session, org_id=org, name="RLS Team B", account_id=account_admin)
        await add_team_member(session, org_id=org, team_id=team_b.id, account_id=account_b, role="operator")

    # Member A should see only Team A's members
    async with factory() as session, session.begin():
        await _set_rls(session, org)
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(account_a)},
        )
        await session.execute(
            text("SELECT set_config('app.org_role', :role, true)"),
            {"role": "viewer"},
        )
        members_a = await list_team_members(session, team_id=team_a.id, page=1, page_size=50)
        assert len(members_a.items) == 1, "Member A should see exactly 1 member in Team A"
        assert members_a.items[0].account_id == account_a

    # Member B should see only Team B's members
    async with factory() as session, session.begin():
        await _set_rls(session, org)
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(account_b)},
        )
        await session.execute(
            text("SELECT set_config('app.org_role', :role, true)"),
            {"role": "operator"},
        )
        members_b = await list_team_members(session, team_id=team_b.id, page=1, page_size=50)
        assert len(members_b.items) == 1, "Member B should see exactly 1 member in Team B"
        assert members_b.items[0].account_id == account_b

    # Admin sees all members
    async with factory() as session, session.begin():
        await _set_rls(session, org)
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(account_admin)},
        )
        await session.execute(
            text("SELECT set_config('app.org_role', :role, true)"),
            {"role": "admin"},
        )
        members_admin_a = await list_team_members(session, team_id=team_a.id, page=1, page_size=50)
        assert len(members_admin_a.items) == 1, "Admin should see 1 member in Team A"


# ---------------------------------------------------------------------------
# ORM tenant filter tests (SQLite — RLS is Postgres-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_tenant_filter_select_update_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """_inject_tenant_filter must add WHERE organisation_id to SELECT/UPDATE/DELETE.

    Uses SQLite in-memory with a minimal model to verify the ORM listener
    correctly filters by organisation_id when session.info["org_id"] is set,
    and does not inject when it is not set.
    """

    from sqlalchemy import String, Uuid
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    # Force non-Postgres path so tenant filter activates
    monkeypatch.setenv("MODULO_DB", "sqlite")

    class _TenantTestBase(DeclarativeBase):
        pass

    class _Item(_TenantTestBase):
        __tablename__ = "tenant_items"
        id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
        organisation_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
        name: Mapped[str] = mapped_column(String(50), nullable=False)

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_TenantTestBase.metadata.create_all)

    # Register tenant filter directly (bypass register_tenant_filter which needs env vars)
    with contextlib.suppress(Exception):
        event.remove(SASession, "do_orm_execute", _inject_tenant_filter)
    event.listen(SASession, "do_orm_execute", _inject_tenant_filter)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()

    # Seed data: 2 items per org
    async with factory() as session, session.begin():
        session.add_all(
            [
                _Item(organisation_id=org_a, name="a-1"),
                _Item(organisation_id=org_a, name="a-2"),
                _Item(organisation_id=org_b, name="b-1"),
                _Item(organisation_id=org_b, name="b-2"),
            ]
        )

    # Test SELECT filtering: with org_a context, only org_a rows visible
    async with factory() as session, session.begin():
        session.info["org_id"] = org_a
        result = (
            await session.scalars(
                select(_Item).where(_Item.name.in_(["a-1", "b-1"])),
            )
        ).all()
        names = {r.name for r in result}
        assert names == {"a-1"}, f"Expected only 'a-1', got {names}"

    # Test SELECT filtering: with org_b context, only org_b rows visible
    async with factory() as session, session.begin():
        session.info["org_id"] = org_b
        result = (
            await session.scalars(
                select(_Item).where(_Item.name.in_(["a-1", "b-1"])),
            )
        ).all()
        names = {r.name for r in result}
        assert names == {"b-1"}, f"Expected only 'b-1', got {names}"

    # Test no filtering: without org_id set, all rows returned
    async with factory() as session, session.begin():
        result = (await session.scalars(select(_Item))).all()
        assert len(result) == 4, f"Expected 4 items without filter, got {len(result)}"

    # Test UPDATE filtering: only org_a's row updated
    async with factory() as session, session.begin():
        session.info["org_id"] = org_a
        await session.execute(
            update(_Item).where(_Item.name == "a-1").values(name="a-1-updated"),
        )

    async with factory() as session, session.begin():
        result = (
            (
                await session.execute(
                    text("SELECT name FROM tenant_items WHERE name LIKE '%updated'"),
                )
            )
            .scalars()
            .all()
        )
        assert len(result) == 1, f"Expected 1 updated item, got {len(result)}"

    # Test DELETE filtering: only org_b's row deleted
    async with factory() as session, session.begin():
        session.info["org_id"] = org_b
        await session.execute(
            delete(_Item).where(_Item.name == "b-2"),
        )

    async with factory() as session, session.begin():
        result = (
            (
                await session.execute(
                    text("SELECT name FROM tenant_items ORDER BY name"),
                )
            )
            .scalars()
            .all()
        )
        assert "b-2" not in result, "b-2 should have been deleted"
        assert "a-1-updated" in result, "a-1-updated should remain"

    await engine.dispose()
