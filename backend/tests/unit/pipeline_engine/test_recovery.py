"""Unit tests for manual-input node recovery.

Tests that ``recover_node``:
- Allows recovery on a failed run with valid input
- Allows skip (input_data=None) on an awaiting_human run
- Rejects recovery on an already-completed node (409)
- Rejects recovery on a terminal-status run (complete)
- Rejects recovery when the node does not exist in the graph
- Uses pipeline-level FOR UPDATE to prevent concurrent races
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    NodeAlreadyCompletedError,
    NodeNotFoundInGraphError,
    RecoveryNotAllowedError,
    recover_node,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_NODE_ID = "manual-node-1"
_ACTOR_ID = uuid.uuid4()

_SAMPLE_GRAPH: dict[str, Any] = {
    "nodes": [
        {
            "id": _NODE_ID,
            "node_type": "manual",
            "manual_prompt": "Enter review result",
        },
        {
            "id": "next-node",
            "node_type": "agent",
        },
    ],
    "edges": [
        {"source": _NODE_ID, "target": "next-node", "type": "normal"},
    ],
}


def _make_run(
    *,
    status: str = "failed",
    outputs_json: dict[str, Any] | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = _PIPELINE_ID
    run.snapshot_id = _SNAPSHOT_ID
    run.langgraph_thread_id = f"{_ORG_ID}:{_RUN_ID}"
    run.status = status
    run.outputs_json = outputs_json
    return run


def _make_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.graph_json = _SAMPLE_GRAPH
    snap.id = _SNAPSHOT_ID
    snap.run_context_defaults = {}
    return snap


def _mock_session() -> AsyncMock:
    """A bare AsyncSession mock that provides begin/flush/execute stubs."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    session.flush = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_node_with_valid_input():
    """Recover a failed manual node by providing new input data."""
    run = _make_run(status="failed", outputs_json={})
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        with patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit:
            pipeline_result = MagicMock()
            pipeline_result.scalar_one.return_value = MagicMock()
            snapshot_result = MagicMock()
            snapshot_result.scalar_one_or_none.return_value = _make_snapshot()
            locked_result = MagicMock()
            locked_result.scalar_one_or_none.return_value = _RUN_ID

            session.execute = AsyncMock(
                side_effect=[
                    pipeline_result,  # Pipeline lock
                    snapshot_result,  # Snapshot query
                    locked_result,  # Update RUN ... RETURNING
                ]
            )

            result = await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"review": "approved", "comments": "LGTM"},
                actor_id=_ACTOR_ID,
            )

    assert result is not None
    assert result.status == "running"
    assert result.outputs_json is not None
    assert _NODE_ID in result.outputs_json
    assert result.outputs_json[_NODE_ID]["recovered"] is True
    assert result.outputs_json[_NODE_ID]["input"] == {"review": "approved", "comments": "LGTM"}

    mock_audit.assert_awaited_once()
    audit_kwargs = mock_audit.await_args.kwargs
    assert audit_kwargs["event_type"] == "node.recovery"
    assert audit_kwargs["payload_json"]["recovery_action"] == "replay"


@pytest.mark.asyncio
async def test_skip_node_on_awaiting_human():
    """Skip a manual node by passing input_data=None."""
    run = _make_run(status="awaiting_human", outputs_json={})
    session = _mock_session()
    snap = _make_snapshot()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        with patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()):
            pipeline_result = MagicMock()
            pipeline_result.scalar_one.return_value = MagicMock()
            snapshot_result = MagicMock()
            snapshot_result.scalar_one_or_none.return_value = snap
            locked_result = MagicMock()
            locked_result.scalar_one_or_none.return_value = _RUN_ID

            session.execute = AsyncMock(
                side_effect=[
                    pipeline_result,
                    snapshot_result,
                    locked_result,
                ]
            )

            result = await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data=None,
                actor_id=_ACTOR_ID,
            )

    assert result is not None
    assert result.status == "running"
    assert _NODE_ID in result.outputs_json
    assert result.outputs_json[_NODE_ID]["skipped"] is True
    assert result.outputs_json[_NODE_ID]["output"] is None


@pytest.mark.asyncio
async def test_recover_node_not_found():
    """Recover on a non-existent run raises RecoveryNotAllowedError."""
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=None):
        with pytest.raises(RecoveryNotAllowedError) as exc_info:
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "not_found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_recover_node_terminal_status():
    """Recover on a completed run raises RecoveryNotAllowedError."""
    run = _make_run(status="complete")
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        # get_run after lock returns same run
        session.execute = AsyncMock(side_effect=[pipeline_result])

        with pytest.raises(RecoveryNotAllowedError) as exc_info:
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "complete" in str(exc_info.value)


@pytest.mark.asyncio
async def test_recover_nonexistent_node():
    """Recover on a node that doesn't exist in the graph raises NodeNotFoundInGraphError."""
    run = _make_run(status="failed")
    snap = _make_snapshot()
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        snapshot_result = MagicMock()
        snapshot_result.scalar_one_or_none.return_value = snap

        session.execute = AsyncMock(
            side_effect=[
                pipeline_result,
                snapshot_result,
            ]
        )

        with pytest.raises(NodeNotFoundInGraphError) as exc_info:
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id="nonexistent-node",
                input_data={"foo": "bar"},
            )

    assert "nonexistent-node" in str(exc_info.value)


@pytest.mark.asyncio
async def test_recover_already_completed_node():
    """Recover on a node that already has output raises NodeAlreadyCompletedError."""
    run = _make_run(status="failed", outputs_json={_NODE_ID: {"output": "already done"}})
    snap = _make_snapshot()
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        snapshot_result = MagicMock()
        snapshot_result.scalar_one_or_none.return_value = snap

        session.execute = AsyncMock(
            side_effect=[
                pipeline_result,
                snapshot_result,
            ]
        )

        with pytest.raises(NodeAlreadyCompletedError) as exc_info:
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "already completed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_concurrent_recovery_race():
    """When another recovery wins the UPDATE race, the loser gets an error."""
    run = _make_run(status="failed", outputs_json={})
    snap = _make_snapshot()
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        snapshot_result = MagicMock()
        snapshot_result.scalar_one_or_none.return_value = snap
        locked_result = MagicMock()
        locked_result.scalar_one_or_none.return_value = None  # No row updated — race lost

        session.execute = AsyncMock(
            side_effect=[
                pipeline_result,
                snapshot_result,
                locked_result,
            ]
        )

        with pytest.raises(ConcurrentRecoveryError) as exc_info:
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "concurrent" in str(exc_info.value).lower()
