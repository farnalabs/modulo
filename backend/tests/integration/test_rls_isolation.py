"""Cross-tenant RLS isolation integration tests.

Proves that set_config(is_local=true) is correctly scoped to the enclosing
transaction and does not leak across transactions sharing a pooled connection.
Also proves that RLS policies actually filter rows when the connection acts
as a non-superuser role.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.rls import register_rls_reset_hook, set_rls_org, set_rls_user_context

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
        try:
            async with conn.begin():
                await conn.execute(
                    text("SELECT set_config('app.organisation_id', :oid, true)"),
                    {"oid": str(org_id)},
                )
                raise RuntimeError("forced rollback")
        except RuntimeError:
            pass

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
    async with factory() as session:
        async with session.begin():
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
                        "AND table_schema = 'public'"
                    )
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
        async with db_engine.connect() as conn:
            async with conn.begin():
                for oid, name in [(org_a, "RLS-Org-A"), (org_b, "RLS-Org-B")]:
                    await conn.execute(
                        text(
                            "INSERT INTO organisations (id, name, slug, settings_json) "
                            "VALUES (:id, :name, :slug, '{}'::json)"
                        ),
                        {"id": str(oid), "name": name, "slug": f"{name}-{oid}"},
                    )
                for eid, oid in [(event_a, org_a), (event_b, org_b)]:
                    await conn.execute(
                        text(
                            "INSERT INTO audit_events "
                            "(id, organisation_id, event_type, payload_json) "
                            "VALUES (:id, :oid, 'test.rls', '{}'::json)"
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
    async with factory() as session:
        async with session.begin():
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

    Checks the five tables that should have the policy: pipelines, stages,
    connector_instances, model_backends, library_primitives.
    """
    async with db_engine.connect() as conn:
        tables_with_policy = {
            row[0]
            for row in (
                await conn.execute(text("SELECT tablename FROM pg_policies WHERE policyname = 'rls_team_isolation'"))
            ).fetchall()
        }

    expected = {"pipelines", "stages", "connector_instances", "model_backends", "library_primitives"}
    missing = expected - tables_with_policy
    assert not missing, f"Tables missing rls_team_isolation policy: {sorted(missing)}"
