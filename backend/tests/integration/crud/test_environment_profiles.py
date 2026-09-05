"""Integration tests for EnvironmentProfile CRUD operations.

These tests verify that environment_profiles can be created, read, updated,
and that RLS isolates profiles between organisations.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.db.models.environment_profile import EnvironmentProfile

pytestmark = pytest.mark.integration


async def _seed_account(db_engine: AsyncEngine, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {"id": str(account_id), "email": email, "name": email.split("@", maxsplit=1)[0]},
        )
    return account_id


async def test_create_environment_profile(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    account_id = await _seed_account(db_engine, f"env-crud-{uuid.uuid4().hex[:8]}@test.local")
    profile_id = uuid.uuid4()
    profile = EnvironmentProfile(
        id=profile_id,
        organisation_id=test_org,
        name="test-env",
        description="A test environment",
        provider_type="local_docker",
        image_ref="python:3.12-slim",
        capabilities_json=["docker", "network"],
        network_policy="outbound",
        persistence_policy="ephemeral",
        account_id=account_id,
    )
    rls_session.add(profile)
    await rls_session.flush()

    result = await rls_session.execute(select(EnvironmentProfile).where(EnvironmentProfile.id == profile_id))
    loaded = result.scalar_one()
    assert loaded.name == "test-env"
    assert loaded.image_ref == "python:3.12-slim"
    assert loaded.capabilities_json == ["docker", "network"]
    assert loaded.network_policy == "outbound"
    assert loaded.persistence_policy == "ephemeral"
    assert loaded.account_id == account_id
    assert loaded.deleted_at is None


async def test_read_environment_profile(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    account_id = await _seed_account(db_engine, f"env-read-{uuid.uuid4().hex[:8]}@test.local")
    profile_id = uuid.uuid4()
    profile = EnvironmentProfile(
        id=profile_id,
        organisation_id=test_org,
        name="readable-env",
        provider_type="local_docker",
        image_ref="node:20-alpine",
        capabilities_json=["docker"],
        account_id=account_id,
    )
    rls_session.add(profile)
    await rls_session.flush()

    loaded = await rls_session.get(EnvironmentProfile, profile_id)
    assert loaded is not None
    assert loaded.name == "readable-env"
    assert loaded.image_ref == "node:20-alpine"


async def test_update_environment_profile(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    account_id = await _seed_account(db_engine, f"env-upd-{uuid.uuid4().hex[:8]}@test.local")
    profile_id = uuid.uuid4()
    profile = EnvironmentProfile(
        id=profile_id,
        organisation_id=test_org,
        name="updatable-env",
        provider_type="local_docker",
        image_ref="ubuntu:22.04",
        capabilities_json=["docker"],
        account_id=account_id,
    )
    rls_session.add(profile)
    await rls_session.flush()

    profile.name = "updated-env"
    profile.capabilities_json = ["docker", "gpu"]
    await rls_session.flush()

    loaded = await rls_session.get(EnvironmentProfile, profile_id)
    assert loaded is not None
    assert loaded.name == "updated-env"
    assert loaded.capabilities_json == ["docker", "gpu"]


async def test_create_with_minimal_fields(
    rls_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    """Only required fields; defaults should be applied."""
    account_id = await _seed_account(db_engine, f"env-min-{uuid.uuid4().hex[:8]}@test.local")
    profile_id = uuid.uuid4()
    profile = EnvironmentProfile(
        id=profile_id,
        organisation_id=test_org,
        name="minimal-env",
        provider_type="local_docker",
        image_ref="alpine:latest",
        capabilities_json=[],
        account_id=account_id,
    )
    rls_session.add(profile)
    await rls_session.flush()

    loaded = await rls_session.get(EnvironmentProfile, profile_id)
    assert loaded is not None
    assert loaded.description is None
    assert not loaded.capabilities_json
    assert loaded.network_policy == "outbound"
    assert loaded.persistence_policy == "ephemeral"
    assert loaded.deleted_at is None


async def test_rls_isolation(db_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """EnvironmentProfiles from org A are invisible from org B."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from modulo.db.rls import set_rls_org

    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    account_id = await _seed_account(db_engine, f"env-rls-{uuid.uuid4().hex[:8]}@test.local")

    # Seed orgs and profiles as the superuser engine (RLS does not apply to the
    # owner role; these inserts carry no org context).
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_a), "name": "RLS Org A", "slug": f"rls-a-{org_a.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_b), "name": "RLS Org B", "slug": f"rls-b-{org_b.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO environment_profiles "
                "(id, organisation_id, name, provider_type, image_ref, capabilities_json, "
                "secret_refs_json, config_json, account_id) "
                "VALUES (:id, :org_id, :name, 'local_docker', :image, '[]'::json, '[]'::json, '{}'::json, :account_id)",
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": str(org_a),
                "name": "org-a-profile",
                "image": "img:latest",
                "account_id": str(account_id),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO environment_profiles "
                "(id, organisation_id, name, provider_type, image_ref, capabilities_json, "
                "secret_refs_json, config_json, account_id) "
                "VALUES (:id, :org_id, :name, 'local_docker', :image, '[]'::json, '[]'::json, '{}'::json, :account_id)",
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": str(org_b),
                "name": "org-b-profile",
                "image": "img:latest",
                "account_id": str(account_id),
            },
        )

    # Query from org_a's context should not see org_b's profile. app_engine
    # runs as a non-superuser role, so the RLS policy actually filters rows.
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await set_rls_org(session, org_a)
            profiles = (await session.execute(select(EnvironmentProfile))).scalars().all()
        names = {p.name for p in profiles}
        assert "org-a-profile" in names
        assert "org-b-profile" not in names
