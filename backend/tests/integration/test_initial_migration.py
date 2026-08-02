import uuid
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session

from modulo.db.models import Base, LibraryPrimitive, Organisation, Run
from tests.factories import (
    AccountFactory,
    OrganisationFactory,
    PipelineFactory,
    PipelineSnapshotFactory,
    RunFactory,
)


async def test_initial_migration_creates_domain_tables(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_table_names())

    assert {
        "agents",
        "hitl_claims",
        "org_api_keys",
        "organisations",
        "pipeline_edges",
        "pipeline_snapshots",
        "pipelines",
        "runs",
    } <= set(table_names)


async def test_migrated_schema_matches_orm_metadata(db_engine: AsyncEngine) -> None:
    """The migrated schema matches ORM metadata, modulo migration-managed drift.

    Migration-only PostgreSQL triggers are intentionally outside ORM metadata.
    The same applies to:
      - performance indexes created by migrations (e.g. ``ix_agent_account_id``)
        that the ORM models do not declare — extra DB indexes never break ORM
        reads/writes, so the parity check ignores ``remove_index``/``add_index``.
      - column/table comments declared on ORM models but not mirrored in every
        migration — cosmetic, ignored via ``modify_comment``/``add_table_comment``.

    Everything else (tables, columns, constraints, nullability, types, server
    defaults) must match exactly; a genuine drift item there fails the test.
    """
    async with db_engine.connect() as connection:
        differences = await connection.run_sync(
            lambda sync_connection: compare_metadata(MigrationContext.configure(sync_connection), Base.metadata),
        )

    def _is_benign_migration_managed(diff: tuple[Any, ...]) -> bool:
        # ``modify_comment``/``add_table_comment`` arrive as single-element
        # lists wrapping the ``(kind, ...)`` tuple; plain tuple diffs carry the
        # kind at index 0. Normalise both before matching.
        inner = diff[0] if isinstance(diff, list) and diff else diff
        kind = inner[0] if isinstance(inner, (tuple, list)) and inner else None
        return kind in ("remove_index", "add_index", "modify_comment", "add_table_comment")

    real_drift = [d for d in differences if not _is_benign_migration_managed(d)]
    assert real_drift == [], (
        f"Schema drift vs ORM metadata (excluding migration-managed indexes/comments): {real_drift}"
    )


async def test_persisted_factories_insert_valid_relationships(db_engine: AsyncEngine) -> None:
    def insert_graph(connection: Any) -> tuple[object, object]:
        with Session(bind=connection) as session:
            factories = (
                AccountFactory,
                OrganisationFactory,
                PipelineFactory,
                PipelineSnapshotFactory,
                RunFactory,
            )
            for factory_class in factories:
                factory_class._meta.sqlalchemy_session = session
            run = RunFactory()
            session.flush()
            persisted = session.scalar(select(Run).where(Run.id == run.id))
            assert persisted is not None
            result = persisted.organisation_id, persisted.pipeline_id
            session.rollback()
            return result

    async with db_engine.connect() as connection:
        organisation_id, pipeline_id = await connection.run_sync(insert_graph)

    assert organisation_id is not None
    assert pipeline_id is not None


async def test_library_fork_provenance_is_registry_only_and_immutable(
    db_engine: AsyncEngine,
) -> None:
    organisation_id = uuid.uuid4()
    registry_id = uuid.uuid4()
    local_id = uuid.uuid4()
    fork_id = uuid.uuid4()
    common = {"tags": [], "content_json": {}}

    async with db_engine.begin() as connection:
        await connection.execute(
            Organisation.__table__.insert().values(
                id=organisation_id,
                name="Fork test",
                slug=f"fork-test-{organisation_id}",
                settings_json={},
            ),
        )
        await connection.execute(
            LibraryPrimitive.__table__.insert().values(
                id=registry_id,
                organisation_id=organisation_id,
                source="registry",
                primitive_type="agent",
                name="Registry",
                slug="registry",
                author="publisher",
                version="1.0.0",
                visibility="org",
                source_url="https://registry.example/agent",
                checksum="a" * 64,
                ed25519_signature="signature",
                verified=True,
                download_count=0,
                average_rating=1,
                review_count=0,
                **common,
            ),
        )
        await connection.execute(
            LibraryPrimitive.__table__.insert().values(
                id=local_id,
                organisation_id=organisation_id,
                source="local",
                primitive_type="agent",
                name="Local",
                slug="local",
                author="user",
                version="1.0.0",
                visibility="org",
                **common,
            ),
        )

    with pytest.raises(DBAPIError):
        async with db_engine.begin() as connection:
            await connection.execute(
                LibraryPrimitive.__table__.insert().values(
                    id=uuid.uuid4(),
                    organisation_id=organisation_id,
                    source="local",
                    primitive_type="agent",
                    name="Invalid fork",
                    slug="invalid-fork",
                    author="user",
                    version="1.0.0",
                    visibility="org",
                    forked_from=local_id,
                    **common,
                ),
            )

    async with db_engine.begin() as connection:
        await connection.execute(
            LibraryPrimitive.__table__.insert().values(
                id=fork_id,
                organisation_id=organisation_id,
                source="local",
                primitive_type="agent",
                name="Valid fork",
                slug="valid-fork",
                author="user",
                version="1.0.0",
                visibility="org",
                forked_from=registry_id,
                **common,
            ),
        )

    with pytest.raises(DBAPIError):
        async with db_engine.begin() as connection:
            await connection.execute(
                update(LibraryPrimitive).where(LibraryPrimitive.id == fork_id).values(forked_from=None),
            )
