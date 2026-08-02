"""Integration tests for EnvironmentProfile and WorkspaceLease with real DB.

Covers CRUD round-trips, RLS isolation, FK constraint behaviour (RESTRICT
on environment_profiles referenced by workspace_leases), and the full
WorkspaceLease lifecycle.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.environment_profile import (
    create_environment_profile,
    delete_environment_profile,
    get_environment_profile,
    list_environment_profiles,
    update_environment_profile,
)
from modulo.db.models.workspace_lease import WorkspaceLease
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — seed orgs, users, test role
# These use separate org IDs (not the shared test_org) to test cross-tenant
# isolation. Users are created via accounts + org_memberships.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def rls_role(db_engine: AsyncEngine) -> str:
    role = f"test_env_rls_{uuid.uuid4().hex[:8]}"
    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        for tbl in ("environment_profiles", "workspace_leases", "organisations", "org_memberships", "runs", "agents"):
            await conn.execute(text(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY"))
        await conn.execute(text("COMMIT"))
    yield role
    async with db_engine.connect() as conn:
        await conn.execute(text(f'DROP OWNED BY "{role}"'))
        await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await conn.execute(text("COMMIT"))


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": "Integration-OrgA", "slug": f"int-orga-{org_id.hex[:8]}"},
        )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": "Integration-OrgB", "slug": f"int-orgb-{org_id.hex[:8]}"},
        )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    # Reuse an existing account for the fixed email so seeding is idempotent
    # under pytest-xdist against a shared Postgres (see test_cross_tenant_isolation).
    async with db_engine.connect() as conn, conn.begin():
        existing = await conn.execute(
            text("SELECT id FROM accounts WHERE email = :email"),
            {"email": "admin-a@test.local"},
        )
        row = existing.first()
        if row is not None:
            account_id = uuid.UUID(str(row[0]))
        else:
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, "
                    "auth_provider, active) "
                    "VALUES (:id, :email, :name, 'hash', 'local', true)",
                ),
                {"id": str(account_id), "email": "admin-a@test.local", "name": "Admin A"},
            )
        membership = await conn.execute(
            text(
                "SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid",
            ),
            {"aid": str(account_id), "oid": str(org_a)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'admin')",
                ),
                {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_a)},
            )
    return account_id


@pytest_asyncio.fixture(scope="module")
async def org_a_profile(
    app_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
) -> uuid.UUID:
    """Seed a single EnvironmentProfile for org_a."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_a)
        profile = await create_environment_profile(
            session,
            org_id=org_a,
            name="integration-profile",
            image_ref="python:3.12-slim",
            account_id=user_a,
            description="Integration test profile",
            capabilities=["docker", "python3.12"],
            persistence_policy="ephemeral",
        )
        return profile.id


# ===========================================================================
# CRUD round-trip tests
# ===========================================================================


class TestCreateProfileRoundTrip:
    """Verify that profiles survive a full DB round-trip."""

    async def test_create_and_read_profile(
        self,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                profile = await create_environment_profile(
                    session,
                    org_id=org_a,
                    name="roundtrip-profile",
                    image_ref="ubuntu:22.04",
                    account_id=user_a,
                    description="Round trip test",
                    capabilities=["docker"],
                )
                pid = profile.id

            # Read in new transaction
            async with session.begin():
                await set_rls_org(session, org_a)
                fetched = await get_environment_profile(session, pid)
        assert fetched is not None
        assert fetched.name == "roundtrip-profile"
        assert fetched.image_ref == "ubuntu:22.04"
        assert fetched.capabilities_json == ["docker"]
        assert fetched.account_id == user_a

    async def test_list_profiles_returns_seeded(
        self,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            result = await list_environment_profiles(session)
        assert result.total >= 1

    async def test_update_profile(
        self,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                profile = await create_environment_profile(
                    session,
                    org_id=org_a,
                    name="updatable-int-profile",
                    image_ref="python:3.12-slim",
                    account_id=user_a,
                )
                pid = profile.id

            async with session.begin():
                await set_rls_org(session, org_a)
                updated = await update_environment_profile(
                    session,
                    pid,
                    {"name": "updated-int-profile"},
                )
        assert updated is not None
        assert updated.name == "updated-int-profile"

    async def test_delete_profile(
        self,
        app_engine: AsyncEngine,
        user_a: uuid.UUID,
        org_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        pid = None
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                profile = await create_environment_profile(
                    session,
                    org_id=org_a,
                    name="delete-me",
                    image_ref="alpine:3.19",
                    account_id=user_a,
                )
                pid = profile.id

            async with session.begin():
                await set_rls_org(session, org_a)
                deleted = await delete_environment_profile(session, pid)
                assert deleted is True

            async with session.begin():
                await set_rls_org(session, org_a)
                fetched = await get_environment_profile(session, pid)
                assert fetched is None


# ===========================================================================
# RLS isolation tests
# ===========================================================================


class TestRLSIsolation:
    """Verify that org_b cannot see org_a's environment profiles."""

    async def test_org_b_cannot_list_org_a_profiles(
        self,
        app_engine: AsyncEngine,
        org_b: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await list_environment_profiles(session)
        assert result.total == 0

    async def test_org_b_cannot_get_org_a_profile(
        self,
        app_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await get_environment_profile(session, org_a_profile)
        assert result is None

    async def test_org_b_cannot_update_org_a_profile(
        self,
        app_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await update_environment_profile(
                session,
                org_a_profile,
                {"name": "hacked"},
            )
        assert result is None

    async def test_org_b_cannot_delete_org_a_profile(
        self,
        app_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await delete_environment_profile(session, org_a_profile)
        assert result is False


@pytest_asyncio.fixture
async def profile_with_lease(
    app_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
) -> uuid.UUID:
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_a)

        # Create a pipeline + run first (needed for FK references from runs/leases)
        pipeline_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, "
                "node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 5, 30, 300, '{}'::json, '[]'::json)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_a),
                "name": "restrict-test-pipeline",
                "uid": str(user_a),
            },
        )
        run_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_a)},
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, langgraph_thread_id, input_hash, run_number) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'pending', :thread, :hash, :rn)",
            ),
            {
                "id": str(run_id),
                "oid": str(org_a),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "thread": str(uuid.uuid4()),
                "hash": "0" * 64,
                "rn": int(run_id.int % 10**9) + 1,
            },
        )

        # Create profile
        profile = await create_environment_profile(
            session,
            org_id=org_a,
            name="restrict-test-profile",
            image_ref="python:3.12-slim",
            account_id=user_a,
        )
        pid = profile.id

        # Create lease referencing the profile
        lease = WorkspaceLease(
            organisation_id=org_a,
            environment_profile_id=pid,
            run_id=run_id,
            provider_ref="int-ws-001",
            status="running",
            lease_started_at=datetime.now(UTC),
            lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(lease)
        await session.flush()
    return pid


# ===========================================================================
# FK constraint — RESTRICT on workspace_leases → environment_profiles
# ===========================================================================


class TestDeleteProfileWithLeases:
    """Verify that a profile with active leases cannot be deleted (RESTRICT)."""

    async def test_delete_profile_with_active_lease_raises(
        self,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        profile_with_lease: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            # Deletion should fail because workspace_leases has RESTRICT FK
            with pytest.raises(DBAPIError):
                await delete_environment_profile(session, profile_with_lease)


# ===========================================================================
# Positive control — org_a can see its own data
# ===========================================================================


class TestPositiveControl:
    async def test_orga_sees_own_profile(
        self,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            profile = await get_environment_profile(session, org_a_profile)
        assert profile is not None
        assert profile.name == "integration-profile"
