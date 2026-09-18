"""Unit tests for the FAR-305 standalone post-node eval path.

Node-scoped evals must fire after EVERY completed node in ``_stream_graph``,
independent of HITL gates — so a user can attach a pure output-validation eval
to a plain node. This exercises ``PipelineExecutor._stream_graph`` directly
with a mock compiled graph, mirroring the executor's real event stream.

Covered:
  - json_schema eval passes -> run completes, result persisted passed=True.
  - json_schema eval fails with failure_behaviour="block" -> EvalBlockedError
    propagates, run transitions to eval_failed / error_code eval_blocked.
  - node with NO eval definitions -> no eval runs, no error.
  - warn eval fails -> warning logged, run continues normally.
  - persistence writes to eval_results with correct org/run/node/eval ids.
"""

import uuid
from contextlib import asynccontextmanager
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.pipeline_engine.executor import PipelineExecutor


def _node_event(inner: dict[str, Any], name: str = "node-a") -> dict[str, Any]:
    """An ``on_chain_end`` event carrying the node envelope ``{"output": ...}``."""
    return {
        "event": "on_chain_end",
        "name": name,
        "data": {"output": {"output": inner}},
    }


def _mock_compiled(events: list[dict[str, Any]]) -> MagicMock:
    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        for e in events:
            yield e

    c = MagicMock()
    c.astream_events = _astream
    return c


class _RecordingSession:
    """Fake async session that records ``EvalResultModel`` rows added."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    def add(self, obj: Any) -> None:
        self.added.append(obj)


ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
EVAL_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
NODE_UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d4")

PASSING_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}


def _make_executor(
    session: _RecordingSession | None = None, monkeypatch: pytest.MonkeyPatch | None = None
) -> tuple[PipelineExecutor, _RecordingSession | None]:
    executor = PipelineExecutor(MagicMock())
    if session is not None and monkeypatch is not None:

        @asynccontextmanager
        async def _fake_factory():
            yield session

        executor._session_factory = _fake_factory
        monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_org", AsyncMock())
        monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock())
    return executor, session


async def _run_stream_graph(
    events: list[dict[str, Any]],
    eval_definitions_by_node: dict[str, list[EvalDefinition]] | None,
    executor: PipelineExecutor,
) -> tuple[tuple[Any, ...], MagicMock]:
    broker = MagicMock()
    completed_node_outputs: dict[str, Any] = {}
    result = await executor._stream_graph(
        _mock_compiled(events),
        None,
        {"configurable": {"thread_id": str(uuid.uuid4())}},
        {"node-a"},
        broker,
        RUN_ID,
        pipeline_id=uuid.uuid4(),
        org_id=ORG_ID,
        completed_node_outputs=completed_node_outputs,
        eval_definitions_by_node=eval_definitions_by_node,
    )
    return result, broker


def _json_schema_eval_def(*, failure_behaviour: str = "warn", node_id: str | None = str(NODE_UUID)) -> EvalDefinition:
    return EvalDefinition(
        id=EVAL_ID,
        org_id=ORG_ID,
        node_id=node_id,
        name="schema-check",
        eval_type=EvalType.JSON_SCHEMA,
        config={"schema": PASSING_SCHEMA},
        failure_behaviour=failure_behaviour,
    )


async def test_passing_json_schema_eval_completes_and_persists(monkeypatch: pytest.MonkeyPatch):
    """A passing json_schema eval -> run completes; result persisted passed=True."""
    from modulo.db.models.eval_result import EvalResult as EvalResultModel

    session = _RecordingSession()
    executor, _ = _make_executor(session, monkeypatch)
    eval_def = _json_schema_eval_def()
    event = _node_event({"summary": "all good"})

    result, broker = await _run_stream_graph(
        [event],
        {"node-a": [eval_def]},
        executor,
    )

    final_status, error_code, _error_detail, _node_token_usage = result
    assert final_status == "complete"
    assert error_code is None
    assert ("run_completed", {}) in [c.args for c in broker.publish.call_args_list]

    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, EvalResultModel)
    assert row.organisation_id == ORG_ID
    assert row.run_id == RUN_ID
    assert row.node_id == NODE_UUID
    assert row.eval_id == EVAL_ID
    assert row.passed is True
    assert row.score == 1.0


async def test_blocking_json_schema_eval_failure_transitions_to_eval_failed(monkeypatch: pytest.MonkeyPatch):
    """A failing json_schema eval with failure_behaviour=block -> EvalBlockedError
    propagates and the run transitions to eval_failed / eval_blocked."""
    executor, _ = _make_executor(monkeypatch=monkeypatch)
    eval_def = _json_schema_eval_def(failure_behaviour="block")
    # "summary" is missing -> schema validation fails.
    event = _node_event({"other": "nope"})

    result, broker = await _run_stream_graph(
        [event],
        {"node-a": [eval_def]},
        executor,
    )

    final_status, error_code, error_detail, _node_token_usage = result
    assert final_status == "eval_failed"
    assert error_code == "eval_blocked"
    assert error_detail is not None
    assert ("run_failed", {"error": "eval_blocked", "detail": error_detail}) in [
        c.args for c in broker.publish.call_args_list
    ]


async def test_no_eval_definitions_runs_no_evals_and_completes(monkeypatch: pytest.MonkeyPatch):
    """A node with NO eval definitions -> no eval runs, no error, run completes."""
    executor, _ = _make_executor(monkeypatch=monkeypatch)
    event = _node_event({"summary": "all good"})

    result, broker = await _run_stream_graph([event], None, executor)

    final_status, error_code, _error_detail, _node_token_usage = result
    assert final_status == "complete"
    assert error_code is None
    assert ("run_completed", {}) in [c.args for c in broker.publish.call_args_list]


async def test_no_eval_definitions_for_this_node_skips_evals(monkeypatch: pytest.MonkeyPatch):
    """A node NOT present in the eval map -> no eval runs, no error."""
    executor, _ = _make_executor(monkeypatch=monkeypatch)
    event = _node_event({"summary": "all good"})

    # node-a completes, but evals are only defined for node-zz (not executed).
    result, _broker = await _run_stream_graph(
        [event],
        {"node-zz": [_json_schema_eval_def()]},
        executor,
    )

    assert result[0] == "complete"
    assert result[1] is None


async def test_warn_eval_failure_logs_warning_and_run_continues(monkeypatch: pytest.MonkeyPatch, caplog):
    """A failing warn eval -> warning logged, run continues normally (complete)."""
    executor, _ = _make_executor(monkeypatch=monkeypatch)
    eval_def = _json_schema_eval_def(failure_behaviour="warn")
    event = _node_event({"other": "nope"})

    with caplog.at_level("WARNING", logger="modulo.core.pipeline_engine.executor"):
        result, broker = await _run_stream_graph(
            [event],
            {"node-a": [eval_def]},
            executor,
        )

    final_status, error_code, _error_detail, _node_token_usage = result
    assert final_status == "complete"
    assert error_code is None
    assert ("run_completed", {}) in [c.args for c in broker.publish.call_args_list]
    # The eval engine logs the warn-level failure; the post-node path persists a
    # failed row but does NOT block the run.
    assert any("JSON Schema validation failed" in r.message for r in caplog.records)


async def test_persistence_writes_correct_org_run_node_eval_ids(monkeypatch: pytest.MonkeyPatch):
    """Eval results are written to eval_results with correct org/run/node/eval ids."""
    from modulo.db.models.eval_result import EvalResult as EvalResultModel

    session = _RecordingSession()
    executor, _ = _make_executor(session, monkeypatch)
    eval_def = _json_schema_eval_def()
    event = _node_event({"summary": "ok"})

    await _run_stream_graph([event], {"node-a": [eval_def]}, executor)

    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, EvalResultModel)
    assert row.organisation_id == ORG_ID
    assert row.run_id == RUN_ID
    assert row.node_id == NODE_UUID
    assert row.eval_id == EVAL_ID
