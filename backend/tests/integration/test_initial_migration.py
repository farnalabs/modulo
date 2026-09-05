import uuid
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import JSON, CheckConstraint, inspect, select, update
from sqlalchemy.dialects.postgresql import JSONB
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

pytestmark = pytest.mark.integration


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
      - the LangGraph checkpoint tables (``checkpoints``, ``checkpoint_blobs``,
        ``checkpoint_writes``, ``checkpoint_migrations``) — created and managed
        entirely by ``ModuloPostgresSaver.setup()`` at runtime (raw SQL in
        ``_MIGRATION_SQL``), deliberately NOT ORM models and NOT Alembic
        migrations. ``compare_metadata`` always reports them as
        ``remove_table``; ignored here.
      - ``hitl_claims.decision_payload`` declared as generic ``JSON`` in the
        ORM for SQLite/MariaDB parity while migration 0075 creates it as
        ``JSONB`` on Postgres — a documented, deliberate divergence, ignored.
      - ``runs.raw_output_markers`` / ``run_classification`` /
        ``work_item_refs`` — the same multi-backend JSON-parity pattern
        (migrations create JSONB, the ORM maps generic JSON), ignored.
      - ``run_number_counters`` — the ORM model was deleted (FAR-253 dead-code
        cleanup) but the reconciliation chain still creates the table for the
        raw-SQL counter path; the table lives outside ORM metadata, ignored.
      - ``run_node_outputs_quarantine`` — the ops/remediation side table
        (FAR-583, migration 0176) deliberately has no ORM model (ops SQL only,
        no app-role grant), ignored like ``run_number_counters``.
      - ``modulo_journey_facts.updated_at`` — the ORM's ``TimestampMixin``
        declares ``updated_at`` but no migration ever created the column; the
        fact table's write instant is ``created_at`` only, ignored.
      - every ``modify_type`` where the DB is ``JSONB`` and the ORM maps
        generic ``JSON`` — the repo-wide multi-backend JSON-parity convention
        (migrations create JSONB on Postgres; the ORM maps generic JSON for
        SQLite/MariaDB parity). Documented for the sentinel columns above and
        applied uniformly since ORM JSON columns are added continuously.
      - audit-chain columns (``created_by``/``updated_by``/``deleted_by``/
        ``deleted_at``) and their ``fk_*`` FKs to ``accounts.id`` present in
        the DB but absent from ORM metadata — the DB triggers populate them
        (0108 audit chain); the ORM never reads/writes them directly.
      - ``CheckConstraint`` divergence both directions — the ORM declares the
        portable subset of each guard (repo parity rule: dialect-specific SQL
        like ``jsonb_typeof`` never belongs in the ORM; see the
        ``run_node_outputs`` model) while migrations own the strict DB-side
        CHECKs. CHECK SQL text parity is intentionally not enforced here.
      - ``add_fk`` to ``nodes.id`` (graph-consistency FKs the ORM declares on
        ``eval_definitions``/``eval_results``/``node_observations``/
        ``run_evidence``/``pipeline_edges``/``snapshot_schema_pins``) that no
        migration has created yet — pre-existing gap, tracked separately.

    Everything else (tables, columns, nullability, non-JSON types, server
    defaults, non-audit FKs) must match exactly; a genuine drift item there
    fails the test.
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
        if kind in ("remove_index", "add_index", "modify_comment", "add_table_comment"):
            return True
        if kind == "remove_table":
            # Runtime-managed LangGraph checkpoint tables (ModuloPostgresSaver
            # setup) — outside ORM metadata by design. Also the reconciliation
            # chain's run_number_counters, whose ORM model was deleted (FAR-253)
            # while the raw-SQL counter path keeps the table, and the FAR-583
            # ops quarantine table (no ORM model by design).
            return inner[1].name in (
                "checkpoints",
                "checkpoint_blobs",
                "checkpoint_writes",
                "checkpoint_migrations",
                "run_number_counters",
                "run_node_outputs_quarantine",
            )
        if kind == "add_column":
            # ORM TimestampMixin declares updated_at on the fact table but no
            # migration ever created it; created_at is the write instant.
            return inner[2] == "modulo_journey_facts" and inner[3].name == "updated_at"
        if kind == "remove_column":
            # DB triggers own the audit-chain columns (0108); the ORM never
            # reads or writes them directly.
            return inner[3].name in ("created_by", "updated_by", "deleted_by", "deleted_at")
        if kind in ("remove_fk", "add_fk"):
            # remove_fk: the audit-chain FKs on the trigger-maintained columns.
            # add_fk: graph-consistency FKs to nodes.id the ORM declares but no
            # migration has created yet. The diff's FK constraints may not
            # resolve their referred table (comparison metadata), so match on
            # the source column names.
            col_names = {col.name for col in inner[1].columns}
            if kind == "remove_fk":
                return bool(col_names & {"created_by", "updated_by", "deleted_by"})
            return bool(col_names & {"node_id", "source_node_id", "target_node_id"})
        if kind in ("remove_constraint", "add_constraint"):
            # CHECK guards: the ORM declares portable subsets; migrations own
            # the strict dialect-specific versions (repo parity rule).
            return isinstance(inner[1], CheckConstraint)
        if kind == "modify_type":
            # Repo-wide multi-backend JSON parity: migrations create JSONB on
            # Postgres, the ORM maps generic JSON. The four sentinel columns
            # below are the historically-listed instances; the rule applies
            # uniformly (JSONB in DB -> generic JSON in metadata).
            if isinstance(inner[5], JSONB) and isinstance(inner[6], JSON):
                return True
            return (inner[2], inner[3]) in {
                ("hitl_claims", "decision_payload"),
                ("runs", "raw_output_markers"),
                ("runs", "run_classification"),
                ("runs", "work_item_refs"),
            }
        return False

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
