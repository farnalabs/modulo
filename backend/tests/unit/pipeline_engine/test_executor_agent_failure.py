"""Unit tests for A1 elevation (agent-failure UX, phase 1).

When a captured sandbox-agent node output self-reports failure
(``agent_status=failed`` OR ``outcome=failed``), ``_stream_graph`` must
terminalize the run as ``failed`` with error_code ``agent.failed`` — NEVER
``complete`` — regardless of the exit code. The elevation is flag-gated
(``modulo_agent_failure_elevation_enabled``) and fail-open: any computation
error logs and falls back to today's path.
"""

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from modulo.core.pipeline_engine.error_codes import map_legacy_code
from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    _node_output_agent_failure,
    _retry_after_policy,
)


def _agent_failed_event(
    *,
    agent_status: str = "failed",
    agent_outcome: str | None = None,
    summary: str = "agent failed",
    node_status: str = "completed",
) -> dict[str, Any]:
    inner: dict[str, Any] = {"status": node_status, "summary": summary, "agent_status": agent_status}
    if agent_outcome is not None:
        inner["agent_outcome"] = agent_outcome
    return {
        "event": "on_chain_end",
        "name": "node-a",
        "data": {"output": {"output": inner}},
    }


def _complete_event() -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "node-a",
        "data": {
            "output": {
                "output": {
                    "status": "completed",
                    "summary": "all good",
                    "agent_status": "completed",
                    "agent_outcome": "success",
                }
            }
        },
    }


def _stalled_event(stall_reason: str = "went silent") -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "node-a",
        "data": {"output": {"output": {"status": "failed", "stall_reason": stall_reason}}},
    }


def _mock_compiled(events: list[dict[str, Any]]) -> MagicMock:
    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        for e in events:
            yield e

    c = MagicMock()
    c.astream_events = _astream
    return c


class _FakeSettings:
    modulo_agent_failure_elevation_enabled = True


@pytest.fixture
def elevation_enabled(monkeypatch):
    fake = _FakeSettings()
    fake.modulo_agent_failure_elevation_enabled = True
    monkeypatch.setattr("modulo.settings.get_settings", lambda _fresh=False: fake)
    return fake


@pytest.fixture
def elevation_disabled(monkeypatch):
    fake = _FakeSettings()
    fake.modulo_agent_failure_elevation_enabled = False
    monkeypatch.setattr("modulo.settings.get_settings", lambda _fresh=False: fake)
    return fake


async def _run_stream_graph(
    events: list[dict[str, Any]], broker: MagicMock | None = None
) -> tuple[tuple[Any, ...], MagicMock]:
    executor = PipelineExecutor(MagicMock())
    completed_node_outputs: dict[str, Any] = {}
    broker = broker or MagicMock()
    result = await executor._stream_graph(
        _mock_compiled(events),
        None,
        {"configurable": {"thread_id": str(uuid.uuid4())}},
        {"node-a"},
        broker,
        uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        completed_node_outputs=completed_node_outputs,
    )
    return result, broker


def _run_failed_publishes(broker: MagicMock) -> list[tuple[Any, ...]]:
    return [c.args for c in broker.publish.call_args_list if c.args and c.args[0] == "run_failed"]


# ---------------------------------------------------------------------------
# _node_output_agent_failure helper
# ---------------------------------------------------------------------------


def test_node_output_agent_failure_detects_agent_status_failed():
    reason = _node_output_agent_failure(
        {"output": {"status": "completed", "agent_status": "failed", "summary": "all sub-calls timed out"}}
    )
    assert reason == "all sub-calls timed out"


def test_node_output_agent_failure_detects_outcome_failed():
    reason = _node_output_agent_failure({"output": {"status": "completed", "agent_outcome": "failed"}})
    assert reason == "agent self-reported failure"


def test_node_output_agent_failure_ignores_non_failed_and_garbage():
    assert _node_output_agent_failure({"output": {"status": "completed", "agent_status": "completed"}}) is None
    assert _node_output_agent_failure({"output": {"agent_status": "running"}}) is None
    assert _node_output_agent_failure({"output": {}}) is None
    assert _node_output_agent_failure("not-a-dict") is None
    assert _node_output_agent_failure(None) is None


# ---------------------------------------------------------------------------
# A1 elevation in _stream_graph
# ---------------------------------------------------------------------------


async def test_agent_status_failed_exit_zero_elevates_to_run_failed(elevation_enabled):
    """agent_status=failed with a completed (exit 0) node → run failed/agent.failed."""
    result, broker = await _run_stream_graph([_agent_failed_event()])

    final_status, error_code, error_detail, _node_token_usage = result
    assert final_status == "failed"
    assert error_code == "agent.failed"
    assert error_detail == "agent failed"
    assert ("run_failed", {"error": "agent.failed", "detail": "agent failed"}) in _run_failed_publishes(broker)


async def test_outcome_failed_nonzero_exit_elevates(elevation_enabled):
    """outcome=failed with a failed (exit != 0) node → elevation still fires."""
    result, _broker = await _run_stream_graph(
        [_agent_failed_event(agent_status="completed", agent_outcome="failed", node_status="failed")]
    )

    final_status, error_code, _error_detail, _node_token_usage = result
    assert final_status == "failed"
    assert error_code == "agent.failed"


async def test_agent_status_failed_nonzero_exit_elevates(elevation_enabled):
    """agent_status=failed fires regardless of the node's exit code (§13.6)."""
    result, _broker = await _run_stream_graph([_agent_failed_event(node_status="failed")])

    assert result[0] == "failed"
    assert result[1] == "agent.failed"


async def test_elevation_disabled_run_completes_as_today(elevation_disabled):
    """Flag off → no elevation; the run completes exactly as today."""
    result, broker = await _run_stream_graph([_agent_failed_event()])

    final_status, error_code, error_detail, _node_token_usage = result
    assert final_status == "complete"
    assert error_code is None
    assert error_detail is None
    assert _run_failed_publishes(broker) == []
    assert ("run_completed", {}) in [c.args for c in broker.publish.call_args_list]


async def test_no_agent_failure_signal_no_elevation(elevation_enabled):
    """A cleanly completed node keeps the old complete path."""
    result, broker = await _run_stream_graph([_complete_event()])

    assert result[0] == "complete"
    assert result[1] is None
    assert _run_failed_publishes(broker) == []


async def test_stall_wins_over_agent_failure(elevation_enabled):
    """A node that both stalled AND self-reported failure lands 'stalled' —
    the existing stall terminalization is preserved."""
    event = {
        "event": "on_chain_end",
        "name": "node-a",
        "data": {"output": {"output": {"status": "failed", "stall_reason": "went silent", "agent_status": "failed"}}},
    }
    result, broker = await _run_stream_graph([event])

    assert result[0] == "stalled"
    assert result[1] == "executor_stalled"
    assert ("run_failed", {"error": "executor_stalled", "detail": "went silent"}) in _run_failed_publishes(broker)


async def test_elevation_fail_open_on_settings_error(monkeypatch):
    """An exception inside the elevation computation falls back to today's path
    (fail-open) — the run must never crash on elevation."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("settings exploded")

    monkeypatch.setattr("modulo.settings.get_settings", _boom)
    result, broker = await _run_stream_graph([_agent_failed_event()])

    assert result[0] == "complete"
    assert result[1] is None
    assert _run_failed_publishes(broker) == []


# ---------------------------------------------------------------------------
# _retry_after_policy — dotted codes + legacy aliases
# ---------------------------------------------------------------------------


def test_retry_after_policy_failure_matches_agent_failed():
    """An A1 elevation lands status 'failed' with code 'agent.failed' — a
    {'on': ['failure']} policy must retry it."""
    assert _retry_after_policy({"on": ["failure"], "max_retries": 2}, "failed", "agent.failed") == 2


def test_retry_after_policy_timeout_matches_dotted_and_legacy_codes():
    """'timeout' matches node.timeout and node.runaway whether given as dotted
    codes or legacy aliases."""
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 2}, "failed", "node.timeout") == 2
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 2}, "failed", "node_timeout") == 2
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 2}, "failed", "TimeoutError") == 2
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 2}, "failed", "runaway") == 2
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 2}, "failed", "node.runaway") == 2


def test_retry_after_policy_stall_matches_dotted_agent_stall():
    assert _retry_after_policy({"on": ["stall"], "max_retries": 2}, "failed", "agent.stall") == 2
    assert _retry_after_policy({"on": ["stall"], "max_retries": 2}, "stalled", "agent.stall") == 2


def test_retry_after_policy_failure_excludes_agent_stall():
    """A stall mapped to agent.stall is never retried as a generic failure."""
    assert _retry_after_policy({"on": ["failure"], "max_retries": 2}, "failed", "agent.stall") is None


def test_retry_after_policy_legacy_behaviour_unchanged():
    """All pre-existing legacy behaviours remain intact (regression guard)."""
    assert _retry_after_policy({"on": ["stall"], "max_retries": 2}, "stalled", "executor_stalled") == 2
    assert _retry_after_policy({"on": ["stall"], "max_retries": 2}, "failed", "node_timeout") is None
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 1}, "failed", "node_timeout") == 1
    assert _retry_after_policy({"on": ["timeout"], "max_retries": 1}, "failed", "TimeoutError") == 1
    assert _retry_after_policy({"on": ["failure"], "max_retries": 1}, "failed", "some_other_error") == 1
    assert _retry_after_policy({"on": ["failure"], "max_retries": 3}, "failed", "node_timeout") is None
    assert _retry_after_policy({}, "stalled", "executor_stalled") is None


# ---------------------------------------------------------------------------
# work_intact interaction with A1 elevation (FAR-152 §15.4)
# ---------------------------------------------------------------------------


def _executor_instance() -> PipelineExecutor:
    return PipelineExecutor(MagicMock())


def test_work_intact_false_when_elevated_to_agent_failed():
    """An A1-elevated run (failed + agent.failed) is NOT complete — the honest
    work verdict is False, so the zero-work elevation banner renders (§15.4)."""
    executor = _executor_instance()
    work_intact = executor._compute_run_work_intact(
        "failed",
        "agent.failed",
        {"node-a": {"output": {"status": "completed", "summary": "groomed 0/5"}}},
        {"node-a"},
    )
    assert work_intact is False


def test_work_intact_computed_for_harness_failure_with_full_dag():
    """A harness crash after a completed node with the full DAG ran keeps
    work_intact True — restores the false-failure banner for #1/#3."""
    executor = _executor_instance()
    work_intact = executor._compute_run_work_intact(
        "failed",
        "harness.db.connection_lost",
        {"node-a": {"output": {"status": "completed", "summary": "all good"}}},
        {"node-a"},
    )
    assert work_intact is True


def test_work_intact_none_for_non_terminal():
    executor = _executor_instance()
    assert (
        executor._compute_run_work_intact(
            "awaiting_human", None, {"node-a": {"output": {"status": "completed"}}}, {"node-a"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_legacy_aliases_resolve_through_error_codes_module():
    assert map_legacy_code("executor_stalled") == "agent.stall"
    assert map_legacy_code("output_rejected") == "contract.schema"
    assert map_legacy_code("unknown_thing") == "harness.unknown"
