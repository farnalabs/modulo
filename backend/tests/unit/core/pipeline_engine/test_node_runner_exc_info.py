"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.pipeline_engine.node_runner`` (PR #97).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_engine import node_runner as nr


async def test_read_run_raw_output_markers_failure() -> None:
    org_id = uuid.uuid4()
    with patch.object(nr, "_log") as log:
        result = await nr._read_run_raw_output_markers_for_gate(
            MagicMock(side_effect=RuntimeError("boom")),
            run_id="r",
            org_id_raw=str(org_id),
            node_id="n",
            claim_lease="tok",
        )
    assert result is None
    log.warning.assert_called_once_with(
        "sandbox_agent.idempotency_gate_read_failed",
        extra={"node_id": "n", "run_id": "r"},
        exc_info=True,
    )


async def test_read_connector_idempotency_gate_state_failure() -> None:
    org_id = uuid.uuid4()
    with patch.object(nr, "_log") as log:
        markers, key = await nr._read_connector_idempotency_gate_state(
            MagicMock(side_effect=RuntimeError("boom")),
            run_id="r",
            org_id_raw=str(org_id),
            node_id="n",
        )
    assert markers is None
    assert key is None
    log.warning.assert_called_once_with(
        "connector.idempotency_gate_read_failed",
        extra={"node_id": "n", "run_id": "r"},
        exc_info=True,
    )


def test_connector_on_unknown_read_failure() -> None:
    connector = MagicMock()
    connector.on_unknown_for = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(nr, "_log") as log:
        result = nr._connector_on_unknown(connector, "resource")
    assert result == nr.DEFAULT_ON_UNKNOWN
    log.warning.assert_called_once_with(
        "connector.idempotency_gate.on_unknown_read_failed",
        extra={"resource": "resource"},
        exc_info=True,
    )


def test_connector_gate_enabled_killswitch_failure() -> None:
    with (
        patch("modulo.settings.get_settings", side_effect=RuntimeError("boom")),
        patch.object(nr, "_log") as log,
    ):
        result = nr._connector_gate_enabled("fail_open", node_id="n", run_id="r")
    assert result is False
    log.warning.assert_called_once_with(
        "connector.idempotency_gate_killswitch_check_failed",
        extra={"node_id": "n", "run_id": "r"},
        exc_info=True,
    )


def test_connector_write_reported_failure_read_failure() -> None:
    connector = MagicMock()
    connector.connector_type = ""
    connector.write_reported_failure = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(nr, "_log") as log:
        result = nr._connector_write_reported_failure(connector, MagicMock())
    assert result is False
    log.warning.assert_called_once_with(
        "connector.idempotency_gate.write_reported_failure_read_failed",
        extra={"connector_type": ""},
        exc_info=True,
    )


async def test_enforce_resource_limits_metrics_unavailable() -> None:
    wd = nr._SandboxWatchdog(
        sandbox=MagicMock(),
        stall=MagicMock(),
        node_id="n",
        run_id="r",
        watch_log_path=None,
        watch_globs=[],
        resource_limits={"x": 1},
        sandbox_mode="script",
        stdout_percentage_delta=None,
        stream_broker=None,
        drained_chunks=[],
        wall_clock=nr._WatchdogWallClock(None, 0.0),
    )
    wd._sandbox.get_metrics = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(nr, "_log") as log:
        result = await wd.enforce_resource_limits()
    assert result is False
    log.warning.assert_called_once_with(
        "sandbox_agent.resource_metrics_unavailable",
        extra={"node_id": "n", "run_id": "r"},
        exc_info=True,
    )
