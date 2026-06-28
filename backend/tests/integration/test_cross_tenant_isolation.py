"""Cross-tenant data isolation integration tests.

Proves that RLS policies prevent OrgB from seeing or modifying OrgA's data
across all org-scoped entity types (pipeline, agent, schema, connector_instance).
Uses SET ROLE to drop superuser privileges so that RLS policies apply.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.agent import create_agent, get_agent, list_agents
from modulo.db.crud.connector_instance import (
    create_connector_instance,
    get_connector_instance,
    list_connector_instances,
)
from modulo.db.crud.model_backend import create_model_backend
from modulo.db.crud.pipeline import (
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    list_pipelines,
    update_pipeline,
)
from modulo.db.crud.schema import create_schema, create_schema_version, get_schema, list_schemas
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_test_role(engine: AsyncEngine) -> str:
    role = f"test_rls_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await conn.execute(text("COMMIT"))
    return role


async def _drop_role(engine: AsyncEngine, role: str) -> None:
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP OWNED BY "{role}"'))
        await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await conn.execute(text("COMMIT"))


async def _seed_org(engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
            )
    return org_id


async def _seed_user(engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO users (id, organisation_id, email, display_name, "
                    "org_role, auth_provider, active, password_hash) "
                    "VALUES (:id, :oid, :email, :name, 'admin', 'local', true, 'hash')"
                ),
                {"id": str(user_id), "oid": str(org_id), "email": email, "name": f"Admin {email}"},
            )
    return user_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def rls_role(db_engine: AsyncEngine) -> str:
    role = await _create_test_role(db_engine)
    # Ensure FORCE RLS is applied for all test tables
    async with db_engine.connect() as conn:
        for tbl in ("pipelines", "agents", "schemas", "connector_instances",
                     "schema_versions", "model_backends"):
            await conn.execute(text(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY"))
        await conn.commit()
    yield role
    await _drop_role(db_engine, role)


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "Tenant-OrgA")


@pytest_asyncio.fixture(scope="module")
async def org_b(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "Tenant-OrgB")


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "admin-a@test.local")


@pytest_asyncio.fixture(scope="module")
async def user_b(db_engine: AsyncEngine, org_b: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_b, "admin-b@test.local")


@pytest_asyncio.fixture(scope="module")
async def org_a_data(
    db_engine: AsyncEngine,
    org_a: uuid.UUID,
    user_a: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Seed OrgA with entities across all org-scoped types."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {}
    async with factory() as session:
        async with session.begin():
            await set_rls_org(session, org_a)

            schema = await create_schema(session, org_id=org_a, name="OrgA-Schema", created_by=user_a)
            ids["schema"] = schema.id
            await create_schema_version(
                session,
                org_id=org_a,
                schema_id=schema.id,
                version="1.0.0",
                version_number=1,
                definition_json={"type": "object"},
                created_by=user_a,
            )
            ids["schema_version"] = schema.id  # simplified

            mb = await create_model_backend(
                session,
                org_id=org_a,
                name="orga-stub",
                display_name="OrgA Stub",
                provider="ollama",
                model_id="stub-model",
                credentials_ciphertext=b"encrypted",
                created_by=user_a,
            )
            ids["model_backend"] = mb.id

            agent = await create_agent(
                session,
                org_id=org_a,
                name="OrgA-Agent",
                created_by=user_a,
                input_schema_id=schema.id,
                input_schema_version="1.0.0",
                output_schema_id=schema.id,
                output_schema_version="1.0.0",
                prompt_template="You are a test agent.",
                model_backend_id=mb.id,
            )
            ids["agent"] = agent.id

            pipeline = await create_pipeline(session, org_id=org_a, name="OrgA-Pipeline", created_by=user_a)
            ids["pipeline"] = pipeline.id

            ci = await create_connector_instance(
                session,
                org_id=org_a,
                name="OrgA-Connector",
                connector_type_id="stub",
                owner_id=user_a,
                credentials_ciphertext=b"cipher",
            )
            ids["connector_instance"] = ci.id
    return ids


# ---------------------------------------------------------------------------
# Tests — data isolation: OrgB cannot see OrgA's data
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("org_a_data")
class TestCrossTenantPipelines:
    async def test_list_pipelines_hides_org_a(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await list_pipelines(session)
                assert result.total == 0

    async def test_get_pipeline_returns_none_for_org_a_id(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await get_pipeline(session, org_a_data["pipeline"])
                assert result is None

    async def test_update_pipeline_returns_none_for_org_a_id(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await update_pipeline(session, org_a_data["pipeline"], {"name": "Hacked"})
                assert result is None

    async def test_delete_pipeline_returns_false_for_org_a_id(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await delete_pipeline(session, org_a_data["pipeline"])
                assert result is False


class TestCrossTenantAgents:
    async def test_list_agents_hides_org_a(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await list_agents(session)
                assert result.total == 0

    async def test_get_agent_returns_none_for_org_a_id(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await get_agent(session, org_a_data["agent"])
                assert result is None


class TestCrossTenantSchemas:
    async def test_list_schemas_hides_org_a(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await list_schemas(session)
                assert result.total == 0

    async def test_get_schema_returns_none_for_org_a_id(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await get_schema(session, org_a_data["schema"])
                assert result is None


class TestCrossTenantConnectorInstances:
    async def test_list_connector_instances_hides_org_a(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await list_connector_instances(session)
                assert result.total == 0

    async def test_get_connector_instance_returns_none_for_org_a_id(
        self,
        db_engine: AsyncEngine,
        org_b: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_b)
                result = await get_connector_instance(session, org_a_data["connector_instance"])
                assert result is None


# ---------------------------------------------------------------------------
# Positive control — OrgA can see its own data
# ---------------------------------------------------------------------------


class TestPositiveControl:
    async def test_orga_sees_own_pipeline(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                result = await get_pipeline(session, org_a_data["pipeline"])
                assert result is not None
                assert result.name == "OrgA-Pipeline"

    async def test_orga_sees_own_agent(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                result = await get_agent(session, org_a_data["agent"])
                assert result is not None
                assert result.name == "OrgA-Agent"

    async def test_orga_sees_own_schema(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                result = await get_schema(session, org_a_data["schema"])
                assert result is not None
                assert result.name == "OrgA-Schema"

    async def test_orga_sees_own_connector_instance(
        self,
        db_engine: AsyncEngine,
        org_a: uuid.UUID,
        org_a_data: dict[str, uuid.UUID],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            async with session.begin():
                await set_rls_org(session, org_a)
                result = await get_connector_instance(session, org_a_data["connector_instance"])
                assert result is not None
                assert result.name == "OrgA-Connector"
