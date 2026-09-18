"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.cron_helpers`` (PR #97).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import cron_helpers as ch


def _redis_mock(method: str) -> MagicMock:
    client = MagicMock()
    setattr(client, method, AsyncMock(side_effect=RuntimeError("boom")))
    return client


async def test_write_dispatcher_reconcile_stats_redis_failure() -> None:
    client = _redis_mock("set")
    with patch.object(ch, "_log") as log:
        await ch.write_dispatcher_reconcile_stats(client, {"x": 1})
    log.warning.assert_called_once_with("cron_helpers.dispatcher_reconcile stats persist failed", exc_info=True)


async def test_handle_report_failure_counter_unavailable() -> None:
    report_id = uuid.uuid4()
    client = _redis_mock("incr")
    with patch.object(ch, "_log") as log:
        await ch._handle_report_failure(AsyncMock(), client, report_id, datetime.now(UTC))
    log.warning.assert_called_once_with(
        "cron_helpers.report_failure_counter_unavailable report=%s", report_id, exc_info=True
    )


async def test_clear_report_failure_counter_failed() -> None:
    report_id = uuid.uuid4()
    client = _redis_mock("delete")
    with patch.object(ch, "_log") as log:
        await ch._clear_report_failure_counter(client, report_id)
    log.warning.assert_called_once_with(
        "cron_helpers.clear_report_failure_counter failed for %s", report_id, exc_info=True
    )


async def test_bump_ongoing_failure_counter_unavailable() -> None:
    trigger_id = uuid.uuid4()
    client = _redis_mock("incr")
    with patch.object(ch, "_log") as log:
        await ch._bump_ongoing_failure(MagicMock(), client, trigger_id)
    log.warning.assert_called_once_with(
        "cron_helpers.ongoing_failure_counter_unavailable trigger=%s", trigger_id, exc_info=True
    )


async def test_clear_ongoing_failure_failed() -> None:
    trigger_id = uuid.uuid4()
    client = _redis_mock("delete")
    with patch.object(ch, "_log") as log:
        await ch._clear_ongoing_failure(client, trigger_id)
    log.warning.assert_called_once_with("cron_helpers.clear_ongoing_failure failed for %s", trigger_id, exc_info=True)


async def test_claim_catchup_marker_failed() -> None:
    trigger_id = uuid.uuid4()
    client = _redis_mock("set")
    with patch.object(ch, "_log") as log:
        result = await ch._claim_catchup_marker(client, trigger_id, 123)
    assert result is True
    log.warning.assert_called_once_with(
        "cron_helpers.catchup_marker_claim_failed trigger=%s", trigger_id, exc_info=True
    )


async def test_mark_catchup_fired_failed() -> None:
    trigger_id = uuid.uuid4()
    client = _redis_mock("set")
    with patch.object(ch, "_log") as log:
        await ch._mark_catchup_fired(client, trigger_id, 123)
    log.warning.assert_called_once_with(
        "cron_helpers.catchup_marker_write_failed trigger=%s", trigger_id, exc_info=True
    )


async def test_write_cron_liveness_failed() -> None:
    client = _redis_mock("set")
    with patch.object(ch, "_log") as log:
        await ch._write_cron_liveness(client)
    log.warning.assert_called_once_with("cron_helpers.fire_due_triggers liveness heartbeat write failed", exc_info=True)
