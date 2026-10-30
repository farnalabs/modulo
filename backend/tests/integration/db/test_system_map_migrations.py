"""Integration tests for System Maps migration (FAR-2236).

These tests verify the migration can be applied and rolled back,
and that the schema matches the expected structure.

These tests require a live database connection and are marked as integration tests.
They are skipped by default in pre-commit/CI unit test runs.
Run with: pytest -m integration
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

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


class TestSystemMapMigration:
    """Test the system maps migration."""

    @pytest.fixture
    def alembic_config(self):
        """Create Alembic config for testing."""
        return Config("backend/alembic.ini")

    @pytest.mark.asyncio
    async def test_migration_upgrade_creates_tables(self, alembic_config, db_session: AsyncSession):
        """Test that upgrading to 0340 creates all 8 tables."""
        # Get the script directory
        script_dir = ScriptDirectory.from_config(alembic_config)

        # Find our migration
        revisions = list(script_dir.walk_revisions())
        migration_rev = None
        for rev in revisions:
            if "0340_system_maps" in rev.revision:
                migration_rev = rev.revision
                break

        assert migration_rev is not None, "Migration 0340_system_maps not found"

        # Upgrade to the migration
        command.upgrade(alembic_config, migration_rev)

        # Verify tables exist
        inspector = inspect(db_session.bind)
        tables = inspector.get_table_names()

        expected_tables = [
            "system_maps",
            "system_map_revisions",
            "system_map_nodes",
            "system_map_relationships",
            "system_map_loop_configs",
            "system_map_attribution_bindings",
            "system_map_loop_admission_attempts",
            "system_map_loop_reservations",
        ]

        for table in expected_tables:
            assert table in tables, f"Table {table} was not created"

    @pytest.mark.asyncio
    async def test_migration_downgrade_drops_tables(self, alembic_config, db_session: AsyncSession):
        """Test that downgrading from 0340 drops all 8 tables."""
        # First upgrade
        script_dir = ScriptDirectory.from_config(alembic_config)
        migration_rev = None
        for rev in script_dir.walk_revisions():
            if "0340_system_maps" in rev.revision:
                migration_rev = rev.revision
                break

        command.upgrade(alembic_config, migration_rev)

        # Then downgrade
        command.downgrade(alembic_config, "-1")

        # Verify tables are dropped
        inspector = inspect(db_session.bind)
        tables = inspector.get_table_names()

        expected_tables = [
            "system_maps",
            "system_map_revisions",
            "system_map_nodes",
            "system_map_relationships",
            "system_map_loop_configs",
            "system_map_attribution_bindings",
            "system_map_loop_admission_attempts",
            "system_map_loop_reservations",
        ]

        for table in expected_tables:
            assert table not in tables, f"Table {table} was not dropped"

    @pytest.mark.asyncio
    async def test_migration_creates_indexes(self, alembic_config, db_session: AsyncSession):
        """Test that migration creates expected indexes."""
        script_dir = ScriptDirectory.from_config(alembic_config)
        migration_rev = None
        for rev in script_dir.walk_revisions():
            if "0340_system_maps" in rev.revision:
                migration_rev = rev.revision
                break

        command.upgrade(alembic_config, migration_rev)

        inspector = inspect(db_session.bind)

        # Check system_maps indexes
        indexes = inspector.get_indexes("system_maps")
        index_names = [idx["name"] for idx in indexes]
        expected_indexes = [
            "ix_system_maps_organisation_id",
            "ix_system_maps_owner_team_id",
            "ix_system_maps_published_revision_id",
            "uq_system_maps_org_default_live",
        ]
        for idx in expected_indexes:
            assert idx in index_names, f"Index {idx} not found on system_maps"

        # Check system_map_revisions indexes
        indexes = inspector.get_indexes("system_map_revisions")
        index_names = [idx["name"] for idx in indexes]
        expected_indexes = [
            "ix_system_map_revisions_organisation_id",
            "ix_system_map_revisions_system_map_id",
            "ix_system_map_revisions_status",
        ]
        for idx in expected_indexes:
            assert idx in index_names, f"Index {idx} not found on system_map_revisions"

    @pytest.mark.asyncio
    async def test_migration_creates_foreign_keys(self, alembic_config, db_session: AsyncSession):
        """Test that migration creates expected foreign keys."""
        script_dir = ScriptDirectory.from_config(alembic_config)
        migration_rev = None
        for rev in script_dir.walk_revisions():
            if "0340_system_maps" in rev.revision:
                migration_rev = rev.revision
                break

        command.upgrade(alembic_config, migration_rev)

        inspector = inspect(db_session.bind)

        # Check system_maps foreign keys
        fks = inspector.get_foreign_keys("system_maps")
        fk_tables = [fk["referred_table"] for fk in fks]
        assert "organisations" in fk_tables
        assert "teams" in fk_tables
        # published_revision_id references system_map_revisions (composite)
        # Note: composite FKs may not be detected the same way

        # Check system_map_revisions foreign keys
        fks = inspector.get_foreign_keys("system_map_revisions")
        fk_tables = [fk["referred_table"] for fk in fks]
        assert "organisations" in fk_tables
        assert "system_maps" in fk_tables

        # Check system_map_nodes foreign keys
        fks = inspector.get_foreign_keys("system_map_nodes")
        fk_tables = [fk["referred_table"] for fk in fks]
        assert "organisations" in fk_tables
        assert "system_map_revisions" in fk_tables
        assert "pipelines" in fk_tables
        assert "connector_instances" in fk_tables
        assert "teams" in fk_tables
        assert "evals" in fk_tables

    @pytest.mark.asyncio
    async def test_migration_creates_check_constraints(self, alembic_config, db_session: AsyncSession):
        """Test that migration creates expected CHECK constraints."""
        script_dir = ScriptDirectory.from_config(alembic_config)
        migration_rev = None
        for rev in script_dir.walk_revisions():
            if "0340_system_maps" in rev.revision:
                migration_rev = rev.revision
                break

        command.upgrade(alembic_config, migration_rev)

        inspector = inspect(db_session.bind)

        # Check system_maps check constraints
        constraints = inspector.get_check_constraints("system_maps")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_system_maps_visibility" in constraint_names
        assert "ck_system_maps_team_owner" in constraint_names

        # Check system_map_revisions
        constraints = inspector.get_check_constraints("system_map_revisions")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_system_map_revisions_status" in constraint_names

        # Check system_map_nodes
        constraints = inspector.get_check_constraints("system_map_nodes")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_system_map_nodes_type" in constraint_names

        # Check system_map_relationships
        constraints = inspector.get_check_constraints("system_map_relationships")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_system_map_relationships_type" in constraint_names

        # Check system_map_loop_configs
        constraints = inspector.get_check_constraints("system_map_loop_configs")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_loop_configs_obs_window" in constraint_names
        assert "ck_loop_configs_budget" in constraint_names
        assert "ck_loop_configs_cooldown" in constraint_names

        # Check system_map_attribution_bindings
        constraints = inspector.get_check_constraints("system_map_attribution_bindings")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_system_map_attribution_bindings_kind" in constraint_names

        # Check system_map_loop_admission_attempts
        constraints = inspector.get_check_constraints("system_map_loop_admission_attempts")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_loop_admission_attempts_status" in constraint_names

        # Check system_map_loop_reservations
        constraints = inspector.get_check_constraints("system_map_loop_reservations")
        constraint_names = [c["name"] for c in constraints]
        assert "ck_loop_reservations_status" in constraint_names

    @pytest.mark.asyncio
    async def test_migration_creates_unique_constraints(self, alembic_config, db_session: AsyncSession):
        """Test that migration creates expected unique constraints."""
        script_dir = ScriptDirectory.from_config(alembic_config)
        migration_rev = None
        for rev in script_dir.walk_revisions():
            if "0340_system_maps" in rev.revision:
                migration_rev = rev.revision
                break

        command.upgrade(alembic_config, migration_rev)

        inspector = inspect(db_session.bind)

        # Check system_map_revisions unique constraint
        unique_constraints = inspector.get_unique_constraints("system_map_revisions")
        uc_names = [uc["name"] for uc in unique_constraints]
        assert "uq_system_map_revisions_map_number" in uc_names

        # Check system_map_nodes unique constraint
        unique_constraints = inspector.get_unique_constraints("system_map_nodes")
        uc_names = [uc["name"] for uc in unique_constraints]
        assert "uq_system_map_nodes_revision_key" in uc_names

        # Check system_map_relationships unique constraint
        unique_constraints = inspector.get_unique_constraints("system_map_relationships")
        uc_names = [uc["name"] for uc in unique_constraints]
        assert "uq_system_map_relationships_rev_src_tgt_type" in uc_names

        # Check system_map_loop_configs unique constraint
        unique_constraints = inspector.get_unique_constraints("system_map_loop_configs")
        uc_names = [uc["name"] for uc in unique_constraints]
        assert "uq_system_map_loop_configs_revision_key" in uc_names

        # Check system_map_loop_admission_attempts unique constraint
        unique_constraints = inspector.get_unique_constraints("system_map_loop_admission_attempts")
        uc_names = [uc["name"] for uc in unique_constraints]
        assert "uq_loop_admission_attempts_config_key" in uc_names

        # Check system_map_loop_reservations unique constraint
        unique_constraints = inspector.get_unique_constraints("system_map_loop_reservations")
        uc_names = [uc["name"] for uc in unique_constraints]
        assert "uq_loop_reservations_config_key" in uc_names

    @pytest.mark.asyncio
    async def test_migration_creates_partial_unique_indexes_on_attribution_bindings(
        self, alembic_config, db_session: AsyncSession
    ):
        """Test that partial unique indexes are created on attribution_bindings."""
        script_dir = ScriptDirectory.from_config(alembic_config)
        migration_rev = None
        for rev in script_dir.walk_revisions():
            if "0340_system_maps" in rev.revision:
                migration_rev = rev.revision
                break

        command.upgrade(alembic_config, migration_rev)

        inspector = inspect(db_session.bind)

        # Check indexes on system_map_attribution_bindings
        indexes = inspector.get_indexes("system_map_attribution_bindings")
        index_names = [idx["name"] for idx in indexes]
        assert "uq_attr_binding_trigger_per_pipeline_node" in index_names
        assert "uq_attr_binding_action_per_pipeline_node" in index_names


class TestSystemMapModelSchema:
    """Test that ORM models match the migrated schema."""

    @pytest.mark.asyncio
    async def test_system_map_model_columns(self, db_session: AsyncSession):
        """Verify SystemMap model columns match expectations."""
        # This test requires the migration to be applied first
        # The fixture should handle this
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()

        map_obj = SystemMap(
            organisation_id=org_id,
            name="Test Map",
            account_id=account_id,
        )
        db_session.add(map_obj)
        await db_session.flush()

        # Verify we can read it back
        result = await db_session.execute(select(SystemMap).where(SystemMap.id == map_obj.id))
        loaded = result.scalar_one()
        assert loaded.name == "Test Map"
        assert loaded.organisation_id == org_id
        assert loaded.account_id == account_id

    @pytest.mark.asyncio
    async def test_system_map_revision_model_columns(self, db_session: AsyncSession):
        """Verify SystemMapRevision model columns match expectations."""
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        map_obj = SystemMap(organisation_id=org_id, name="Test Map", account_id=account_id)
        db_session.add(map_obj)
        await db_session.flush()

        revision = SystemMapRevision(
            organisation_id=org_id,
            system_map_id=map_obj.id,
            revision_number=1,
            status="draft",
            content_json={"test": "data"},
        )
        db_session.add(revision)
        await db_session.flush()

        result = await db_session.execute(select(SystemMapRevision).where(SystemMapRevision.id == revision.id))
        loaded = result.scalar_one()
        assert loaded.revision_number == 1
        assert loaded.status == "draft"
        assert loaded.content_json == {"test": "data"}

    @pytest.mark.asyncio
    async def test_system_map_node_model_columns(self, db_session: AsyncSession):
        """Verify SystemMapNode model columns match expectations."""
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        map_obj = SystemMap(organisation_id=org_id, name="Test Map", account_id=account_id)
        db_session.add(map_obj)
        await db_session.flush()

        revision = SystemMapRevision(
            organisation_id=org_id,
            system_map_id=map_obj.id,
            revision_number=1,
        )
        db_session.add(revision)
        await db_session.flush()

        node = SystemMapNode(
            organisation_id=org_id,
            revision_id=revision.id,
            node_key="node_1",
            node_type="pipeline",
            display_name="Pipeline Node",
        )
        db_session.add(node)
        await db_session.flush()

        result = await db_session.execute(select(SystemMapNode).where(SystemMapNode.id == node.id))
        loaded = result.scalar_one()
        assert loaded.node_key == "node_1"
        assert loaded.node_type == "pipeline"
        assert loaded.display_name == "Pipeline Node"

    @pytest.mark.asyncio
    async def test_system_map_relationship_model_columns(self, db_session: AsyncSession):
        """Verify SystemMapRelationship model columns match expectations."""
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        map_obj = SystemMap(organisation_id=org_id, name="Test Map", account_id=account_id)
        db_session.add(map_obj)
        await db_session.flush()

        revision = SystemMapRevision(
            organisation_id=org_id,
            system_map_id=map_obj.id,
            revision_number=1,
        )
        db_session.add(revision)
        await db_session.flush()

        rel = SystemMapRelationship(
            organisation_id=org_id,
            revision_id=revision.id,
            source_node_key="a",
            target_node_key="b",
            relationship_type="flow",
        )
        db_session.add(rel)
        await db_session.flush()

        result = await db_session.execute(select(SystemMapRelationship).where(SystemMapRelationship.id == rel.id))
        loaded = result.scalar_one()
        assert loaded.source_node_key == "a"
        assert loaded.target_node_key == "b"
        assert loaded.relationship_type == "flow"

    @pytest.mark.asyncio
    async def test_system_map_loop_config_model_columns(self, db_session: AsyncSession):
        """Verify SystemMapLoopConfig model columns match expectations."""
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        map_obj = SystemMap(organisation_id=org_id, name="Test Map", account_id=account_id)
        db_session.add(map_obj)
        await db_session.flush()

        revision = SystemMapRevision(
            organisation_id=org_id,
            system_map_id=map_obj.id,
            revision_number=1,
        )
        db_session.add(revision)
        await db_session.flush()

        # Need a pipeline for the FK
        # For this test, we'll skip the FK check and just verify the model works
        # The FK is validated in the migration test above
        loop_config = SystemMapLoopConfig(
            organisation_id=org_id,
            revision_id=revision.id,
            node_key="loop_1",
            target_signal="error_rate",
            observation_window_seconds=3600,
            action_pipeline_id=uuid.uuid4(),  # This would need a real pipeline in integration test
            budget_limit_usd=Decimal("100.00"),
            cooldown_seconds=1800,
            outcome_metric="error_rate",
        )
        db_session.add(loop_config)
        await db_session.flush()

        result = await db_session.execute(select(SystemMapLoopConfig).where(SystemMapLoopConfig.id == loop_config.id))
        loaded = result.scalar_one()
        assert loaded.node_key == "loop_1"
        assert loaded.target_signal == "error_rate"
        assert loaded.observation_window_seconds == 3600
        assert loaded.budget_limit_usd == Decimal("100.00")
        assert loaded.cooldown_seconds == 1800
        assert loaded.outcome_metric == "error_rate"

    @pytest.mark.asyncio
    async def test_system_map_attribution_binding_model_columns(self, db_session: AsyncSession):
        """Verify SystemMapAttributionBinding model columns match expectations."""
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        map_obj = SystemMap(organisation_id=org_id, name="Test Map", account_id=account_id)
        db_session.add(map_obj)
        await db_session.flush()

        binding = SystemMapAttributionBinding(
            organisation_id=org_id,
            pipeline_id=uuid.uuid4(),
            system_map_id=map_obj.id,
            node_key="node_1",
            binding_kind="trigger",
        )
        db_session.add(binding)
        await db_session.flush()

        result = await db_session.execute(
            select(SystemMapAttributionBinding).where(SystemMapAttributionBinding.id == binding.id)
        )
        loaded = result.scalar_one()
        assert loaded.node_key == "node_1"
        assert loaded.binding_kind == "trigger"
        assert loaded.system_map_id == map_obj.id

    @pytest.mark.asyncio
    async def test_system_map_loop_admission_attempt_model_columns(self, db_session: AsyncSession):
        """Verify SystemMapLoopAdmissionAttempt model columns match expectations."""
        org_id = uuid.uuid4()
        attempt = SystemMapLoopAdmissionAttempt(
            organisation_id=org_id,
            loop_config_id=uuid.uuid4(),
            idempotency_key="idem_123",
            status="pending",
        )
        db_session.add(attempt)
        await db_session.flush()

        result = await db_session.execute(
            select(SystemMapLoopAdmissionAttempt).where(SystemMapLoopAdmissionAttempt.id == attempt.id)
        )
        loaded = result.scalar_one()
        assert loaded.idempotency_key == "idem_123"
        assert loaded.status == "pending"

    @pytest.mark.asyncio
    async def test_system_map_loop_reservation_model_columns(self, db_session: AsyncSession):
        """Verify SystemMapLoopReservation model columns match expectations."""
        org_id = uuid.uuid4()

        reservation = SystemMapLoopReservation(
            organisation_id=org_id,
            loop_config_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            idempotency_key="res_456",
            cooldown_until=datetime.now(UTC) + timedelta(hours=1),
            status="active",
        )
        db_session.add(reservation)
        await db_session.flush()

        result = await db_session.execute(
            select(SystemMapLoopReservation).where(SystemMapLoopReservation.id == reservation.id)
        )
        loaded = result.scalar_one()
        assert loaded.idempotency_key == "res_456"
        assert loaded.status == "active"
        assert loaded.run_id == reservation.run_id
