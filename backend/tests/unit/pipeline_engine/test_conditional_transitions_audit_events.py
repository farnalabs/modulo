"""Unit tests for block failure audit events in PipelineExecutor."""

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalBlockedError, EvalSuiteBlockedError
from modulo.core.pipeline_engine.executor import PipelineExecutor

# ---------------------------------------------------------------------------
# Helpers (matching test_executor.py patterns)
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    status: str = "pending",
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.pipeline_id = pipeline_id or uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = status
    return run


def _make_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.graph_json = {"nodes": [{"id": "node-a", "role": None}], "edges": []}
    snap.run_context_defaults = {}
    snap.default_autonomy_level = None
    return snap


def _make_pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 30
    pipeline.max_duration_seconds = None
    pipeline.max_steps = None
    pipeline.token_budget = None
    return pipeline


def _make_session(snapshot: MagicMock) -> AsyncMock:
    pipeline = _make_pipeline()

    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = pipeline

    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot

    eval_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    eval_result.scalars.return_value = scalars_mock

    count_result = MagicMock()
    count_result.scalar.return_value = 0

    execute_results = iter([pipeline_result, snapshot_result, eval_result, count_result])

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        try:
            return next(execute_results)
        except StopIteration:
            return count_result

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = _execute
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _make_registry() -> MagicMock:
    broker = MagicMock()
    broker.publish = MagicMock()
    broker.is_closed = False
    registry = MagicMock()
    registry.get_or_create.return_value = broker
    registry.close = MagicMock()
    return registry


def _mock_graph_validator() -> MagicMock:
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    return mock_cls


def _make_compiled() -> MagicMock:
    c = MagicMock()
    c.astream_events = MagicMock()
    return c


def _make_compiled_raising(exc: Exception) -> MagicMock:
    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        raise exc
        yield  # pragma: no cover

    async def _aupdate_state(config: Any, data: Any) -> None:
        return None

    c = MagicMock()
    c.astream_events = _astream
    c.aupdate_state = _aupdate_state
    return c


def _bypass_capacity(self: Any, **kwargs: Any) -> MagicMock:
    run = MagicMock()
    run.status = "running"
    run.id = kwargs.get("run_id", uuid.uuid4())
    return AsyncMock(return_value=run)()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("modulo.core.pipeline_engine.executor.append_audit_event", new_callable=AsyncMock)
async def test_eval_blocked_records_audit_event(mock_append: AsyncMock):
    """EvalBlockedError from _stream_graph triggers an audit event."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="eval_failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _make_compiled_raising(EvalBlockedError("quality", "Low score"))
    registry = _make_registry()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    mock_append.assert_called_once()
    call_kwargs = mock_append.call_args[1]
    assert call_kwargs["event_type"] == "eval.blocked"
    assert call_kwargs["resource_type"] == "run"


@pytest.mark.asyncio
@patch("modulo.core.pipeline_engine.executor.append_audit_event", new_callable=AsyncMock)
async def test_eval_suite_blocked_records_audit_event(mock_append: AsyncMock):
    """EvalSuiteBlockedError from post-run suite check triggers an audit event."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _make_compiled()
    registry = _make_registry()
    suite_blocked = EvalSuiteBlockedError("suite-1", 0.3, 0.8)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch.object(PipelineExecutor, "_check_eval_suites", AsyncMock(side_effect=suite_blocked)),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    mock_append.assert_called_once()
    call_kwargs = mock_append.call_args[1]
    assert call_kwargs["event_type"] == "eval.suite_blocked"
    assert call_kwargs["resource_type"] == "run"
    assert call_kwargs["payload_json"]["suite_id"] == "suite-1"


@pytest.mark.asyncio
@patch("modulo.core.pipeline_engine.executor.append_audit_event", new_callable=AsyncMock)
async def test_resume_eval_blocked_records_audit_event(mock_append: AsyncMock):
    """EvalBlockedError from _stream_graph during resume triggers an audit event."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="eval_failed")
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _make_compiled_raising(EvalBlockedError("quality", "Low score"))
    registry = _make_registry()

    checkpointer_mock = MagicMock()
    checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
    checkpointer_mock.__aexit__ = AsyncMock(return_value=False)

    settings_mock = MagicMock()
    settings_mock.fernet_key = "test-fernet-key-not-for-production="

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    mock_append.assert_called_once()
    call_kwargs = mock_append.call_args[1]
    assert call_kwargs["event_type"] == "eval.blocked"
    assert call_kwargs["resource_type"] == "run"
