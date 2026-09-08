"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.pipeline_engine.executor`` (PR #97).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_engine import executor as ex


def _executor() -> MagicMock:
    return ex.PipelineExecutor(MagicMock())


async def test_org_sandbox_capacity_free_get_run_failure() -> None:
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    with (
        patch.object(ex, "get_run", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ex, "_log") as log,
    ):
        result = await ex.org_sandbox_capacity_free(MagicMock(), org_id, run_id)
    assert result is True
    log.warning.assert_called_once_with(
        "hitl.sandbox_capacity_check_failed",
        extra={"org_id": str(org_id), "run_id": str(run_id)},
        exc_info=True,
    )


async def test_claim_run_and_audit_append_failure() -> None:
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    with (
        patch.object(ex, "update_run_status", new=AsyncMock()),
        patch.object(ex, "get_run", new=AsyncMock(return_value=MagicMock())),
        patch.object(ex, "get_pipeline", new=AsyncMock(return_value=MagicMock(name="p"))),
        patch.object(ex, "append_audit_event", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ex, "_log") as log,
    ):
        await _executor()._claim_run_and_audit(
            session=MagicMock(), run_id=run_id, org_id=org_id, pipeline_id=pipeline_id
        )
    log.warning.assert_called_once_with(
        "pipeline.run_started_audit_failed",
        extra={"run_id": str(run_id), "org_id": str(org_id)},
        exc_info=True,
    )


async def test_org_sandbox_active_count_failure() -> None:
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    with (
        patch.object(ex, "count_active_sandbox_runs_for_org", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ex, "_log") as log,
    ):
        result = await _executor()._org_sandbox_active_count(MagicMock(), org_id, run_id)
    assert result == 0
    log.warning.assert_called_once_with(
        "pipeline.sandbox_org_count_failed",
        extra={"org_id": str(org_id), "run_id": str(run_id)},
        exc_info=True,
    )


async def test_org_run_active_count_failure() -> None:
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    with (
        patch.object(ex, "count_active_runs_for_org", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ex, "_log") as log,
    ):
        result = await _executor()._org_run_active_count(MagicMock(), org_id, run_id)
    assert result == 0
    log.warning.assert_called_once_with(
        "pipeline.org_run_count_failed",
        extra={"org_id": str(org_id), "run_id": str(run_id)},
        exc_info=True,
    )


async def test_read_org_sandbox_cap_graph_scan_failure() -> None:
    org_id = uuid.uuid4()
    with (
        patch.object(ex, "_graph_contains_sandbox_agent", side_effect=RuntimeError("boom")),
        patch.object(ex, "_log") as log,
    ):
        result = await _executor()._read_org_sandbox_cap(org_id, {"nodes": []}, None)
    assert result is None
    log.warning.assert_called_once_with(
        "pipeline.sandbox_graph_scan_failed",
        extra={"org_id": str(org_id)},
        exc_info=True,
    )


async def test_read_org_sandbox_cap_read_failure() -> None:
    org_id = uuid.uuid4()
    with (
        patch.object(ex, "_graph_contains_sandbox_agent", return_value=True),
        patch.object(ex, "get_sandbox_concurrency_limit", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ex, "_log") as log,
    ):
        result = await _executor()._read_org_sandbox_cap(org_id, {"nodes": []}, None)
    assert result is None
    log.warning.assert_called_once_with(
        "pipeline.sandbox_cap_read_failed",
        extra={"org_id": str(org_id)},
        exc_info=True,
    )


async def test_read_org_run_concurrency_limit_failure() -> None:
    org_id = uuid.uuid4()
    with (
        patch.object(ex, "get_org_run_concurrency_limit", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ex, "_log") as log,
    ):
        result = await _executor()._read_org_run_concurrency_limit(org_id)
    assert result is None
    log.warning.assert_called_once_with(
        "pipeline.org_run_cap_read_failed",
        extra={"org_id": str(org_id)},
        exc_info=True,
    )


def test_idempotency_gate_check_failure() -> None:
    run_id = uuid.uuid4()
    with (
        patch.object(ex, "_should_skip_retry", side_effect=RuntimeError("boom")),
        patch.object(ex, "_log") as log,
    ):
        result = _executor()._idempotency_gate_ok(
            exc=MagicMock(spec=[]),
            run_markers={},
            run_id=run_id,
            idempotency_key=None,
            index=0,
            payload=None,
            superseded=False,
            stalled=False,
            cancellation_requested=False,
            single_sandbox_node=True,
        )
    assert result is False
    log.warning.assert_called_once_with(
        "pipeline.idempotency_gate.check_failed", extra={"run_id": str(run_id)}, exc_info=True
    )
