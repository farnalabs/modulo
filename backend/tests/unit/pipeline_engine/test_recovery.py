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
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    GuardrailOverrideError,
    GuardrailOverrideRequiredError,
    NodeAlreadyCompletedError,
    NodeNotFoundInGraphError,
    RecoveryNotAllowedError,
    guardrail_override,
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
    node_telemetry_json: dict[str, Any] | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = _PIPELINE_ID
    run.snapshot_id = _SNAPSHOT_ID
    run.langgraph_thread_id = f"{_ORG_ID}:{_RUN_ID}"
    run.status = status
    run.outputs_json = outputs_json
    run.node_telemetry_json = node_telemetry_json
    return run


def _make_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.graph_json = _SAMPLE_GRAPH
    snap.id = _SNAPSHOT_ID
    snap.run_context_defaults = {}
    return snap


def _blocked_false_outcome() -> Any:
    from modulo.core.guardrails import GuardrailInterceptionOutcome

    return GuardrailInterceptionOutcome(payload={"foo": "bar"}, blocked=False)


def _mock_session() -> AsyncMock:
    """Return a contract-shaped AsyncSession mock for recovery tests."""
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction.return_value = True
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind.return_value = bind
    session.info = {}
    session.flush = AsyncMock()
    return session


def _stub_blob_reader(run: Any) -> Any:
    """FAR-583 B1 stub for the recovery paths' blob reads.

    ``recover_node`` reads the completed-marker check and the pre-mutation
    dicts through :func:`read_run_blobs` (new table + legacy
    fallback); the mocked sessions here have no schema, so the stub serves
    the run's PRE-state from the mock attrs — mirroring the pre-B1 ORM read
    the assertions were written against.
    """
    from modulo.db.crud.run_node_outputs import RunBlobs

    async def _read(_session: Any, *, run_id: Any, organisation_id: Any = None) -> Any:
        outputs = (
            run.outputs_json if isinstance(getattr(run, "outputs_json", None), dict) and run.outputs_json else None
        )
        telemetry = (
            run.node_telemetry_json
            if isinstance(getattr(run, "node_telemetry_json", None), dict) and run.node_telemetry_json
            else None
        )
        return RunBlobs(outputs=outputs, telemetry=telemetry, markers=None)

    return _read


def _capturing_store_write(captured: dict[str, Any]) -> Any:
    """FAR-583 B1: capture the primary store write's merged payloads (the
    former in-transaction ORM write the assertions read off the run)."""

    async def _write(session: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    return _write


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_node_with_valid_input():
    """Recover a failed manual node by providing new input data."""
    run = _make_run(status="failed", outputs_json={})
    session = _mock_session()

    with (
        patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run),
        # Two binding points consume the reader: the module-level import in
        # recover_node and the in-function import in _apply_recovery_markers.
        patch("modulo.core.pipeline_engine.recovery.read_run_blobs", _stub_blob_reader(run)),
        patch("modulo.db.crud.run_node_outputs.read_run_blobs", _stub_blob_reader(run)),
        patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit,
        patch("modulo.db.crud.run.write_run_outputs_from_run", _capturing_store_write(captured := {})),
    ):
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
    # FAR-583 B1: the markers land on the PRIMARY store write — the returned
    # run object no longer carries them on (dropped) legacy columns.
    assert captured["outputs"] is not None
    assert _NODE_ID in captured["outputs"]
    assert captured["outputs"][_NODE_ID] == {"review": "approved", "comments": "LGTM"}
    assert captured["telemetry"] is not None
    assert captured["telemetry"][_NODE_ID]["recovered"] is True
    assert captured["telemetry"][_NODE_ID]["recovery_input"] == {"review": "approved", "comments": "LGTM"}

    mock_audit.assert_awaited_once()
    audit_kwargs = mock_audit.await_args.kwargs
    assert audit_kwargs["event_type"] == "node.recovery"
    assert audit_kwargs["payload_json"]["recovery_action"] == "replay"
    # FAR-728: descriptive summary composed at the emit site; a user-initiated
    # recovery carries the acting user on the canonical actor column and does
    # NOT get a label key.
    assert audit_kwargs["payload_json"]["summary"] == f'Replay recovery applied to node "{_NODE_ID}"'
    assert audit_kwargs["actor_user_id"] == _ACTOR_ID
    assert "actor" not in audit_kwargs["payload_json"]


@pytest.mark.asyncio
async def test_skip_node_on_awaiting_human():
    """Skip a manual node by passing input_data=None."""
    run = _make_run(status="awaiting_human", outputs_json={})
    session = _mock_session()
    snap = _make_snapshot()

    with (
        patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.recovery.read_run_blobs", _stub_blob_reader(run)),
        patch("modulo.db.crud.run_node_outputs.read_run_blobs", _stub_blob_reader(run)),
        patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()),
        patch("modulo.db.crud.run.write_run_outputs_from_run", _capturing_store_write(captured := {})),
    ):
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
    # FAR-583 B1: the skip marker lands on the PRIMARY store write.
    assert captured["outputs"] is not None
    assert _NODE_ID not in captured["outputs"]
    assert captured["telemetry"] is not None
    assert captured["telemetry"][_NODE_ID]["skipped"] is True


@pytest.mark.asyncio
async def test_recovery_audit_without_actor_resolves_to_system():
    """A recovery with no acting user records the literal system actor (FAR-728)."""
    run = _make_run(status="failed", outputs_json={})
    session = _mock_session()

    with (
        patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run),
        # Two binding points consume the reader: the module-level import in
        # recover_node and the in-function import in _apply_recovery_markers
        # (same shape as test_recover_node_with_valid_input).
        patch("modulo.core.pipeline_engine.recovery.read_run_blobs", _stub_blob_reader(run)),
        patch("modulo.db.crud.run_node_outputs.read_run_blobs", _stub_blob_reader(run)),
        patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit,
        patch("modulo.db.crud.run.write_run_outputs_from_run", _capturing_store_write({})),
    ):
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

        await recover_node(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            node_id=_NODE_ID,
            input_data={"review": "approved"},
        )

    mock_audit.assert_awaited_once()
    audit_kwargs = mock_audit.await_args.kwargs
    assert audit_kwargs["payload_json"]["actor"] == "system"
    assert audit_kwargs["payload_json"]["summary"] == f'Replay recovery applied to node "{_NODE_ID}"'
    assert audit_kwargs["actor_user_id"] is None


@pytest.mark.asyncio
async def test_recover_node_not_found():
    """Recover on a non-existent run raises RecoveryNotAllowedError."""
    session = _mock_session()

    with (
        patch("modulo.core.pipeline_engine.recovery.get_run", return_value=None),
        pytest.raises(RecoveryNotAllowedError) as exc_info,
    ):
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
async def test_recover_node_refuses_guardrail_blocked_run():
    """A guardrail-blocked run (eval_failed / eval_blocked) must NOT be
    resurrected through the generic recovery path (MAJOR-1) — the generic path
    does not re-run the guardrail pass on the supplied input and would resume
    execution on the blocked payload. Only the guardrail-override endpoint may
    remediate it."""
    run = _make_run(status="eval_failed")
    run.error_code = "eval_blocked"
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        session.execute = AsyncMock(side_effect=[pipeline_result])

        with pytest.raises(GuardrailOverrideRequiredError) as exc_info:
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "guardrail-override" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_recover_node_eval_failed_non_blocked_still_rejected():
    """An eval_failed run with a NON-guardrail error_code is not recoverable
    via the generic path either (eval_failed is no longer a generic recoverable
    status) — it gets the plain RecoveryNotAllowedError."""
    run = _make_run(status="eval_failed")
    run.error_code = "eval.blocked"  # output-side eval block, not guardrail-blocked
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        session.execute = AsyncMock(side_effect=[pipeline_result])

        with pytest.raises(RecoveryNotAllowedError) as exc_info:
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "eval_failed" in str(exc_info.value)


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

        with (
            patch(
                "modulo.core.pipeline_engine.recovery.read_run_blobs",
                _stub_blob_reader(run),
            ),
            pytest.raises(NodeAlreadyCompletedError) as exc_info,
        ):
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "already completed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_recover_skipped_node_not_recoverable():
    """Recover on a node whose skip marker lives only in telemetry raises NodeAlreadyCompletedError."""
    run = _make_run(status="failed", outputs_json={}, node_telemetry_json={_NODE_ID: {"skipped": True}})
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

        with (
            patch(
                "modulo.core.pipeline_engine.recovery.read_run_blobs",
                _stub_blob_reader(run),
            ),
            pytest.raises(NodeAlreadyCompletedError) as exc_info,
        ):
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

        with (
            patch(
                "modulo.core.pipeline_engine.recovery.read_run_blobs",
                _stub_blob_reader(run),
            ),
            pytest.raises(ConcurrentRecoveryError) as exc_info,
        ):
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )

    assert "concurrent" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_recover_node_disappears_after_lock():
    """If the run vanishes after the pipeline lock is taken, raise not_found."""
    run = _make_run(status="failed")
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", side_effect=[run, None]):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        session.execute = AsyncMock(side_effect=[pipeline_result])

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
async def test_recover_node_missing_snapshot():
    """A run whose snapshot was deleted raises a hard RuntimeError."""
    run = _make_run(status="failed")
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        snapshot_result = MagicMock()
        snapshot_result.scalar_one_or_none.return_value = None

        session.execute = AsyncMock(
            side_effect=[
                pipeline_result,
                snapshot_result,
            ]
        )

        with pytest.raises(RuntimeError, match="Snapshot"):
            await recover_node(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                node_id=_NODE_ID,
                input_data={"foo": "bar"},
            )


@pytest.mark.asyncio
async def test_recover_node_audit_failure_is_logged_not_fatal():
    """An append_audit_event failure must not abort the recovery."""
    run = _make_run(status="failed", outputs_json={})
    session = _mock_session()
    snap = _make_snapshot()

    with (
        patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.recovery.read_run_blobs", _stub_blob_reader(run)),
        patch("modulo.db.crud.run_node_outputs.read_run_blobs", _stub_blob_reader(run)),
        patch(
            "modulo.core.pipeline_engine.recovery.append_audit_event",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch("modulo.db.crud.run.write_run_outputs_from_run", AsyncMock()),
    ):
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
            input_data={"review": "ok"},
            actor_id=_ACTOR_ID,
        )

    assert result is not None
    assert result.status == "running"


@pytest.mark.asyncio
async def test_guardrail_override_run_not_found():
    """guardrail_override on a missing run raises GuardrailOverrideError."""
    session = _mock_session()

    with (
        patch("modulo.core.pipeline_engine.recovery.get_run", return_value=None),
        pytest.raises(GuardrailOverrideError) as exc_info,
    ):
        await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"foo": "bar"},
            actor_id=_ACTOR_ID,
        )

    assert "not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_guardrail_override_disappears_after_lock():
    """If the run vanishes after the pipeline lock, raise GuardrailOverrideError."""
    run = _make_run(status="eval_failed")
    run.error_code = "eval_blocked"
    session = _mock_session()

    with patch("modulo.core.pipeline_engine.recovery.get_run", side_effect=[run, None]):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        session.execute = AsyncMock(side_effect=[pipeline_result])

        with pytest.raises(GuardrailOverrideError) as exc_info:
            await guardrail_override(
                session,
                org_id=_ORG_ID,
                run_id=_RUN_ID,
                input_data={"foo": "bar"},
                actor_id=_ACTOR_ID,
            )

    assert "not found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_guardrail_override_audit_failure_is_logged_not_fatal():
    """An append_audit_event failure must not abort the override."""
    run = _make_run(status="eval_failed")
    run.error_code = "eval_blocked"
    session = _mock_session()

    with (
        patch("modulo.core.pipeline_engine.recovery.get_run", return_value=run),
        patch(
            "modulo.core.pipeline_engine.recovery.append_audit_event",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch(
            "modulo.core.guardrails.run_interception_pass_async",
            AsyncMock(return_value=_blocked_false_outcome()),
        ),
        patch("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows", return_value=[]),
    ):
        pipeline_result = MagicMock()
        pipeline_result.scalar_one.return_value = MagicMock()
        locked_result = MagicMock()
        locked_result.scalar_one_or_none.return_value = _RUN_ID

        session.execute = AsyncMock(
            side_effect=[
                pipeline_result,
                locked_result,
            ]
        )

        result = await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"foo": "bar"},
            actor_id=_ACTOR_ID,
        )

    assert result is not None
    assert result.status == "pending"
