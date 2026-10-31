"""Integration tests for the System Maps migration + ORM schema parity (FAR-2236).

Two layers of proof:

    * a real Alembic round-trip against a PRIVATE Postgres database (upgrade from
      ``0340_drop_assistant_ui_driving_flag_row`` to ``0344_system_maps_tenant_integrity``

  then ``downgrade -3``), so the migration's DDL is exercised exactly as shipped;
* schema assertions against the session's fully-migrated database, including
  model↔migrated-schema column parity for all eight tables — the check that
  catches a migration drifting behind the ORM.

These tests require a live database and are marked as integration tests. Run
with: ``pytest -m integration``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.db.models.system_map import (
    SystemMap,
    SystemMapAttributionBinding,
    SystemMapLoopAdmissionAttempt,
    SystemMapLoopConfig,
    SystemMapLoopReservation,
    SystemMapNode,
    SystemMapRelationship,
    SystemMapRevision,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

MIGRATION_REV = "0344_system_maps_tenant_integrity"
PREV_REV = "0340_drop_assistant_ui_driving_flag_row"

_SYSTEM_MAP_MODELS = (
    SystemMap,
    SystemMapRevision,
    SystemMapNode,
    SystemMapRelationship,
    SystemMapLoopConfig,
    SystemMapAttributionBinding,
    SystemMapLoopAdmissionAttempt,
    SystemMapLoopReservation,
)

_EXPECTED_TABLES = tuple(model.__tablename__ for model in _SYSTEM_MAP_MODELS)


def _column_names(conn: Connection, table: str) -> set[str]:
    return {name for col in inspect(conn).get_columns(table) if (name := col["name"]) is not None}


def _index_names(conn: Connection, table: str) -> set[str]:
    return {name for idx in inspect(conn).get_indexes(table) if (name := idx["name"]) is not None}


def _check_names(conn: Connection, table: str) -> set[str]:
    return {name for ck in inspect(conn).get_check_constraints(table) if (name := ck["name"]) is not None}


def _unique_names(conn: Connection, table: str) -> set[str]:
    return {name for uc in inspect(conn).get_unique_constraints(table) if (name := uc["name"]) is not None}


def _referred_tables(conn: Connection, table: str) -> set[str]:
    return {name for fk in inspect(conn).get_foreign_keys(table) if (name := fk["referred_table"]) is not None}


def _foreign_keys(conn: Connection, table: str) -> list[dict[str, Any]]:
    return inspect(conn).get_foreign_keys(table)


def _alembic_config(db_url: str) -> Config:
    """House conftest pattern: backend-absolute ini + script_location."""
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


def _run_alembic(iso_url: str, cmd: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        getattr(command, cmd)(_alembic_config(iso_url), target)


@pytest_asyncio.fixture
async def private_db_url(db_url: str) -> AsyncGenerator[str, None]:
    db_name, iso_url = await _fresh_db(db_url, "m0341")
    try:
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))


@pytest.mark.asyncio
async def test_migration_round_trips_against_private_database(private_db_url: str) -> None:
    """Upgrade 0342 creates all eight tables and integrity constraints; downgrade removes them again."""
    engine = create_async_engine(private_db_url, poolclass=NullPool)
    try:
        _run_alembic(private_db_url, "upgrade", PREV_REV)
        assert not set(_EXPECTED_TABLES) & await _table_names(engine)

        _run_alembic(private_db_url, "upgrade", MIGRATION_REV)
        tables = await _table_names(engine)
        for table in _EXPECTED_TABLES:
            assert table in tables, f"Table {table} was not created"

        _run_alembic(private_db_url, "downgrade", "-4")
        tables = await _table_names(engine)
        for table in _EXPECTED_TABLES:
            assert table not in tables, f"Table {table} was not dropped"
    finally:
        await engine.dispose()


class TestSystemMapMigratedSchema:
    """Schema assertions against the session's fully-migrated database."""

    @pytest.mark.asyncio
    async def test_migration_creates_tables(self, db_engine: AsyncEngine) -> None:
        tables = await _table_names(db_engine)
        for table in _EXPECTED_TABLES:
            assert table in tables, f"Table {table} was not created"

    @pytest.mark.asyncio
    async def test_migration_creates_indexes(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            maps_indexes = await conn.run_sync(lambda c: {idx["name"] for idx in inspect(c).get_indexes("system_maps")})
            revisions_indexes = await conn.run_sync(
                lambda c: {idx["name"] for idx in inspect(c).get_indexes("system_map_revisions")}
            )

        for name in (
            "ix_system_maps_organisation_id",
            "ix_system_maps_owner_team_id",
            "ix_system_maps_published_revision_id",
            "ix_system_maps_account_id",
            "ix_system_maps_deleted_by",
            "uq_system_maps_org_default_live",
        ):
            assert name in maps_indexes, f"Index {name} not found on system_maps"
        for name in (
            "ix_system_map_revisions_organisation_id",
            "ix_system_map_revisions_system_map_id",
            "ix_system_map_revisions_status",
            "ix_system_map_revisions_deleted_by",
            "uq_system_map_revisions_one_published",
        ):
            assert name in revisions_indexes, f"Index {name} not found on system_map_revisions"

    @pytest.mark.asyncio
    async def test_migration_creates_foreign_keys(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            maps_fks = await conn.run_sync(_referred_tables, "system_maps")
            revisions_fks = await conn.run_sync(_referred_tables, "system_map_revisions")
            revisions = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("system_map_revisions"))
            nodes_fks = await conn.run_sync(_referred_tables, "system_map_nodes")
            reservations = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("system_map_loop_reservations"))

        assert {"organisations", "teams", "accounts", "system_map_revisions"} <= maps_fks
        assert {"organisations", "system_maps"} <= revisions_fks
        reservation_run_fks = [fk for fk in reservations if fk["name"] == "system_map_loop_reservations_run_id_fkey"]
        assert len(reservation_run_fks) == 1
        assert reservation_run_fks[0]["constrained_columns"] == ["run_id"]
        assert reservation_run_fks[0]["referred_table"] == "runs"
        assert reservation_run_fks[0]["referred_columns"] == ["id"]
        revision_map_fks = [fk for fk in revisions if fk["name"] == "fk_system_map_revisions_map_org"]
        assert len(revision_map_fks) == 1
        assert revision_map_fks[0]["constrained_columns"] == ["system_map_id", "organisation_id"]
        assert revision_map_fks[0]["referred_columns"] == ["id", "organisation_id"]
        assert {
            "organisations",
            "system_map_revisions",
            "pipelines",
            "connector_instances",
            "teams",
            "evals",
        } <= nodes_fks

    @pytest.mark.asyncio
    async def test_owner_team_trigger_enforces_same_live_organisation(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            triggers = await conn.execute(
                text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'system_maps'::regclass AND NOT tgisinternal")
            )
            trigger_names = {row[0] for row in triggers}
        assert "trg_system_maps_owner_team_tenant" in trigger_names

    @pytest.mark.asyncio
    async def test_reservation_run_trigger_enforces_same_organisation(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            triggers = await conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'system_map_loop_reservations'::regclass AND NOT tgisinternal"
                )
            )
            trigger_names = {row[0] for row in triggers}
        assert "trg_system_map_loop_reservations_run_id_tenant" in trigger_names

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("table", "constraint_name"),
        [
            ("system_map_revisions", "fk_system_map_revisions_map_org"),
            ("system_map_nodes", "fk_system_map_nodes_revision"),
            ("system_map_relationships", "fk_system_map_relationships_revision"),
            ("system_map_loop_configs", "fk_system_map_loop_configs_revision"),
            ("system_map_attribution_bindings", "fk_system_map_attribution_bindings_map"),
            ("system_map_loop_admission_attempts", "fk_system_map_loop_admission_attempts_config"),
            ("system_map_loop_reservations", "fk_system_map_loop_reservations_config"),
        ],
    )
    async def test_migrated_composite_fk_matches_model(
        self, db_engine: AsyncEngine, table: str, constraint_name: str
    ) -> None:
        model = next(model for model in _SYSTEM_MAP_MODELS if model.__tablename__ == table)
        model_constraint = next(item for item in model.__table__.constraints if item.name == constraint_name)
        expected_constrained = list(model_constraint.columns.keys())
        expected_referred = [element.target_fullname.rsplit(".", 1)[-1] for element in model_constraint.elements]
        async with db_engine.connect() as conn:
            fks = await conn.run_sync(_foreign_keys, table)
        actual = next(fk for fk in fks if fk["name"] == constraint_name)
        assert actual["constrained_columns"] == expected_constrained
        assert actual["referred_columns"] == expected_referred

    @pytest.mark.asyncio
    async def test_migration_creates_check_constraints(self, db_engine: AsyncEngine) -> None:
        expected = {
            "system_maps": {"ck_system_maps_visibility", "ck_system_maps_team_owner"},
            "system_map_revisions": {"ck_system_map_revisions_status"},
            "system_map_nodes": {"ck_system_map_nodes_type"},
            "system_map_relationships": {"ck_system_map_relationships_type"},
            "system_map_loop_configs": {
                "ck_loop_configs_obs_window",
                "ck_loop_configs_budget",
                "ck_loop_configs_cooldown",
            },
            "system_map_attribution_bindings": {"ck_system_map_attribution_bindings_kind"},
            "system_map_loop_admission_attempts": {"ck_loop_admission_attempts_status"},
            "system_map_loop_reservations": {"ck_loop_reservations_status"},
        }
        async with db_engine.connect() as conn:
            for table, names in expected.items():
                actual = await conn.run_sync(_check_names, table)
                assert names <= actual, f"CHECK constraints missing on {table}: {names - actual}"

    @pytest.mark.asyncio
    async def test_migration_creates_unique_constraints(self, db_engine: AsyncEngine) -> None:
        expected = {
            "system_map_revisions": {"uq_system_map_revisions_map_number"},
            "system_map_nodes": {"uq_system_map_nodes_revision_key"},
            "system_map_relationships": {"uq_system_map_relationships_rev_src_tgt_type"},
            "system_map_loop_configs": {"uq_system_map_loop_configs_revision_key"},
            "system_map_loop_admission_attempts": {"uq_loop_admission_attempts_config_key"},
            "system_map_loop_reservations": {"uq_loop_reservations_config_key"},
        }
        async with db_engine.connect() as conn:
            for table, names in expected.items():
                actual = await conn.run_sync(_unique_names, table)
                assert names <= actual, f"Unique constraints missing on {table}: {names - actual}"

    @pytest.mark.asyncio
    async def test_migration_creates_partial_unique_indexes_on_attribution_bindings(
        self, db_engine: AsyncEngine
    ) -> None:
        async with db_engine.connect() as conn:
            names = await conn.run_sync(_index_names, "system_map_attribution_bindings")
        assert "uq_attr_binding_trigger_per_pipeline_node" in names
        assert "uq_attr_binding_action_per_pipeline_node" in names

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", _SYSTEM_MAP_MODELS, ids=[m.__tablename__ for m in _SYSTEM_MAP_MODELS])
    async def test_model_columns_match_migrated_schema(self, db_engine: AsyncEngine, model: Any) -> None:
        """Every ORM column exists in the migrated table (and nothing extra)."""
        table = str(model.__tablename__)
        async with db_engine.connect() as conn:
            actual = await conn.run_sync(_column_names, table)
        expected = {col.name for col in model.__table__.columns}
        assert actual == expected, (
            f"{table} model/migration drift: "
            f"only-in-migration={sorted(actual - expected)}, only-in-model={sorted(expected - actual)}"
        )


@dataclass(frozen=True)
class _GraphParents:
    org_id: uuid.UUID
    account_id: uuid.UUID
    pipeline_id: uuid.UUID
    map: SystemMap
    revision: SystemMapRevision
    loop_config: SystemMapLoopConfig


class TestSystemMapModelRoundTrip:
    """Write/read every model against the real migrated schema with real FKs."""

    @pytest_asyncio.fixture
    async def graph_parents(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
    ) -> _GraphParents:
        map_obj = SystemMap(
            organisation_id=test_org,
            name="Round Trip Map",
            account_id=test_user,
        )
        db_session.add(map_obj)
        await db_session.flush()

        revision = SystemMapRevision(
            organisation_id=test_org,
            system_map_id=map_obj.id,
            revision_number=1,
            status="draft",
            content_json={"test": "data"},
        )
        db_session.add(revision)
        await db_session.flush()

        loop_config = SystemMapLoopConfig(
            organisation_id=test_org,
            revision_id=revision.id,
            node_key="loop_1",
            target_signal="error_rate",
            observation_window_seconds=3600,
            action_pipeline_id=test_pipeline,
            budget_limit_usd=Decimal("100.00"),
            cooldown_seconds=1800,
            outcome_metric="error_rate",
        )
        db_session.add(loop_config)
        await db_session.flush()

        return _GraphParents(
            org_id=test_org,
            account_id=test_user,
            pipeline_id=test_pipeline,
            map=map_obj,
            revision=revision,
            loop_config=loop_config,
        )

    @pytest.mark.asyncio
    async def test_published_revision_index_rejects_duplicate_published_rows(
        self, db_session: AsyncSession, graph_parents: _GraphParents
    ) -> None:
        first = graph_parents.revision
        first.status = "published"
        second = SystemMapRevision(
            organisation_id=graph_parents.org_id,
            system_map_id=graph_parents.map.id,
            revision_number=2,
            status="published",
            content_json={"test": "second"},
        )
        db_session.add(second)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_system_map_and_revision_round_trip(
        self, db_session: AsyncSession, graph_parents: _GraphParents
    ) -> None:
        map_obj = graph_parents.map
        revision = graph_parents.revision

        loaded_map = (await db_session.execute(select(SystemMap).where(SystemMap.id == map_obj.id))).scalar_one()
        assert loaded_map.name == "Round Trip Map"
        assert loaded_map.visibility == "org"
        assert loaded_map.account_id == graph_parents.account_id
        assert loaded_map.deleted_by is None

        loaded_revision = (
            await db_session.execute(select(SystemMapRevision).where(SystemMapRevision.id == revision.id))
        ).scalar_one()
        assert loaded_revision.revision_number == 1
        assert loaded_revision.content_json == {"test": "data"}
        assert loaded_revision.deleted_by is None

    @pytest.mark.asyncio
    async def test_node_relationship_and_binding_round_trip(
        self, db_session: AsyncSession, graph_parents: _GraphParents
    ) -> None:
        org_id = graph_parents.org_id
        revision = graph_parents.revision
        map_obj = graph_parents.map
        pipeline_id = graph_parents.pipeline_id

        node = SystemMapNode(
            organisation_id=org_id,
            revision_id=revision.id,
            node_key="node_1",
            node_type="pipeline",
            display_name="Pipeline Node",
            pipeline_id=pipeline_id,
            position_x=Decimal("1.50"),
            position_y=Decimal("2.25"),
        )
        relationship = SystemMapRelationship(
            organisation_id=org_id,
            revision_id=revision.id,
            source_node_key="a",
            target_node_key="b",
            relationship_type="flow",
        )
        binding = SystemMapAttributionBinding(
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            system_map_id=map_obj.id,
            node_key="node_1",
            binding_kind="action_pipeline",
        )
        db_session.add_all([node, relationship, binding])
        await db_session.flush()

        loaded_node = (await db_session.execute(select(SystemMapNode).where(SystemMapNode.id == node.id))).scalar_one()
        assert loaded_node.position_x == Decimal("1.50")
        assert loaded_node.deleted_at is None
        assert loaded_node.deleted_by is None

        loaded_rel = (
            await db_session.execute(select(SystemMapRelationship).where(SystemMapRelationship.id == relationship.id))
        ).scalar_one()
        assert loaded_rel.relationship_type == "flow"
        assert loaded_rel.deleted_by is None

        loaded_binding = (
            await db_session.execute(
                select(SystemMapAttributionBinding).where(SystemMapAttributionBinding.id == binding.id)
            )
        ).scalar_one()
        assert loaded_binding.system_map_id == map_obj.id
        assert loaded_binding.deleted_by is None

    @pytest.mark.asyncio
    async def test_loop_config_attempt_and_reservation_round_trip(
        self, db_session: AsyncSession, graph_parents: _GraphParents, test_snapshot: uuid.UUID
    ) -> None:
        org_id = graph_parents.org_id
        loop_config = graph_parents.loop_config

        loaded_config = (
            await db_session.execute(select(SystemMapLoopConfig).where(SystemMapLoopConfig.id == loop_config.id))
        ).scalar_one()
        assert loaded_config.budget_limit_usd == Decimal("100.00")
        assert loaded_config.deleted_by is None

        attempt = SystemMapLoopAdmissionAttempt(
            organisation_id=org_id,
            loop_config_id=loop_config.id,
            idempotency_key="idem_123",
            status="pending",
        )
        run_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO runs "
                "(id, organisation_id, pipeline_id, snapshot_id, trigger_type, run_number, input_hash, "
                "langgraph_thread_id) "
                "VALUES (:id, :organisation_id, :pipeline_id, :snapshot_id, 'manual', :run_number, "
                ":input_hash, :thread_id)"
            ),
            {
                "id": run_id,
                "organisation_id": org_id,
                "pipeline_id": graph_parents.pipeline_id,
                "snapshot_id": test_snapshot,
                "run_number": 999999,
                "input_hash": "0" * 64,
                "thread_id": str(uuid.uuid4()),
            },
        )
        reservation = SystemMapLoopReservation(
            organisation_id=org_id,
            loop_config_id=loop_config.id,
            run_id=run_id,
            idempotency_key="res_456",
            cooldown_until=datetime.now(UTC) + timedelta(hours=1),
            status="active",
        )
        db_session.add_all([attempt, reservation])
        await db_session.flush()

        loaded_attempt = (
            await db_session.execute(
                select(SystemMapLoopAdmissionAttempt).where(SystemMapLoopAdmissionAttempt.id == attempt.id)
            )
        ).scalar_one()
        assert loaded_attempt.status == "pending"
        assert loaded_attempt.deleted_by is None

        loaded_reservation = (
            await db_session.execute(
                select(SystemMapLoopReservation).where(SystemMapLoopReservation.id == reservation.id)
            )
        ).scalar_one()
        assert loaded_reservation.run_id == reservation.run_id
        assert loaded_reservation.status == "active"
        assert loaded_reservation.deleted_by is None

    @pytest.mark.asyncio
    async def test_reservation_rejects_foreign_organisation_run_on_insert(
        self,
        db_session: AsyncSession,
        graph_parents: _GraphParents,
        test_org: uuid.UUID,
    ) -> None:
        foreign_org = uuid.uuid4()
        foreign_account = uuid.uuid4()
        foreign_pipeline = uuid.uuid4()
        foreign_snapshot = uuid.uuid4()
        foreign_run = uuid.uuid4()
        await db_session.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": foreign_org, "name": "Foreign System Map Org", "slug": f"foreign-{foreign_org.hex[:8]}"},
        )
        await db_session.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": foreign_account,
                "email": f"foreign-{foreign_account.hex[:8]}@example.com",
                "name": "Foreign System Map User",
            },
        )
        await db_session.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) VALUES "
                "(:id, :oid, :name, :account_id, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {
                "id": foreign_pipeline,
                "oid": foreign_org,
                "name": "Foreign System Map Pipeline",
                "account_id": foreign_account,
            },
        )
        await db_session.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, run_context_defaults, config_json) VALUES "
                "(:id, :pipeline_id, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": foreign_snapshot, "pipeline_id": foreign_pipeline, "oid": foreign_org},
        )
        await db_session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                "run_number, input_hash, langgraph_thread_id) VALUES "
                "(:id, :oid, :pipeline_id, :snapshot_id, 'manual', 999998, :input_hash, :thread_id)"
            ),
            {
                "id": foreign_run,
                "oid": foreign_org,
                "pipeline_id": foreign_pipeline,
                "snapshot_id": foreign_snapshot,
                "input_hash": "1" * 64,
                "thread_id": str(uuid.uuid4()),
            },
        )

        reservation = SystemMapLoopReservation(
            organisation_id=test_org,
            loop_config_id=graph_parents.loop_config.id,
            run_id=foreign_run,
            idempotency_key=f"foreign-insert-{uuid.uuid4().hex}",
            cooldown_until=datetime.now(UTC) + timedelta(hours=1),
            status="active",
        )
        db_session.add(reservation)
        with pytest.raises(IntegrityError, match="system map reservation run"):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_reservation_rejects_foreign_organisation_run_on_update(
        self,
        db_session: AsyncSession,
        graph_parents: _GraphParents,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
    ) -> None:
        foreign_org = uuid.uuid4()
        foreign_account = uuid.uuid4()
        foreign_pipeline = uuid.uuid4()
        foreign_snapshot = uuid.uuid4()
        foreign_run = uuid.uuid4()
        await db_session.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": foreign_org, "name": "Foreign Update Org", "slug": f"foreign-update-{foreign_org.hex[:8]}"},
        )
        await db_session.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": foreign_account,
                "email": f"foreign-update-{foreign_account.hex[:8]}@example.com",
                "name": "Foreign Update User",
            },
        )
        await db_session.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) VALUES "
                "(:id, :oid, :name, :account_id, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {
                "id": foreign_pipeline,
                "oid": foreign_org,
                "name": "Foreign Update Pipeline",
                "account_id": foreign_account,
            },
        )
        await db_session.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
                "graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                "model_backend_pins_json, run_context_defaults, config_json) VALUES "
                "(:id, :pipeline_id, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": foreign_snapshot, "pipeline_id": foreign_pipeline, "oid": foreign_org},
        )
        await db_session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                "run_number, input_hash, langgraph_thread_id) VALUES "
                "(:id, :oid, :pipeline_id, :snapshot_id, 'manual', 999997, :input_hash, :thread_id)"
            ),
            {
                "id": foreign_run,
                "oid": foreign_org,
                "pipeline_id": foreign_pipeline,
                "snapshot_id": foreign_snapshot,
                "input_hash": "2" * 64,
                "thread_id": str(uuid.uuid4()),
            },
        )
        own_run = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                "run_number, input_hash, langgraph_thread_id) VALUES "
                "(:id, :oid, :pipeline_id, :snapshot_id, 'manual', 999996, :input_hash, :thread_id)"
            ),
            {
                "id": own_run,
                "oid": test_org,
                "pipeline_id": test_pipeline,
                "snapshot_id": test_snapshot,
                "input_hash": "3" * 64,
                "thread_id": str(uuid.uuid4()),
            },
        )
        reservation = SystemMapLoopReservation(
            organisation_id=test_org,
            loop_config_id=graph_parents.loop_config.id,
            run_id=own_run,
            idempotency_key=f"foreign-update-{uuid.uuid4().hex}",
            cooldown_until=datetime.now(UTC) + timedelta(hours=1),
            status="active",
        )
        db_session.add(reservation)
        await db_session.flush()
        reservation.run_id = foreign_run
        with pytest.raises(IntegrityError, match="system map reservation run"):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_reservation_rejects_missing_run_reference(
        self,
        db_session: AsyncSession,
        graph_parents: _GraphParents,
        test_org: uuid.UUID,
    ) -> None:
        reservation = SystemMapLoopReservation(
            organisation_id=test_org,
            loop_config_id=graph_parents.loop_config.id,
            run_id=uuid.uuid4(),
            idempotency_key=f"missing-run-{uuid.uuid4().hex}",
            cooldown_until=datetime.now(UTC) + timedelta(hours=1),
            status="active",
        )
        db_session.add(reservation)
        with pytest.raises(IntegrityError):
            await db_session.flush()
