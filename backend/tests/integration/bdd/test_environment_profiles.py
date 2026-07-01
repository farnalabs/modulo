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
# Fixtures - seed orgs, users, test role
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def rls_role(db_engine: AsyncEngine) -> str:
    role = f"test_env_rls_{uuid.uuid4().hex[:8]}"
    async with db_engine.connect() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        for tbl in ("environment_profiles", "workspace_leases", "organisations", "users", "runs", "agents"):
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
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, '{}'::json)",
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
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": "Integration-OrgB", "slug": f"int-orgb-{org_id.hex[:8]}"},
        )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO users (id, organisation_id, email, display_name, "
                "org_role, auth_provider, active, password_hash) "
                "VALUES (:id, :oid, :email, :name, 'admin', 'local', true, 'hash')",
            ),
            {"id": str(user_id), "oid": str(org_a), "email": "admin-a@test.local", "name": "Admin A"},
        )
    return user_id


@pytest_asyncio.fixture(scope="module")
async def org_a_profile(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
) -> uuid.UUID:
    """Seed a single EnvironmentProfile for org_a."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_a)
        profile = await create_environment_profile(
            session,
            org_id=org_a,
            name="integration-profile",
            image_ref="python:3.12-slim",
            created_by=user_a,
            description="Integration test profile",
            capabilities=["docker", "python3.12"],
            egress_policy="allow_all",
            timeout_seconds=3600,
            resource_limits={"cpu": "1", "memory": "512Mi"},
            persistence_policy={},
        )
        return profile.id


# ===========================================================================
# CRUD round-trip tests
# ===========================================================================


class TestCreateProfileRoundTrip:
    """Verify that profiles survive a full DB round-trip."""

    async def test_create_and_read_profile(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                profile = await create_environment_profile(
                    session,
                    org_id=org_a,
                    name="roundtrip-profile",
                    image_ref="ubuntu:22.04",
                    created_by=user_a,
                    description="Round trip test",
                    capabilities=["docker"],
                    egress_policy="deny_all",
                    timeout_seconds=7200,
                )
                pid = profile.id

            # Read in new transaction
            async with session.begin():
                await set_rls_org(session, org_a)
                fetched = await get_environment_profile(session, pid)
        assert fetched is not None
        assert fetched.name == "roundtrip-profile"
        assert fetched.image_ref == "ubuntu:22.04"
        assert fetched.capabilities == ["docker"]
        assert fetched.egress_policy == "deny_all"
        assert fetched.timeout_seconds == 7200
        assert fetched.is_active is True
        assert fetched.created_by == user_a

    async def test_list_profiles_returns_seeded(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            result = await list_environment_profiles(session)
        assert result.total >= 1

    async def test_update_profile(
        self,
        db_engine: AsyncEngine,
        org_a_profile: uuid.UUID,
        org_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            updated = await update_environment_profile(
                session,
                org_a_profile,
                {"name": "updated-int-profile", "timeout_seconds": 7200},
            )
        assert updated is not None
        assert updated.name == "updated-int-profile"
        assert updated.timeout_seconds == 7200

    async def test_delete_profile(
        self,
        db_engine: AsyncEngine,
        user_a: uuid.UUID,
        org_a: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        pid = None
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                profile = await create_environment_profile(
                    session,
                    org_id=org_a,
                    name="delete-me",
                    image_ref="alpine:3.19",
                    created_by=user_a,
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
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await list_environment_profiles(session)
        assert result.total == 0

    async def test_org_b_cannot_get_org_a_profile(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await get_environment_profile(session, org_a_profile)
        assert result is None

    async def test_org_b_cannot_update_org_a_profile(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await update_environment_profile(
                session, org_a_profile, {"name": "hacked"},
            )
        assert result is None

    async def test_org_b_cannot_delete_org_a_profile(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_b)
            result = await delete_environment_profile(session, org_a_profile)
        assert result is False


# ===========================================================================
# FK constraint — RESTRICT on workspace_leases → environment_profiles
# ===========================================================================


class TestDeleteProfileWithLeases:
    """Verify that a profile with active leases cannot be deleted (RESTRICT)."""

    @pytest_asyncio.fixture(scope="class")
    async def profile_with_lease(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> uuid.UUID:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)

            # Create a run first (needed for FK reference from workspace_leases)
            run_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, status, langgraph_thread_id) "
                    "VALUES (:id, :oid, 'pending', :thread)",
                ),
                {"id": str(run_id), "oid": str(org_a), "thread": str(uuid.uuid4())},
            )

            # Create profile
            profile = await create_environment_profile(
                session,
                org_id=org_a,
                name="restrict-test-profile",
                image_ref="python:3.12-slim",
                created_by=user_a,
            )
            pid = profile.id

            # Create lease referencing the profile
            lease = WorkspaceLease(
                organisation_id=org_a,
                environment_profile_id=pid,
                run_id=run_id,
                provider_ref="int-ws-001",
                status="active",
                started_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            session.add(lease)
            await session.flush()
        return pid

    async def test_delete_profile_with_active_lease_raises(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        profile_with_lease: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            # Deletion should fail because workspace_leases has RESTRICT FK
            with pytest.raises(Exception):
                await delete_environment_profile(session, profile_with_lease)


# ===========================================================================
# Positive control — org_a can see its own data
# ===========================================================================


class TestPositiveControl:
    async def test_orga_sees_own_profile(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_a_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            profile = await get_environment_profile(session, org_a_profile)
        assert profile is not None
        assert profile.name == "integration-profile"
