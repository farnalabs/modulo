"""Tests for immutable snapshots created from the editable live graph."""

import copy
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.exceptions import SnapshotLockNotAvailableError
from modulo.core.guardrails import fingerprint_guardrail_pins
from modulo.db.crud.pipeline_snapshot import (
    SNAPSHOT_LOCK_ATTEMPTS,
    SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS,
    create_snapshot_from_live_graph,
)
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _scalars_result(values: list[object]) -> MagicMock:
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    scalars_mock.__iter__.return_value = iter(values)
    result.scalars.return_value = scalars_mock
    return result


async def test_live_graph_becomes_executable_snapshot_with_dependency_pins() -> None:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    connector_id = uuid.uuid4()
    input_schema_id = uuid.uuid4()
    output_schema_id = uuid.uuid4()
    backend_id = uuid.uuid4()

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = org_id
    pipeline.graph_nodes_json = [
        {
            "id": str(source_id),
            "agent_id": str(agent_id),
            "connector_binding": {
                "type": "filesystem",
                "instance_id": str(connector_id),
            },
        },
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {"branch": "main"}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_gate_config = None
    edge.condition_expression = None

    agent = MagicMock()
    agent.id = agent_id
    agent.input_schema_id = input_schema_id
    agent.input_schema_version = "1.0"
    agent.output_schema_id = output_schema_id
    agent.output_schema_version = "2.0"
    agent.prompt_template = "Build the artifact"
    agent.updated_at = datetime(2026, 6, 20, tzinfo=UTC)
    agent.model_backend_id = backend_id
    agent.token_budget = None
    agent.max_input_length = None
    agent.parameter_schema_id = None
    agent.agent_command = None
    agent.agent_commands = None

    connector = MagicMock()
    connector.id = connector_id
    connector.connector_type_id = "filesystem"
    connector.name = "Workspace"
    connector.credentials_ciphertext = b"must-not-be-copied"

    input_schema = MagicMock()
    input_schema.id = input_schema_id
    input_schema.abstract_name = "input"
    output_schema = MagicMock()
    output_schema.id = output_schema_id
    output_schema.abstract_name = "output"

    backend = MagicMock()
    backend.id = backend_id
    backend.model_id = "fixed-model-version"
    backend.credentials_ciphertext = b"must-not-be-copied"

    guardrail_id = uuid.uuid4()
    guardrail_row = MagicMock()
    guardrail_row.id = guardrail_id
    guardrail_row.organisation_id = org_id
    guardrail_row.pipeline_id = pipeline_id
    guardrail_row.node_id = None
    guardrail_row.name = "no-secrets"
    guardrail_row.eval_type = "guardrail"
    guardrail_row.config_json = {
        "action": "block",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    guardrail_row.failure_behaviour = "warn"
    guardrail_row.pass_threshold = None
    guardrail_row.suite_id = None

    session = AsyncMock(spec=AsyncSession)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    unlock_result = MagicMock()
    session.execute.side_effect = [
        lock_result,
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalars_result([agent]),
        _scalars_result([connector]),
        _scalars_result([input_schema, output_schema]),
        _scalars_result([backend]),
        _scalar_result(4),
        _scalars_result([guardrail_row]),
        unlock_result,
    ]

    snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.snapshot_version == 5
    expected_nodes = copy.deepcopy(pipeline.graph_nodes_json)
    for node in expected_nodes:
        if node.get("agent_id") is not None:
            node.setdefault("prompt_template", "Build the artifact")
            node.setdefault("model_backend_id", str(backend_id))
    assert snapshot.graph_json["nodes"] == expected_nodes
    assert snapshot.graph_json["edges"] == [
        {
            "id": str(edge.id),
            "source": str(source_id),
            "target": str(target_id),
            "type": "normal",
            "hitl_gate_config": None,
            "condition_expression": None,
        }
    ]
    assert snapshot.connector_bindings_json[0]["instance_name"] == "Workspace"
    assert snapshot.schema_pins_json == [
        {"schema_id": str(input_schema_id), "version": "1.0", "abstract_name": "input"},
        {"schema_id": str(output_schema_id), "version": "2.0", "abstract_name": "output"},
    ]
    assert snapshot.model_backend_pins_json == [
        {
            "agent_id": str(agent_id),
            "model_backend_id": str(backend_id),
            "model_id": "fixed-model-version",
        }
    ]
    # FAR-223 item 10: the pipeline's guardrail rows are pinned at snapshot
    # creation so a replay evaluates the ORIGINAL conditions, never the live
    # rows. The pin is self-contained (serialized by serialize_guardrail_pin).
    assert snapshot.guardrail_pins_json == [
        {
            "id": str(guardrail_id),
            "org_id": str(org_id),
            "pipeline_id": str(pipeline_id),
            "node_id": None,
            "name": "no-secrets",
            "eval_type": "guardrail",
            "config_json": guardrail_row.config_json,
            "failure_behaviour": "warn",
            "pass_threshold": None,
            "suite_id": None,
        }
    ]
    # FAR-309 PR B: the snapshot carries a deterministic fingerprint of the
    # serialized pin set so the run-start replay seam can detect a tampered or
    # drifted pin set and fail closed. It must be a 64-char SHA-256 hex digest
    # that matches the recomputed fingerprint of the stored pins.
    assert isinstance(snapshot.guardrail_pins_fingerprint, str)
    assert len(snapshot.guardrail_pins_fingerprint) == 64
    assert snapshot.guardrail_pins_fingerprint == fingerprint_guardrail_pins(snapshot.guardrail_pins_json)
    assert "credentials" not in repr(snapshot.connector_bindings_json)
    assert "credentials" not in repr(snapshot.model_backend_pins_json)
    session.add.assert_called_once_with(snapshot)
    session.flush.assert_awaited_once()


async def test_snapshot_carries_condition_expression_for_conditional_edge() -> None:
    """A conditional edge must keep its JMESPath ``condition_expression`` when
    the live graph is frozen into a run snapshot (FAR-455). Regression guard:
    without it a conditional-edge pipeline fails every run with
    GraphValidationError CONDITION_MISSING_EXPRESSION even though the live edge
    row holds the expression.
    """
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    expr = "result.answer != 'UNKNOWN'"

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = org_id
    pipeline.graph_nodes_json = [
        {"id": str(source_id), "agent_id": None, "connector_binding": None},
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {"branch": "main"}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "conditional"
    edge.hitl_gate_config = None
    edge.condition_expression = expr

    session = AsyncMock(spec=AsyncSession)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    unlock_result = MagicMock()
    session.execute.side_effect = [
        lock_result,
        _scalar_result(pipeline),  # _load_pipeline_and_edges -> Pipeline
        _scalars_result([edge]),  # _load_pipeline_and_edges -> PipelineEdge
        _scalar_result(1),  # snapshot_version max
        _scalars_result([]),  # guardrail rows (none bound)
        unlock_result,
    ]

    snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.graph_json["edges"] == [
        {
            "id": str(edge.id),
            "source": str(source_id),
            "target": str(target_id),
            "type": "conditional",
            "hitl_gate_config": None,
            "condition_expression": expr,
        }
    ]
    session.add.assert_called_once_with(snapshot)
    session.flush.assert_awaited_once()


def _lock_attempt_result(acquired: bool) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = acquired
    return result


async def test_snapshot_lock_retry_succeeds_when_lock_frees_within_budget() -> None:
    """FAR-527: two near-simultaneous run-starts contend on the per-pipeline
    snapshot advisory lock. The bounded-wait loop must retry the (non-blocking)
    pg_try_advisory_lock attempt and succeed once the lock frees — a single
    failed attempt used to raise outright and silently drop the trigger."""

    pipeline_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = uuid.uuid4()
    pipeline.graph_nodes_json = [
        {"id": str(source_id), "agent_id": None, "connector_binding": None},
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {"branch": "main"}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_gate_config = None
    edge.condition_expression = None

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _lock_attempt_result(False),  # attempt 1: contended
        _lock_attempt_result(True),  # attempt 2: lock freed
        _scalar_result(pipeline),  # _load_pipeline_and_edges -> Pipeline
        _scalars_result([edge]),  # _load_pipeline_and_edges -> PipelineEdge
        _scalar_result(1),  # snapshot_version max
        _scalars_result([]),  # guardrail rows (none bound)
        MagicMock(),  # unlock
    ]

    with patch("modulo.db.crud.pipeline_snapshot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.pipeline_id == pipeline_id
    mock_sleep.assert_awaited_once_with(SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS)
    # Lock held across the copy, released exactly once in the finally path.
    assert session.execute.await_count == 7


async def test_snapshot_lock_raises_after_exhausting_retry_budget() -> None:
    """FAR-527: when the lock stays unavailable for the whole budget the
    function must still raise SnapshotLockNotAvailableError — after exactly
    SNAPSHOT_LOCK_ATTEMPTS lock queries (never an unlock of a lock it does
    not hold) and SNAPSHOT_LOCK_ATTEMPTS - 1 sleeps."""

    pipeline_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = _lock_attempt_result(False)

    with (
        patch("modulo.db.crud.pipeline_snapshot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        pytest.raises(SnapshotLockNotAvailableError, match=f"after {SNAPSHOT_LOCK_ATTEMPTS} attempts"),
    ):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert session.execute.await_count == SNAPSHOT_LOCK_ATTEMPTS
    assert mock_sleep.await_count == SNAPSHOT_LOCK_ATTEMPTS - 1
