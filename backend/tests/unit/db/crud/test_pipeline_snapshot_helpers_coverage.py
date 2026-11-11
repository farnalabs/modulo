"""Coverage for pipeline snapshot helper branches using real async results."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError

from farna.core.exceptions import SnapshotLockNotAvailableError
from farna.db.crud import pipeline_snapshot as snapshots


def _result(*, scalar: object | None = None, rows: list[object] | None = None) -> MagicMock:
    value = MagicMock()
    value.scalar_one.return_value = scalar
    value.scalar_one_or_none.return_value = scalar
    value.scalars.return_value.all.return_value = rows or []
    value.scalars.return_value.__iter__.return_value = iter(rows or [])
    value.mappings.return_value.all.return_value = rows or []
    return value


@pytest.mark.anyio
async def test_snapshot_lock_inspection_and_termination_paths() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _result(rows=[{"pid": 22, "application_name": None, "state": "active"}]),
            _result(rows=[22, 23]),
            _result(scalar=True),
            _result(scalar=False),
        ]
    )
    pipeline_id = uuid.uuid4()

    with patch.object(snapshots, "_masked_snapshot_lock_keys", return_value=(1, 2)):
        inspected = await snapshots.inspect_snapshot_lock(session, pipeline_id)
        terminated = await snapshots.terminate_snapshot_lock_holders(session, pipeline_id)

    assert inspected["held"] is True
    assert inspected["holders"][0]["pid"] == 22
    assert terminated == {"released": 1, "pids": [22]}
    assert session.execute.await_count == 4


@pytest.mark.anyio
async def test_snapshot_termination_maps_privilege_error() -> None:
    session = AsyncMock()
    denied = ProgrammingError("denied", {}, RuntimeError("denied"))
    denied.orig = SimpleNamespace(sqlstate="42501")
    session.execute = AsyncMock(
        side_effect=[
            _result(rows=[44]),
            denied,
        ]
    )
    with (
        patch.object(snapshots, "_masked_snapshot_lock_keys", return_value=(1, 2)),
        pytest.raises(snapshots.SnapshotLockTerminateDeniedError),
    ):
        await snapshots.terminate_snapshot_lock_holders(session, uuid.uuid4())


@pytest.mark.anyio
async def test_open_and_poll_snapshot_lock_retries_then_acquires() -> None:
    connection = AsyncMock()
    connection.execute = AsyncMock(side_effect=[_result(scalar=False), _result(scalar=True)])
    engine = MagicMock()
    engine.connect = AsyncMock(return_value=connection)

    with (
        patch.object(snapshots, "SNAPSHOT_LOCK_ATTEMPTS", 2),
        patch.object(snapshots, "SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS", 0),
    ):
        result = await snapshots._open_and_poll_snapshot_lock(engine, pipeline_id=uuid.uuid4(), key1=1, key2=2)

    assert result is connection
    connection.close.assert_not_awaited()


@pytest.mark.anyio
async def test_open_and_poll_snapshot_lock_exhaustion_disposes_connection() -> None:
    connection = AsyncMock()
    connection.execute = AsyncMock(return_value=_result(scalar=False))
    engine = MagicMock()
    engine.connect = AsyncMock(return_value=connection)

    with (
        patch.object(snapshots, "SNAPSHOT_LOCK_ATTEMPTS", 1),
        pytest.raises(SnapshotLockNotAvailableError),
    ):
        await snapshots._open_and_poll_snapshot_lock(engine, pipeline_id=uuid.uuid4(), key1=1, key2=2)

    connection.close.assert_awaited_once()


@pytest.mark.anyio
async def test_acquire_snapshot_lock_maps_timeout_and_connection_error() -> None:
    session = MagicMock()
    engine = MagicMock()
    with (
        patch.object(snapshots, "_snapshot_lock_engine", return_value=engine),
        patch.object(snapshots, "_open_and_poll_snapshot_lock", AsyncMock(side_effect=TimeoutError)),
        pytest.raises(SnapshotLockNotAvailableError, match="within"),
    ):
        await snapshots._acquire_snapshot_lock(session, pipeline_id=uuid.uuid4(), key1=1, key2=2)

    with (
        patch.object(snapshots, "_snapshot_lock_engine", return_value=engine),
        patch.object(snapshots, "_open_and_poll_snapshot_lock", AsyncMock(side_effect=RuntimeError("down"))),
        pytest.raises(SnapshotLockNotAvailableError, match="RuntimeError"),
    ):
        await snapshots._acquire_snapshot_lock(session, pipeline_id=uuid.uuid4(), key1=1, key2=2)


@pytest.mark.anyio
async def test_release_snapshot_lock_invalidates_when_unlock_fails() -> None:
    connection = AsyncMock()
    connection.execute = AsyncMock(side_effect=RuntimeError("transport"))
    await snapshots._release_snapshot_lock(connection, key1=1, key2=2)
    connection.invalidate.assert_awaited_once()


@pytest.mark.anyio
async def test_load_pipeline_and_edges_returns_none_for_missing_pipeline() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result(scalar=None))
    assert await snapshots._load_pipeline_and_edges(session, uuid.uuid4()) == (None, [], [])


@pytest.mark.anyio
async def test_load_pipeline_and_edges_serializes_edge_condition() -> None:
    pipeline = SimpleNamespace(graph_nodes_json=[{"id": "node"}])
    edge = SimpleNamespace(
        id=uuid.uuid4(),
        source_node_id=uuid.uuid4(),
        target_node_id=uuid.uuid4(),
        edge_type="conditional",
        hitl_review_config={"label": "Review"},
        condition_expression="value > 0",
        created_at=1,
    )
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_result(scalar=pipeline), _result(rows=[edge])])
    result_pipeline, nodes, edges = await snapshots._load_pipeline_and_edges(session, uuid.uuid4())
    assert result_pipeline is pipeline
    assert nodes == [{"id": "node"}]
    assert edges[0]["condition_expression"] == "value > 0"


def test_snapshot_pure_helpers_cover_defaults_and_conflicts() -> None:
    assert snapshots._ids([None, "00000000-0000-0000-0000-000000000001"]) == {
        uuid.UUID("00000000-0000-0000-0000-000000000001")
    }
    assert snapshots._collect_parameter_set_ids([{}, {"parameter_set_id": uuid.UUID(int=1)}]) == {uuid.UUID(int=1)}
    assert snapshots._pipeline_lock_keys(uuid.UUID(int=1)) != snapshots._pipeline_lock_keys(uuid.UUID(int=2))
    assert snapshots._masked_snapshot_lock_keys(uuid.UUID(int=1))[0] >= 0
    assert (
        snapshots._is_snapshot_version_conflict(IntegrityError("uq_pipeline_snapshot_version", {}, RuntimeError()))
        is True
    )
    assert snapshots._is_snapshot_version_conflict(IntegrityError("other", {}, RuntimeError())) is False


def test_snapshot_model_and_policy_pin_helpers() -> None:
    agent_id = uuid.uuid4()
    schema_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        token_budget=100,
        prompt_template="prompt",
        model_backend_id=backend_id,
        agent_commands=["run"],
        parameter_schema_id=schema_id,
        schema_profile="strict",
        input_schema_id=schema_id,
        input_schema_version="1",
        output_schema_id=None,
        output_schema_version=None,
        updated_at=SimpleNamespace(isoformat=lambda: "now"),
    )
    node = {"id": str(uuid.uuid4()), "schema_profile": "explicit"}
    assert snapshots._apply_agent_fields(node, agent) == schema_id
    assert node["schema_profile"] == "explicit"
    schema_pins = snapshots._build_schema_pins([agent], {schema_id: SimpleNamespace(abstract_name="Input")})
    assert schema_pins[0]["version"] == "1"
    prompt_pins, backend_pins = snapshots._build_prompt_and_backend_pins(
        [agent], {backend_id: SimpleNamespace(model_id="model")}
    )
    assert prompt_pins[0]["agent_id"] == str(agent_id)
    assert backend_pins[0]["model_id"] == "model"
    gate = SimpleNamespace(
        id=uuid.uuid4(),
        eval_id=uuid.uuid4(),
        action="block",
        node_id=uuid.uuid4(),
        evaluation_mode="human",
    )
    pins = snapshots._build_policy_gate_pins([gate])
    assert pins[0]["evaluation_mode"] == "human"
    assert not snapshots._build_policy_gate_pins([])


@pytest.mark.anyio
async def test_allocate_returns_none_for_programming_error_before_version_read() -> None:
    source = SimpleNamespace(pipeline=SimpleNamespace(id=uuid.uuid4()))
    session = AsyncMock()
    session.begin_nested = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("missing", {}, RuntimeError()))
    cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested.return_value = cm
    result = await snapshots._allocate_snapshot(
        session,
        source,
        account_id=None,
        version_kind="run",
        created_kind="run",
        draft=False,
        channel="none",
    )
    assert result is None


@pytest.mark.anyio
async def test_dispose_rethrows_cancellation() -> None:
    connection = AsyncMock()
    connection.close = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await snapshots._dispose_snapshot_lock_connection(connection, can_pool=True)
