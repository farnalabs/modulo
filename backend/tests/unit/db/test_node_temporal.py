"""Unit tests for Node model temporal fields — timeout_seconds, retry_count, retry_delay_seconds."""

import uuid

from sqlalchemy import CheckConstraint, MetaData, Table

from modulo.db.models.node import Node


class TestNodeTemporalFields:
    def test_timeout_seconds_is_optional(self) -> None:
        node = Node(
            organisation_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            name="test",
            created_by=uuid.uuid4(),
        )
        assert node.timeout_seconds is None

    def test_retry_count_is_optional(self) -> None:
        node = Node(
            organisation_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            name="test",
            created_by=uuid.uuid4(),
        )
        assert node.retry_count is None

    def test_retry_delay_seconds_is_optional(self) -> None:
        node = Node(
            organisation_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            name="test",
            created_by=uuid.uuid4(),
        )
        assert node.retry_delay_seconds is None

    def test_can_set_all_temporal_fields(self) -> None:
        node = Node(
            organisation_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            name="test",
            created_by=uuid.uuid4(),
            timeout_seconds=60,
            retry_count=3,
            retry_delay_seconds=10,
        )
        assert node.timeout_seconds == 60
        assert node.retry_count == 3
        assert node.retry_delay_seconds == 10

    def test_can_set_some_temporal_fields(self) -> None:
        node = Node(
            organisation_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            name="test",
            created_by=uuid.uuid4(),
            timeout_seconds=120,
        )
        assert node.timeout_seconds == 120
        assert node.retry_count is None
        assert node.retry_delay_seconds is None

    def test_can_set_zero_retry_count(self) -> None:
        node = Node(
            organisation_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            name="test",
            created_by=uuid.uuid4(),
            retry_count=0,
        )
        assert node.retry_count == 0

    def test_can_set_zero_retry_delay(self) -> None:
        node = Node(
            organisation_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            name="test",
            created_by=uuid.uuid4(),
            retry_delay_seconds=0,
        )
        assert node.retry_delay_seconds == 0

    def test_check_constraints_defined(self) -> None:
        table = Node.__table__
        assert isinstance(table, Table)

        constraint_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
        assert "ck_nodes_timeout_seconds" in constraint_names
        assert "ck_nodes_retry_count" in constraint_names
        assert "ck_nodes_retry_delay_seconds" in constraint_names

    def test_check_constraint_expressions(self) -> None:
        meta = MetaData()
        table = Node.__table__
        table.to_metadata(meta)

        constraints: dict[str, CheckConstraint] = {
            c.name: c for c in meta.tables["nodes"].constraints if isinstance(c, CheckConstraint)
        }

        timeout_ck = constraints.get("ck_nodes_timeout_seconds")
        assert timeout_ck is not None
        sql = str(timeout_ck.sqltext)
        assert "timeout_seconds" in sql
        assert "> 0" in sql or "IS NULL" in sql
