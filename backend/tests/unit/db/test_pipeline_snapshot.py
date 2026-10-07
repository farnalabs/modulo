"""Tests for immutable snapshots created from the editable live graph."""

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.core.exceptions import SnapshotLockNotAvailableError
from modulo.core.guardrails import fingerprint_guardrail_pins
from modulo.db.crud.pipeline_snapshot import (
    SNAPSHOT_LOCK_ATTEMPTS,
    SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS,
    SNAPSHOT_VERSION_ATTEMPTS,
    SnapshotLockTerminateDeniedError,
    SnapshotVersionAllocationError,
    _pipeline_lock_keys,
    create_snapshot_from_live_graph,
    inspect_snapshot_lock,
    terminate_snapshot_lock_holders,
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


def _bind_lock_connection(session: AsyncMock, *attempts: MagicMock) -> Any:
    """Build the stubbed DEDICATED lock engine and return a patch that installs it.

    FAR-1287: the lock connection is drawn from a dedicated NullPool engine that
    ``_dedicated_lock_engine`` resolves from the caller's bound engine — never
    from ``session.bind``'s pool — so unit tests replace that resolver (they
    must never build a real engine). Enter the returned patch around the
    snapshot call.

    The stub engine's ``connect`` yields a lock connection carrying *attempts*
    (the ``pg_try_advisory_lock`` results) plus one trailing result for the
    ``pg_advisory_unlock`` — the trailing entry is simply unused when no unlock
    runs (budget exhausted). ``session.bind`` is wired to the same stub engine so
    the derivation contract (``isinstance(bind, AsyncEngine)``) stays exercised.

    After the ``with`` block, the lock connection is reachable as
    ``session.bind.connect.return_value``.
    """
    lock_conn = AsyncMock()
    lock_conn.execute.side_effect = [*attempts, MagicMock()]
    engine = MagicMock(spec=AsyncEngine)
    engine.connect = AsyncMock(return_value=lock_conn)
    session.bind = engine
    return patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=engine)


def _lock_attempt_result(acquired: bool) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = acquired
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
    edge.hitl_review_config = None
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
    agent.agent_commands = None
    agent.schema_profile = None

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
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalars_result([agent]),
        _scalars_result([connector]),
        _scalars_result([input_schema, output_schema]),
        _scalars_result([backend]),
        _scalar_result(4),
        _scalars_result([guardrail_row]),
        _scalars_result([]),  # policy gate rows (empty - no gates bound)
    ]

    # FAR-1287: lock/unlock run on the dedicated lock connection, not the session.
    with _bind_lock_connection(session, _lock_attempt_result(True)):
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
            "hitl_review_config": None,
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
    edge.hitl_review_config = None
    edge.condition_expression = expr

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),  # _load_pipeline_and_edges -> Pipeline
        _scalars_result([edge]),  # _load_pipeline_and_edges -> PipelineEdge
        _scalar_result(1),  # snapshot_version max
        _scalars_result([]),  # guardrail rows (none bound)
        _scalars_result([]),  # policy gate rows (none bound)
    ]

    # FAR-1287: lock/unlock run on the dedicated lock connection, not the session.
    with _bind_lock_connection(session, _lock_attempt_result(True)):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.graph_json["edges"] == [
        {
            "id": str(edge.id),
            "source": str(source_id),
            "target": str(target_id),
            "type": "conditional",
            "hitl_review_config": None,
            "condition_expression": expr,
        }
    ]
    session.add.assert_called_once_with(snapshot)
    session.flush.assert_awaited_once()


@pytest.mark.parametrize("autonomy", ["fully_autonomous", "notify_on_complete", None])
async def test_snapshot_carries_pipeline_default_autonomy_level(autonomy: str | None) -> None:
    """The pipeline-level autonomy setting must be frozen into the run
    snapshot so the executor can seed ``_pipeline_default_autonomy`` for HITL
    gate nodes. Legacy pipelines with a NULL setting keep snapshotting NULL.
    """
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
    pipeline.default_autonomy_level = autonomy

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalar_result(1),
        _scalars_result([]),
        _scalars_result([]),  # policy gate rows (none bound)
    ]

    # FAR-1287: lock/unlock run on the dedicated lock connection, not the session.
    with _bind_lock_connection(session, _lock_attempt_result(True)):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.default_autonomy_level == autonomy


@pytest.mark.parametrize("ceiling", ["fully_autonomous", "notify_on_complete", None])
async def test_snapshot_carries_pipeline_max_autonomy_level(ceiling: str | None) -> None:
    """FAR-1163: the pipeline's autonomy CEILING must be frozen into the run
    snapshot alongside the default — runs read the SNAPSHOT, not the live
    row, so a write-only ceiling would be invisible to execution. Legacy
    pipelines with a NULL ceiling keep snapshotting NULL (effective ceiling =
    default)."""
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
    pipeline.default_autonomy_level = "manual_approval"
    pipeline.max_autonomy_level = ceiling

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalar_result(1),
        _scalars_result([]),
        _scalars_result([]),  # policy gate rows (none bound)
    ]

    # FAR-1287: lock/unlock run on the dedicated lock connection, not the session.
    with _bind_lock_connection(session, _lock_attempt_result(True)):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.max_autonomy_level == ceiling


def test_snapshot_to_dict_serialises_max_autonomy_level() -> None:
    """FAR-1163: the clone/plain-data serializer carries the ceiling so a
    cloned pipeline never silently loses (or invents) a ceiling."""
    from modulo.db.crud.pipeline import _snapshot_to_dict

    snap = PipelineSnapshot(
        organisation_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        snapshot_version=1,
        graph_json={"nodes": [], "edges": []},
        connector_bindings_json=[],
        schema_pins_json=[],
        prompt_pins_json=[],
        model_backend_pins_json=[],
        default_autonomy_level="manual_approval",
        max_autonomy_level="notify_on_complete",
        config_json={},
        run_context_defaults={},
    )

    plain = _snapshot_to_dict(snap, pins=[])

    assert plain["max_autonomy_level"] == "notify_on_complete"
    assert plain["default_autonomy_level"] == "manual_approval"


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
    edge.hitl_review_config = None
    edge.condition_expression = None

    session = AsyncMock(spec=AsyncSession)
    lock_stub = _bind_lock_connection(
        session,
        _lock_attempt_result(False),  # attempt 1: contended
        _lock_attempt_result(True),  # attempt 2: lock freed
    )
    session.execute.side_effect = [
        _scalar_result(pipeline),  # _load_pipeline_and_edges -> Pipeline
        _scalars_result([edge]),  # _load_pipeline_and_edges -> PipelineEdge
        _scalar_result(1),  # snapshot_version max
        _scalars_result([]),  # guardrail rows (none bound)
        _scalars_result([]),  # policy gate rows (none bound)
    ]

    with (
        lock_stub,
        patch("modulo.db.crud.pipeline_snapshot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
    # The stub lock engine's connection (session.bind IS the stubbed engine).
    lock_conn = session.bind.connect.return_value

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.pipeline_id == pipeline_id
    mock_sleep.assert_awaited_once_with(SNAPSHOT_LOCK_RETRY_SLEEP_SECONDS)
    # FAR-1287: the two lock attempts + the single unlock all ran on the
    # dedicated lock connection, which is then returned to the pool.
    assert lock_conn.execute.await_count == 3
    lock_conn.close.assert_awaited_once()
    # The caller's session ran only the 5 graph-copy reads — never a lock query,
    # so an aborted caller transaction can no longer strand the advisory lock.
    assert session.execute.await_count == 5
    assert not any("pg_advisory" in str(call.args[0]) for call in session.execute.call_args_list)


async def test_snapshot_lock_raises_after_exhausting_retry_budget() -> None:
    """FAR-527: when the lock stays unavailable for the whole budget the
    function must still raise SnapshotLockNotAvailableError — after exactly
    SNAPSHOT_LOCK_ATTEMPTS lock queries (never an unlock of a lock it does
    not hold) and SNAPSHOT_LOCK_ATTEMPTS - 1 sleeps.

    FAR-1287: all of those lock queries run on the dedicated lock connection;
    the caller's session never issues one."""

    pipeline_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    lock_stub = _bind_lock_connection(session, *[_lock_attempt_result(False)] * SNAPSHOT_LOCK_ATTEMPTS)

    with (
        lock_stub,
        patch("modulo.db.crud.pipeline_snapshot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        pytest.raises(SnapshotLockNotAvailableError, match=f"after {SNAPSHOT_LOCK_ATTEMPTS} attempts"),
    ):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
    lock_conn = session.bind.connect.return_value

    assert lock_conn.execute.await_count == SNAPSHOT_LOCK_ATTEMPTS
    assert session.execute.await_count == 0
    assert mock_sleep.await_count == SNAPSHOT_LOCK_ATTEMPTS - 1
    # Never acquired, so the connection is pooled again without an unlock, and
    # (its state being provably lock-free) without an invalidate either.
    lock_conn.close.assert_awaited_once()
    lock_conn.invalidate.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-1287 (pool-contention regression): the lock must not consume a main-pool
# slot, and acquisition must be bounded.
# ---------------------------------------------------------------------------


async def test_lock_connection_comes_from_a_dedicated_engine_not_the_callers_pool() -> None:
    """FAR-1287: the lock is drawn from the DEDICATED lock engine.

    ``caller_engine`` is the pool the session was built from — the main web
    pool in production. If the lock checked a slot out of it, every concurrent
    snapshot creation would hold TWO main-pool connections (caller + lock) and a
    burst would drive every waiter into the 30s pool checkout timeout, surfacing
    as ``sqlalchemy.exc.TimeoutError`` instead of the lock contract.
    """
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
    pipeline.run_context_defaults = {}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None

    session = AsyncMock(spec=AsyncSession)
    caller_engine = MagicMock(spec=AsyncEngine)  # the MAIN pool
    caller_engine.connect = AsyncMock()
    session.bind = caller_engine

    lock_conn = AsyncMock()
    lock_conn.execute.side_effect = [_lock_attempt_result(True), MagicMock()]
    lock_engine = MagicMock(spec=AsyncEngine)
    lock_engine.connect = AsyncMock(return_value=lock_conn)
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalar_result(1),
        _scalars_result([]),
        _scalars_result([]),  # policy gate rows (none bound)
    ]

    with patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=lock_engine) as derive:
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    # The main pool was never checked out for the lock ...
    caller_engine.connect.assert_not_called()
    # ... the resolver still keyed off the caller's engine, and the lock itself
    # (1 try-lock + 1 unlock) ran on the dedicated engine, which released it.
    derive.assert_called_once_with(caller_engine)
    lock_engine.connect.assert_awaited_once()
    assert lock_conn.execute.await_count == 2
    lock_conn.close.assert_awaited_once()


async def test_unavailable_lock_source_fails_fast_as_snapshot_lock_not_available() -> None:
    """FAR-1287 (bounded acquisition): a lock source that never answers surfaces
    as ``SnapshotLockNotAvailableError`` inside the acquisition bound — never a
    30s pool checkout stall.

    The stub ``connect`` never resolves, so the ONLY way this test finishes is
    ``_SNAPSHOT_LOCK_ACQUIRE_TIMEOUT_SECONDS`` (patched small here). If the
    bound were removed this test would hang and fail under pytest-timeout — that
    is the oracle for the regression.
    """
    pipeline_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    session.bind = MagicMock(spec=AsyncEngine)
    hanging_engine = MagicMock(spec=AsyncEngine)
    never = asyncio.Event()
    hanging_engine.connect = AsyncMock(side_effect=never.wait)

    with (
        patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=hanging_engine),
        patch("modulo.db.crud.pipeline_snapshot._SNAPSHOT_LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.05),
        pytest.raises(SnapshotLockNotAvailableError, match="lock source unavailable"),
    ):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    # Acquisition failed before a single graph read.
    assert session.execute.await_count == 0


async def test_lock_source_connect_error_is_relabelled_snapshot_lock_unavailable() -> None:
    """An unusable lock source (connection refused, auth failure, saturated
    Postgres) is reported through the existing lock contract with the original
    error chained — never as a raw driver exception thrown out of a snapshot
    call, and never as a bare 30s stall."""
    pipeline_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    session.bind = MagicMock(spec=AsyncEngine)
    broken_engine = MagicMock(spec=AsyncEngine)
    broken_engine.connect = AsyncMock(side_effect=RuntimeError("connection refused"))

    with (
        patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=broken_engine),
        pytest.raises(SnapshotLockNotAvailableError, match=r"lock source unavailable \(RuntimeError\)") as excinfo,
    ):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert session.execute.await_count == 0


async def test_dedicated_lock_engine_is_null_pool_and_cached_per_url() -> None:
    """FAR-1287: the lock engine is a per-URL ``NullPool`` engine.

    NullPool means no pooled slots to contend (nothing to starve the main pool
    with, nothing to time out after 30s) and a ``close()`` that physically ends
    the PG session — so a released lock can never outlive its connection. The
    URL comes from the caller's engine so the lock always lands in the SAME
    database the snapshot is written to; the engine is cached per URL like
    ``db.session``'s shared engine. Never connects (engine construction is
    lazy), so this needs no database.
    """
    from modulo.db.crud.pipeline_snapshot import _dedicated_lock_engine

    probe_url = f"postgresql+asyncpg://probe:{uuid.uuid4().hex}@localhost/probe"
    other_url = f"postgresql+asyncpg://probe:{uuid.uuid4().hex}@localhost/other"
    source = create_async_engine(probe_url)
    other_source = create_async_engine(other_url)
    try:
        with patch("modulo.db.crud.pipeline_snapshot.get_settings") as settings:
            settings.return_value.database_url = probe_url
            lock_engine = _dedicated_lock_engine(source)

            assert lock_engine is not source
            assert isinstance(lock_engine.pool, NullPool)
            assert lock_engine.url.render_as_string(hide_password=False) == source.url.render_as_string(
                hide_password=False
            )
            # Cached per URL — a second resolution returns the same engine.
            assert _dedicated_lock_engine(source) is lock_engine
            # A different database gets its own lock engine (its own locks).
            assert _dedicated_lock_engine(other_source) is not lock_engine
    finally:
        await source.dispose()
        await other_source.dispose()


async def test_session_without_engine_binding_raises_runtime_error() -> None:
    """The derivation contract stays loud: a session with no usable engine
    binding is a hard error, never a silent fallback onto the caller's own
    connection — that fallback is exactly what leaked the lock originally."""
    session = AsyncMock(spec=AsyncSession)  # .bind is a plain MagicMock, not an AsyncEngine

    with pytest.raises(RuntimeError, match="bound to an AsyncEngine"):
        await create_snapshot_from_live_graph(session, pipeline_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# FAR-1287 teardown edge cases: the dispose/unlock failure arms.
# ---------------------------------------------------------------------------


async def test_dispose_lock_connection_reraises_cancellation() -> None:
    """A cancellation must not be swallowed while the dedicated lock connection
    is being disposed: ``asyncio.shield`` lets the dispose run to completion so
    the lock cannot leak, but the cancellation still propagates to the caller."""
    from modulo.db.crud.pipeline_snapshot import _dispose_snapshot_lock_connection

    lock_conn = AsyncMock()
    lock_conn.close.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _dispose_snapshot_lock_connection(lock_conn, can_pool=True)

    lock_conn.close.assert_awaited_once()
    lock_conn.invalidate.assert_not_awaited()


async def test_dispose_lock_connection_swallows_and_logs_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A best-effort teardown failure is reported loudly but must never mask the
    caller's own error — the dispose returns normally after logging."""
    import logging

    from modulo.db.crud.pipeline_snapshot import _dispose_snapshot_lock_connection

    lock_conn = AsyncMock()
    lock_conn.close.side_effect = RuntimeError("teardown boom")

    with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline_snapshot"):
        await _dispose_snapshot_lock_connection(lock_conn, can_pool=True)

    assert any("snapshot_lock_connection_dispose_failed" in record.getMessage() for record in caplog.records)


async def test_release_lock_invalidates_physical_session_when_unlock_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the ``pg_advisory_unlock`` itself fails (transport/abort), the lock
    state is unconfirmed, so the connection is invalidated (physical session
    teardown) rather than pooled — and the failure is logged."""
    import logging

    from modulo.db.crud.pipeline_snapshot import _release_snapshot_lock

    lock_conn = AsyncMock()
    lock_conn.execute.side_effect = RuntimeError("transport gone")

    with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline_snapshot"):
        await _release_snapshot_lock(lock_conn, key1=1, key2=2)

    lock_conn.invalidate.assert_awaited_once()
    lock_conn.close.assert_not_awaited()
    assert any("snapshot_lock_unlock_failed" in record.getMessage() for record in caplog.records)


def test_build_lock_engine_non_postgres_skips_postgres_connect_args() -> None:
    """A non-Postgres bind (SQLite/MySQL deployments) takes none of the asyncpg
    knobs — only the shared connect timeout is passed."""
    from modulo.db.crud.pipeline_snapshot import _build_lock_engine

    bind = MagicMock(spec=AsyncEngine)
    bind.url.drivername = "sqlite+aiosqlite"

    with patch("modulo.db.crud.pipeline_snapshot.create_async_engine") as create:
        sentinel = object()
        create.return_value = sentinel
        assert _build_lock_engine(bind) is sentinel

    assert create.call_args.kwargs["connect_args"] == {"timeout": 10}
    assert create.call_args.kwargs["poolclass"] is NullPool


def test_build_lock_engine_skips_ssl_when_settings_url_is_not_postgres() -> None:
    """The TLS posture is read from ``settings.database_url`` via
    ``split_engine_sslmode``; a non-Postgres settings URL resolves to ``None``,
    meaning no ``ssl``/``statement_cache_size`` connect args even for a Postgres
    bind — the non-Postgres arm of the SSL guard."""
    from modulo.db.crud.pipeline_snapshot import _build_lock_engine

    bind = MagicMock(spec=AsyncEngine)
    bind.url.drivername = "postgresql+asyncpg"

    with (
        patch("modulo.db.crud.pipeline_snapshot.get_settings") as settings,
        patch("modulo.db.crud.pipeline_snapshot.create_async_engine") as create,
    ):
        settings.return_value.database_url = "sqlite+aiosqlite:///probe.db"
        sentinel = object()
        create.return_value = sentinel
        assert _build_lock_engine(bind) is sentinel

    assert create.call_args.kwargs["connect_args"] == {"timeout": 10}
    assert create.call_args.kwargs["poolclass"] is NullPool


# ---------------------------------------------------------------------------
# FAR-1287 Part 2 (Workstream B): bounded optimistic retry of the
# snapshot_version allocation.
# ---------------------------------------------------------------------------


def _two_node_pipeline(pipeline_id: uuid.UUID) -> tuple[MagicMock, MagicMock]:
    """A minimal pipeline + edge pair with no agents (no reference-model reads)."""
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = uuid.uuid4()
    pipeline.graph_nodes_json = [
        {"id": str(source_id), "agent_id": None, "connector_binding": None},
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None
    return pipeline, edge


def _version_conflict() -> IntegrityError:
    """The unique violation a concurrent same-pipeline creator produces."""
    return IntegrityError(
        "INSERT INTO pipeline_snapshots (snapshot_version)",
        {},
        Exception('duplicate key value violates unique constraint "uq_pipeline_snapshot_version"'),
    )


def _per_attempt_reads(max_version: int) -> list[MagicMock]:
    """The three reads one attempt issues: version max, guardrail pins, policy gates."""
    return [_scalar_result(max_version), _scalars_result([]), _scalars_result([])]


async def test_snapshot_version_conflict_retries_and_lands_on_the_next_version() -> None:
    """FAR-1287 Part 2: a collision on ``uq_pipeline_snapshot_version`` rolls
    back to a SAVEPOINT and re-allocates instead of surfacing IntegrityError at
    the caller.

    The first attempt reads max=0 (the competitor's row is not committed yet),
    inserts version 1 and fails on the unique constraint; the retry re-reads
    max=1 and lands on version 2. Each attempt runs in its own ``begin_nested``
    savepoint, and the FAILED attempt's row object is replaced — a rollback
    must never let a retry re-use (or duplicate) it.
    """
    pipeline_id = uuid.uuid4()
    pipeline, edge = _two_node_pipeline(pipeline_id)

    session = AsyncMock(spec=AsyncSession)
    session.flush.side_effect = [_version_conflict(), None]
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        *_per_attempt_reads(0),  # attempt 1: max=0 -> version 1 -> conflict
        *_per_attempt_reads(1),  # attempt 2: competitor committed -> max=1 -> version 2
    ]

    with _bind_lock_connection(session, _lock_attempt_result(True)):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.snapshot_version == 2
    assert session.begin_nested.call_count == 2
    assert session.flush.await_count == 2
    added = [call.args[0] for call in session.add.call_args_list]
    assert [obj.snapshot_version for obj in added] == [1, 2]
    assert added[0] is not added[1]


async def test_snapshot_version_conflict_exhausts_the_bounded_retry_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exhausting ``SNAPSHOT_VERSION_ATTEMPTS`` raises the specific
    ``SnapshotVersionAllocationError`` — never a silent ``None``, and never an
    unbounded retry loop. The error subclasses ``IntegrityError`` so every
    existing route/trigger handler keeps its 409 mapping, and it chains the
    original driver error. The terminal exhaustion also emits its own warning
    (FAR-1287 review nit) so a wedged allocation is visible by the log line
    alone when the propagated exception is filtered out."""
    pipeline_id = uuid.uuid4()
    pipeline, edge = _two_node_pipeline(pipeline_id)

    session = AsyncMock(spec=AsyncSession)
    session.flush.side_effect = _version_conflict()
    reads: list[MagicMock] = [_scalar_result(pipeline), _scalars_result([edge])]
    for _ in range(SNAPSHOT_VERSION_ATTEMPTS):
        reads.extend(_per_attempt_reads(0))
    session.execute.side_effect = reads

    with (
        _bind_lock_connection(session, _lock_attempt_result(True)),
        caplog.at_level("WARNING", logger="modulo.db.crud.pipeline_snapshot"),
        pytest.raises(SnapshotVersionAllocationError, match="could not allocate snapshot_version") as excinfo,
    ):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert session.begin_nested.call_count == SNAPSHOT_VERSION_ATTEMPTS
    assert session.flush.await_count == SNAPSHOT_VERSION_ATTEMPTS
    assert "uq_pipeline_snapshot_version" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, IntegrityError)
    # The subclass contract: existing `except IntegrityError` arms still catch it.
    assert isinstance(excinfo.value, IntegrityError)
    # Terminal exhaustion is logged, not just raised, so it survives log filtering.
    assert "snapshot_version_allocation_exhausted" in caplog.text


async def test_non_version_integrity_error_is_never_retried() -> None:
    """Only the allocation collision is retried. Any other integrity failure
    (FK, CHECK) would fail identically on every attempt, so it propagates
    unchanged after the FIRST savepoint — no wasted retries, no masking."""
    pipeline_id = uuid.uuid4()
    pipeline, edge = _two_node_pipeline(pipeline_id)

    session = AsyncMock(spec=AsyncSession)
    session.flush.side_effect = IntegrityError(
        "INSERT INTO snapshot_schema_pins",
        {},
        Exception('insert or update violates foreign key constraint "fk_pins_schema"'),
    )
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        *_per_attempt_reads(0),
    ]

    with _bind_lock_connection(session, _lock_attempt_result(True)), pytest.raises(IntegrityError) as excinfo:
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert type(excinfo.value) is IntegrityError  # NOT the retrying subclass
    assert session.begin_nested.call_count == 1
    assert session.flush.await_count == 1


async def test_programming_error_at_the_version_read_still_returns_none() -> None:
    """The pre-existing contract is preserved: a missing ``snapshot_version``
    column (migration not applied yet) reports "no snapshot". The error is
    raised OUT of the savepoint block so SQLAlchemy ROLLS BACK to it — catching
    it inside would RELEASE the savepoint on an aborted transaction (25P02) and
    mask the original error."""
    pipeline_id = uuid.uuid4()
    pipeline, edge = _two_node_pipeline(pipeline_id)

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        ProgrammingError("SELECT max(snapshot_version)", {}, Exception("column does not exist")),
    ]

    with _bind_lock_connection(session, _lock_attempt_result(True)):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert snapshot is None
    assert session.begin_nested.call_count == 1
    session.flush.assert_not_awaited()


async def test_programming_error_after_the_version_read_propagates() -> None:
    """A ``ProgrammingError`` raised AFTER the version read completes (during
    the guardrail/policy pin loads or the insert) is NOT the pre-existing
    "missing column" signal and must propagate, so a real schema fault is never
    masked as "no snapshot". The ``version_read_completed`` guard distinguishes
    the two: only a failure at the version read itself maps to ``None``."""
    pipeline_id = uuid.uuid4()
    pipeline, edge = _two_node_pipeline(pipeline_id)

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalar_result(0),  # the version read completes ...
        ProgrammingError("SELECT evals", {}, Exception("relation does not exist")),  # ... then a later read fails
    ]

    with _bind_lock_connection(session, _lock_attempt_result(True)), pytest.raises(ProgrammingError):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert session.begin_nested.call_count == 1
    session.flush.assert_not_awaited()


async def test_zero_allocation_attempts_still_fails_loudly() -> None:
    """The guard after the retry loop is reachable when
    ``SNAPSHOT_VERSION_ATTEMPTS`` is misconfigured below 1: with an empty
    attempt range the loop body never runs, and the function must still raise
    the typed allocation error rather than silently return ``None``."""
    pipeline_id = uuid.uuid4()
    pipeline, edge = _two_node_pipeline(pipeline_id)

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [_scalar_result(pipeline), _scalars_result([edge])]

    with (
        _bind_lock_connection(session, _lock_attempt_result(True)),
        patch("modulo.db.crud.pipeline_snapshot.SNAPSHOT_VERSION_ATTEMPTS", 0),
        pytest.raises(SnapshotVersionAllocationError) as excinfo,
    ):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert excinfo.value.attempts == 0
    session.begin_nested.assert_not_called()
    session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-1287 Part 2 (Workstream A): the operator snapshot-lock diagnostic and
# the terminate-only-matching-holders clear path.
# ---------------------------------------------------------------------------


def _negative_key_pipeline_id() -> tuple[uuid.UUID, tuple[int, int]]:
    """A pipeline id whose derived keys are NEGATIVE, so masking is observable.

    ``pg_locks.classid``/``objid`` are uint32: a signed key is stored as a
    uint32 bit-cast, so a test that only ever used a positive key could not
    tell a masked parameter from an unmasked one. Deterministic (uuid5 over a
    fixed namespace), not random.
    """
    for i in range(10000):
        candidate = uuid.uuid5(uuid.NAMESPACE_URL, f"https://farnalabs.dev/snapshot-lock/{i}")
        keys = _pipeline_lock_keys(candidate)
        if keys[0] < 0 and keys[1] < 0:
            return (candidate, keys)
    raise AssertionError("no pipeline id with two negative lock keys in 10000 candidates")


def _holder_rows() -> list[dict[str, Any]]:
    return [
        {
            "pid": 4242,
            "application_name": "modulo",
            "state": "idle",
            "backend_start": datetime(2026, 1, 1, tzinfo=UTC),
            "query_start": datetime(2026, 1, 1, tzinfo=UTC),
            "granted": True,
        }
    ]


async def test_inspect_snapshot_lock_masks_keys_and_returns_holders() -> None:
    """The diagnostic derives the SAME keys the acquirer uses and binds them
    MASKED to the uint32 ``pg_locks`` domain — binding the signed value fails
    outright in Postgres ("value out of uint32 range")."""
    pipeline_id, (key1, key2) = _negative_key_pipeline_id()
    assert key1 < 0
    assert key2 < 0

    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.mappings.return_value.all.return_value = _holder_rows()
    session.execute.return_value = result

    status = await inspect_snapshot_lock(session, pipeline_id)

    assert status["held"] is True
    assert status["holders"] == _holder_rows()
    stmt, params = session.execute.call_args.args
    assert "pg_locks" in str(stmt)
    assert params == {"key1": key1 & 0xFFFFFFFF, "key2": key2 & 0xFFFFFFFF}
    assert params["key1"] > 0x7FFFFFFF  # uint32, not the signed key


async def test_inspect_snapshot_lock_reports_free_lock_as_not_held() -> None:
    pipeline_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    session.execute.return_value = result

    status = await inspect_snapshot_lock(session, pipeline_id)

    assert status["held"] is False
    assert not status["holders"]


async def test_terminate_without_holders_is_an_idempotent_no_op() -> None:
    """0 holders -> ``released: 0`` and success, and NOT ONE terminate statement
    is issued (the only execute is the holder read)."""
    pipeline_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result

    outcome = await terminate_snapshot_lock_holders(session, pipeline_id)

    assert outcome["released"] == 0
    assert not outcome["pids"]
    assert session.execute.await_count == 1


async def test_terminate_targets_only_backends_holding_the_derived_keys() -> None:
    """Every terminate carries a pid read from the derived-key query — granted
    rows only, never this session — and a backend whose terminate comes back
    false (it released/died first) is not counted as released."""
    pipeline_id, (key1, key2) = _negative_key_pipeline_id()

    pid_read = MagicMock()
    pid_read.scalars.return_value.all.return_value = [4242, 4243]
    terminated = MagicMock()
    terminated.scalar_one.return_value = True
    already_gone = MagicMock()
    already_gone.scalar_one.return_value = False

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [pid_read, terminated, already_gone]

    outcome = await terminate_snapshot_lock_holders(session, pipeline_id)

    assert outcome["released"] == 1
    assert outcome["pids"] == [4242]

    holder_read, *terminate_calls = session.execute.call_args_list
    assert holder_read.args[1] == {"key1": key1 & 0xFFFFFFFF, "key2": key2 & 0xFFFFFFFF}
    holder_sql = str(holder_read.args[0])
    assert "pg_locks" in holder_sql
    assert "l.granted" in holder_sql
    assert "pg_backend_pid()" in holder_sql
    assert [call.args[1]["pid"] for call in terminate_calls] == [4242, 4243]
    assert all("pg_terminate_backend" in str(call.args[0]) for call in terminate_calls)


async def test_terminate_signal_privilege_refusal_becomes_a_typed_error() -> None:
    """SQLSTATE 42501 (neither superuser nor ``pg_signal_backend``) surfaces as
    ``SnapshotLockTerminateDeniedError`` naming the grant, so the route can
    answer with a clear typed 403 instead of a generic 500."""
    pipeline_id = uuid.uuid4()
    pid_read = MagicMock()
    pid_read.scalars.return_value.all.return_value = [4242]

    class _InsufficientPrivilegeError(Exception):
        sqlstate = "42501"

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        pid_read,
        DBAPIError(
            "SELECT pg_terminate_backend(:pid)",
            {"pid": 4242},
            _InsufficientPrivilegeError("permission denied"),
        ),
    ]

    with pytest.raises(SnapshotLockTerminateDeniedError, match="pg_signal_backend") as excinfo:
        await terminate_snapshot_lock_holders(session, pipeline_id)

    assert excinfo.value.sqlstate == "42501"


async def test_terminate_non_privilege_db_error_propagates_unchanged() -> None:
    """A different failure (statement timeout, aborted transaction) is NOT
    re-labelled as a missing grant — it propagates for the normal error
    mapping."""
    pipeline_id = uuid.uuid4()
    pid_read = MagicMock()
    pid_read.scalars.return_value.all.return_value = [4242]

    class _StatementTimeoutError(Exception):
        sqlstate = "57014"

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        pid_read,
        DBAPIError("SELECT pg_terminate_backend(:pid)", {"pid": 4242}, _StatementTimeoutError("canceling statement")),
    ]

    with pytest.raises(DBAPIError) as excinfo:
        await terminate_snapshot_lock_holders(session, pipeline_id)

    assert not isinstance(excinfo.value, SnapshotLockTerminateDeniedError)


# ---------------------------------------------------------------------------
# FAR-1558: the pipeline's environment-profile binding is frozen onto the
# snapshot (the column dispatch already reads; it was simply never written).
# ---------------------------------------------------------------------------


def _binding_snapshot_setup(bound_profile_id: uuid.UUID | None) -> tuple[AsyncMock, uuid.UUID]:
    """Build an agent-less pipeline + edge and a session whose five graph-copy
    reads serve it (pipeline, edges, version max, guardrails, policy gates)."""
    pipeline_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = uuid.uuid4()
    pipeline.environment_profile_id = bound_profile_id
    pipeline.graph_nodes_json = [
        {"id": str(source_id), "agent_id": None, "connector_binding": None},
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),  # _load_pipeline_and_edges -> Pipeline
        _scalars_result([edge]),  # _load_pipeline_and_edges -> PipelineEdge
        _scalar_result(1),  # snapshot_version max -> version 2
        _scalars_result([]),  # guardrail rows (none bound)
        _scalars_result([]),  # policy gate rows (none bound)
    ]
    return session, pipeline_id


async def test_environment_profile_binding_is_frozen_onto_the_snapshot() -> None:
    """FAR-1558: a pipeline bound to an environment profile freezes that
    binding onto the new snapshot — the value dispatch reads at run start."""
    bound_profile_id = uuid.uuid4()
    session, pipeline_id = _binding_snapshot_setup(bound_profile_id)

    with _bind_lock_connection(session, _lock_attempt_result(True)):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.environment_profile_id == bound_profile_id


async def test_unbound_pipeline_freezes_a_null_binding() -> None:
    """NULL is the historical default and must stay byte-identical: an
    unbound pipeline freezes NULL, so dispatch keeps its default route."""
    session, pipeline_id = _binding_snapshot_setup(None)

    with _bind_lock_connection(session, _lock_attempt_result(True)):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.environment_profile_id is None
