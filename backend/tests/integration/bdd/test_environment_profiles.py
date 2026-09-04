"""Integration tests for EnvironmentProfile with a real DB.

Covers CRUD round-trips, RLS isolation, and delete behaviour. The
workspace_leases RESTRICT constraint was removed with the lease scaffolding
(FAR-587 / ADR 029) ? hard deletion is no longer blocked.
"""

import uuid

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
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — seed orgs, users
# These use separate org IDs (not the shared test_org) to test cross-tenant
# isolation. Users are created via accounts + org_memberships.
# ---------------------------------------------------------------------------


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
            account_id=user_a,
            description="Integration test profile",
            provider_type="local_docker",
            image_ref="python:3.12-slim",
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
                    account_id=user_a,
                    provider_type="local_docker",
                    image_ref="ubuntu:22.04",
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
        assert fetched.provider_type == "local_docker"
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
                    account_id=user_a,
                    provider_type="local_docker",
                    image_ref="python:3.12-slim",
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
                    account_id=user_a,
                    provider_type="local_docker",
                    image_ref="alpine:3.19",
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
async def deletable_profile(
    app_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
) -> uuid.UUID:
    """Seed a profile that the delete test may hard-delete."""
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_a)
        profile = await create_environment_profile(
            session,
            org_id=org_a,
            name="hard-delete-test-profile",
            account_id=user_a,
            provider_type="local_docker",
            image_ref="python:3.12-slim",
        )
        return profile.id


# ===========================================================================
# Delete behaviour ? no lease RESTRICT since FAR-587
# ===========================================================================


class TestDeleteProfileWithoutLeaseTable:
    """Hard delete succeeds: the workspace_leases RESTRICT FK no longer exists."""

    async def test_delete_profile_succeeds(
        self,
        app_engine: AsyncEngine,
        org_a: uuid.UUID,
        deletable_profile: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, org_a)
            deleted = await delete_environment_profile(session, deletable_profile)
            assert deleted is True

            fetched = await get_environment_profile(session, deletable_profile, include_deleted=True)
        assert fetched is not None
        assert fetched.deleted_at is not None
