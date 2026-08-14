"""Unit tests for modulo.db.seed — the system-schema seeding routine.

``seed_system_schemas`` is invoked on organisation creation
(``modulo.db.crud.organisation``) and again at startup for every existing
organisation (``modulo.api.main._seed_system_schemas``). These tests lock the
contract of that routine against a real (hermetic, in-memory SQLite) async
session:

  * every spec in ``SYSTEM_SCHEMAS`` produces exactly one ``Schema`` and one
    published ``SchemaVersion`` with the full attribute set (name, abstract
    name, account/org scoping, ``system=True``, description, version metadata,
    and the exact JSON definition).
  * idempotency — a second run over the same org is a no-op (no duplicate
    schemas, no duplicate versions).
  * partial pre-existing state — only the missing schemas are created, and no
    ``SchemaVersion`` is fabricated for a schema that already existed.
  * org scoping — the existence check is ``(organisation_id, name)`` scoped; a
    same-named schema in a *different* organisation never suppresses creation.

SQLite with foreign keys disabled stands in for Postgres; no external
database is required.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.models.base import Base
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.seed import SYSTEM_SCHEMAS, seed_system_schemas


def _needed_tables() -> list:
    """Tables required by ``Schema``/``SchemaVersion`` incl. FK dependencies.

    ``create_all`` is scoped to these five tables because other models use
    Postgres-only column types (e.g. ARRAY) that SQLite cannot render.
    """
    wanted = {"accounts", "organisations", "schema_folders", "schemas", "schema_versions"}
    return [t for t in Base.metadata.sorted_tables if t.name in wanted]


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_needed_tables()))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_all(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    await seed_system_schemas(session, org_id, account_id)
    await session.commit()
    session.expire_all()


async def _schemas(session: AsyncSession, org_id: uuid.UUID) -> list[Schema]:
    return list(
        (await session.execute(select(Schema).where(Schema.organisation_id == org_id).order_by(Schema.name))).scalars()
    )


async def _versions_for(session: AsyncSession, schema_id: uuid.UUID) -> list[SchemaVersion]:
    return list(
        (
            await session.execute(
                select(SchemaVersion).where(SchemaVersion.schema_id == schema_id).order_by(SchemaVersion.version_number)
            )
        ).scalars()
    )


class TestSeedCreatesSchemas:
    async def test_creates_one_schema_per_system_spec(self, session: AsyncSession) -> None:
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        await _seed_all(session, org_id, account_id)

        schemas = await _schemas(session, org_id)
        assert {s.name for s in schemas} == {spec["name"] for spec in SYSTEM_SCHEMAS}

    async def test_schema_attributes_match_spec(self, session: AsyncSession) -> None:
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        await _seed_all(session, org_id, account_id)

        schemas = {s.name: s for s in await _schemas(session, org_id)}
        for spec in SYSTEM_SCHEMAS:
            schema = schemas[spec["name"]]
            assert schema.abstract_name == spec["abstract_name"]
            assert schema.description == f"System schema: {spec['name']}"
            assert schema.system is True
            assert schema.account_id == account_id
            assert schema.organisation_id == org_id

    async def test_every_spec_gets_a_published_v1_version(self, session: AsyncSession) -> None:
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        await _seed_all(session, org_id, account_id)

        schemas = {s.name: s for s in await _schemas(session, org_id)}
        for spec in SYSTEM_SCHEMAS:
            schema = schemas[spec["name"]]
            versions = await _versions_for(session, schema.id)
            assert len(versions) == 1
            version = versions[0]
            assert version.version == spec["version"]
            assert version.version_number == 1
            assert version.published is True
            assert version.definition_json == spec["definition"]
            assert version.account_id == account_id
            assert version.organisation_id == org_id
            assert version.schema_id == schema.id


class TestSeedIdempotent:
    async def test_running_twice_creates_no_duplicates(self, session: AsyncSession) -> None:
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        await _seed_all(session, org_id, account_id)
        await _seed_all(session, org_id, account_id)

        schemas = await _schemas(session, org_id)
        assert len(schemas) == len(SYSTEM_SCHEMAS)
        for schema in schemas:
            assert len(await _versions_for(session, schema.id)) == 1


class TestSeedPartialPreExisting:
    async def test_only_missing_schemas_are_created(self, session: AsyncSession) -> None:
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        session.add(
            Schema(
                organisation_id=org_id,
                name="schema_text",
                abstract_name="_system.schema_text",
                account_id=account_id,
                system=True,
            )
        )
        await session.flush()
        await _seed_all(session, org_id, account_id)

        schemas = {s.name: s for s in await _schemas(session, org_id)}
        assert set(schemas) == {spec["name"] for spec in SYSTEM_SCHEMAS}
        # the pre-existing schema is left untouched (no description back-filled)
        assert schemas["schema_text"].description is None
        for name in ("schema_freeform", "schema_trigger_payload"):
            assert schemas[name].description == f"System schema: {name}"
            assert schemas[name].system is True

    async def test_no_version_is_fabricated_for_pre_existing_schema(self, session: AsyncSession) -> None:
        org_id, account_id = uuid.uuid4(), uuid.uuid4()
        session.add(
            Schema(
                organisation_id=org_id,
                name="schema_text",
                abstract_name="_system.schema_text",
                account_id=account_id,
                system=True,
            )
        )
        await session.flush()
        await _seed_all(session, org_id, account_id)

        schemas = {s.name: s for s in await _schemas(session, org_id)}
        assert not await _versions_for(session, schemas["schema_text"].id)
        # the freshly created schemas still get their versions
        for name in ("schema_freeform", "schema_trigger_payload"):
            assert len(await _versions_for(session, schemas[name].id)) == 1


class TestSeedOrgScoping:
    async def test_same_name_in_other_org_does_not_block_creation(self, session: AsyncSession) -> None:
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        account_id = uuid.uuid4()
        await _seed_all(session, org_a, account_id)
        await _seed_all(session, org_b, account_id)

        assert {s.name for s in await _schemas(session, org_a)} == {spec["name"] for spec in SYSTEM_SCHEMAS}
        assert {s.name for s in await _schemas(session, org_b)} == {spec["name"] for spec in SYSTEM_SCHEMAS}

    async def test_partial_pre_existing_in_other_org_seeds_its_missing_schemas(self, session: AsyncSession) -> None:
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        account_id = uuid.uuid4()
        await _seed_all(session, org_a, account_id)

        # org_b already has one schema whose name collides with org_a's set —
        # seeding org_b must still create the other two for org_b alone.
        session.add(
            Schema(
                organisation_id=org_b,
                name="schema_freeform",
                abstract_name="_system.schema_freeform",
                account_id=account_id,
                system=True,
            )
        )
        await session.flush()
        await _seed_all(session, org_b, account_id)

        assert len(await _schemas(session, org_a)) == len(SYSTEM_SCHEMAS)
        assert {s.name for s in await _schemas(session, org_b)} == {spec["name"] for spec in SYSTEM_SCHEMAS}


class TestSystemSchemasContract:
    def test_spec_names_are_unique(self) -> None:
        names = [spec["name"] for spec in SYSTEM_SCHEMAS]
        assert len(names) == len(set(names))

    def test_specs_share_version_metadata(self) -> None:
        assert {spec["version"] for spec in SYSTEM_SCHEMAS} == {"v1"}
