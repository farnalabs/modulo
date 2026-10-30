"""Unit tests for System Maps models (FAR-2236)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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


class TestSystemMapModels:
    """Test System Maps model construction and basic constraints."""

    def test_system_map_creation(self):
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        map_obj = SystemMap(
            organisation_id=org_id,
            name="Test Map",
            description="A test system map",
            account_id=account_id,
            default_map=False,
            visibility="org",
        )
        assert map_obj.organisation_id == org_id
        assert map_obj.name == "Test Map"
        assert map_obj.description == "A test system map"
        assert map_obj.default_map is False
        assert map_obj.visibility == "org"
        assert map_obj.account_id == account_id
        assert map_obj.deleted_by is None

    def test_system_map_revision_creation(self):
        org_id = uuid.uuid4()
        map_id = uuid.uuid4()
        revision = SystemMapRevision(
            organisation_id=org_id,
            system_map_id=map_id,
            revision_number=1,
            status="draft",
            content_json={"nodes": [], "edges": []},
        )
        assert revision.organisation_id == org_id
        assert revision.system_map_id == map_id
        assert revision.revision_number == 1
        assert revision.status == "draft"
        assert revision.content_json == {"nodes": [], "edges": []}
        assert revision.deleted_by is None

    def test_system_map_revision_parent_fk_is_tenant_scoped(self):
        foreign_keys = [
            constraint
            for constraint in SystemMapRevision.__table__.constraints
            if constraint.name == "fk_system_map_revisions_map_org"
        ]

        assert len(foreign_keys) == 1
        assert list(foreign_keys[0].columns.keys()) == ["system_map_id", "organisation_id"]
        assert [element.target_fullname.rsplit(".", 1)[-1] for element in foreign_keys[0].elements] == [
            "id",
            "organisation_id",
        ]

    def test_system_map_node_creation(self):
        org_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        node = SystemMapNode(
            organisation_id=org_id,
            revision_id=revision_id,
            node_key="node_1",
            node_type="pipeline",
            display_name="Pipeline Node",
            config_json={"key": "value"},
            pipeline_id=uuid.uuid4(),
            position_x=Decimal("100.5"),
            position_y=Decimal("200.0"),
        )
        assert node.organisation_id == org_id
        assert node.revision_id == revision_id
        assert node.node_key == "node_1"
        assert node.node_type == "pipeline"
        assert node.display_name == "Pipeline Node"
        assert node.config_json == {"key": "value"}
        assert node.position_x == Decimal("100.5")
        assert node.position_y == Decimal("200.0")
        assert node.deleted_by is None

    def test_system_map_relationship_creation(self):
        org_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        rel = SystemMapRelationship(
            organisation_id=org_id,
            revision_id=revision_id,
            source_node_key="node_1",
            target_node_key="node_2",
            relationship_type="flow",
            config_json={"label": "data flow"},
        )
        assert rel.organisation_id == org_id
        assert rel.revision_id == revision_id
        assert rel.source_node_key == "node_1"
        assert rel.target_node_key == "node_2"
        assert rel.relationship_type == "flow"
        assert rel.config_json == {"label": "data flow"}
        assert rel.deleted_by is None

    def test_system_map_loop_config_creation(self):
        org_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        loop_config = SystemMapLoopConfig(
            organisation_id=org_id,
            revision_id=revision_id,
            node_key="loop_1",
            target_signal="error_rate",
            observation_window_seconds=3600,
            action_pipeline_id=pipeline_id,
            budget_limit_usd=Decimal("100.00"),
            cooldown_seconds=1800,
            outcome_metric="error_rate",
        )
        assert loop_config.organisation_id == org_id
        assert loop_config.revision_id == revision_id
        assert loop_config.node_key == "loop_1"
        assert loop_config.target_signal == "error_rate"
        assert loop_config.observation_window_seconds == 3600
        assert loop_config.action_pipeline_id == pipeline_id
        assert loop_config.budget_limit_usd == Decimal("100.00")
        assert loop_config.cooldown_seconds == 1800
        assert loop_config.outcome_metric == "error_rate"
        assert loop_config.deleted_by is None

    def test_system_map_attribution_binding_creation(self):
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        map_id = uuid.uuid4()
        binding = SystemMapAttributionBinding(
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            system_map_id=map_id,
            node_key="node_1",
            binding_kind="trigger",
        )
        assert binding.organisation_id == org_id
        assert binding.pipeline_id == pipeline_id
        assert binding.system_map_id == map_id
        assert binding.node_key == "node_1"
        assert binding.binding_kind == "trigger"
        assert binding.deleted_by is None

    def test_system_map_loop_admission_attempt_creation(self):
        org_id = uuid.uuid4()
        loop_config_id = uuid.uuid4()
        attempt = SystemMapLoopAdmissionAttempt(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            idempotency_key="idem_key_123",
            status="pending",
        )
        assert attempt.organisation_id == org_id
        assert attempt.loop_config_id == loop_config_id
        assert attempt.idempotency_key == "idem_key_123"
        assert attempt.status == "pending"
        assert attempt.deleted_by is None

    def test_system_map_loop_reservation_creation(self):
        org_id = uuid.uuid4()
        loop_config_id = uuid.uuid4()
        run_id = uuid.uuid4()
        reservation = SystemMapLoopReservation(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            run_id=run_id,
            idempotency_key="res_key_456",
            cooldown_until=datetime.now(UTC) + timedelta(hours=1),
            status="active",
        )
        assert reservation.organisation_id == org_id
        assert reservation.loop_config_id == loop_config_id
        assert reservation.run_id == run_id
        assert reservation.idempotency_key == "res_key_456"
        assert reservation.status == "active"
        assert reservation.deleted_by is None


class TestSystemMapModelConstraints:
    """Test CHECK constraints and model validation logic."""

    def test_valid_node_types(self):
        """Test that all valid node types can be set."""
        valid_types = ("pipeline", "agent", "connector", "improvement_loop", "data_store", "external_system")
        for node_type in valid_types:
            node = SystemMapNode(
                organisation_id=uuid.uuid4(),
                revision_id=uuid.uuid4(),
                node_key=f"node_{node_type}",
                node_type=node_type,
                display_name=f"Test {node_type}",
            )
            assert node.node_type == node_type

    def test_valid_relationship_types(self):
        """Test that all valid relationship types can be set."""
        valid_types = ("flow", "data", "trigger", "loop_back")
        for rel_type in valid_types:
            rel = SystemMapRelationship(
                organisation_id=uuid.uuid4(),
                revision_id=uuid.uuid4(),
                source_node_key="a",
                target_node_key="b",
                relationship_type=rel_type,
            )
            assert rel.relationship_type == rel_type

    def test_valid_revision_statuses(self):
        """Test that all valid revision statuses can be set."""
        valid_statuses = ("draft", "published", "archived")
        for status in valid_statuses:
            rev = SystemMapRevision(
                organisation_id=uuid.uuid4(),
                system_map_id=uuid.uuid4(),
                revision_number=1,
                status=status,
            )
            assert rev.status == status

    def test_valid_binding_kinds(self):
        """Test that all valid binding kinds can be set."""
        valid_kinds = ("trigger", "action_pipeline")
        for kind in valid_kinds:
            binding = SystemMapAttributionBinding(
                organisation_id=uuid.uuid4(),
                pipeline_id=uuid.uuid4(),
                system_map_id=uuid.uuid4(),
                node_key="node_1",
                binding_kind=kind,
            )
            assert binding.binding_kind == kind

    def test_valid_admission_statuses(self):
        """Test that all valid admission statuses can be set."""
        valid_statuses = ("pending", "admitted", "rejected", "duplicate")
        for status in valid_statuses:
            attempt = SystemMapLoopAdmissionAttempt(
                organisation_id=uuid.uuid4(),
                loop_config_id=uuid.uuid4(),
                idempotency_key="key",
                status=status,
            )
            assert attempt.status == status

    def test_valid_reservation_statuses(self):
        """Test that all valid reservation statuses can be set."""
        valid_statuses = ("active", "consumed", "released", "expired")
        for status in valid_statuses:
            reservation = SystemMapLoopReservation(
                organisation_id=uuid.uuid4(),
                loop_config_id=uuid.uuid4(),
                run_id=uuid.uuid4(),
                idempotency_key="key",
                cooldown_until=datetime.now(UTC),
                status=status,
            )
            assert reservation.status == status

    def test_loop_config_numeric_constraints(self):
        """Test that loop config numeric fields accept valid values."""
        loop_config = SystemMapLoopConfig(
            organisation_id=uuid.uuid4(),
            revision_id=uuid.uuid4(),
            node_key="loop_1",
            target_signal="metric",
            observation_window_seconds=1,
            action_pipeline_id=uuid.uuid4(),
            budget_limit_usd=Decimal(0),
            cooldown_seconds=0,
            outcome_metric="metric",
        )
        assert loop_config.observation_window_seconds == 1
        assert loop_config.budget_limit_usd == Decimal(0)
        assert loop_config.cooldown_seconds == 0


class TestSystemMapRelationships:
    """Test model relationships are properly configured."""

    def test_system_map_revisions_relationship(self):
        org_id = uuid.uuid4()
        map_id = uuid.uuid4()
        SystemMap(organisation_id=org_id, name="Test", account_id=uuid.uuid4())
        revision = SystemMapRevision(
            organisation_id=org_id,
            system_map_id=map_id,
            revision_number=1,
        )
        # The relationship is configured but we can't easily test without a session
        # Just verify the foreign key is set
        assert revision.system_map_id == map_id

    def test_system_map_nodes_relationship(self):
        org_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        node = SystemMapNode(
            organisation_id=org_id,
            revision_id=revision_id,
            node_key="node_1",
            node_type="pipeline",
            display_name="Pipeline",
        )
        assert node.revision_id == revision_id

    def test_loop_config_relationships(self):
        org_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        loop_config = SystemMapLoopConfig(
            organisation_id=org_id,
            revision_id=revision_id,
            node_key="loop_1",
            target_signal="metric",
            observation_window_seconds=3600,
            action_pipeline_id=pipeline_id,
            budget_limit_usd=Decimal(100),
            cooldown_seconds=1800,
            outcome_metric="metric",
        )
        assert loop_config.revision_id == revision_id
        assert loop_config.action_pipeline_id == pipeline_id

    def test_admission_attempt_loop_config_relationship(self):
        org_id = uuid.uuid4()
        loop_config_id = uuid.uuid4()
        attempt = SystemMapLoopAdmissionAttempt(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            idempotency_key="key",
        )
        assert attempt.loop_config_id == loop_config_id

    def test_reservation_loop_config_relationship(self):
        org_id = uuid.uuid4()
        loop_config_id = uuid.uuid4()
        run_id = uuid.uuid4()
        reservation = SystemMapLoopReservation(
            organisation_id=org_id,
            loop_config_id=loop_config_id,
            run_id=run_id,
            idempotency_key="key",
            cooldown_until=datetime.now(UTC),
        )
        assert reservation.loop_config_id == loop_config_id
        assert reservation.run_id == run_id
