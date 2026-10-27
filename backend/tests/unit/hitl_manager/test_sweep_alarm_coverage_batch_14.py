"""Additional sweep-alarm helper and webhook coverage."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager import sweep_alarm
from modulo.core.hitl_manager.sweep_alarm import count_recent_approves, maybe_alarm_approve_sweep


@pytest.mark.asyncio
async def test_count_recent_approves_uses_real_aggregate_result():
    session = AsyncMock()
    result = MagicMock()
    result.one.return_value = (7, 3)
    session.execute.return_value = result
    result = await count_recent_approves(
        session,
        org_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        window_start=MagicMock(),
    )
    assert result == (7, 3)


def test_alarm_marker_overflow_drops_oldest():
    sweep_alarm.reset_alarm_state()
    now = MagicMock()
    first = (uuid.uuid4(), uuid.uuid4())
    sweep_alarm._alarm_marks[first] = now
    with patch.object(sweep_alarm, "_ALARM_MARK_MAX_KEYS", 1):
        second = (uuid.uuid4(), uuid.uuid4())
        sweep_alarm._mark_alarmed(second, now)
    assert first not in sweep_alarm._alarm_marks
    assert second in sweep_alarm._alarm_marks


def test_schedule_webhook_without_running_loop(caplog):
    with patch.object(sweep_alarm.asyncio, "get_running_loop", side_effect=RuntimeError):
        sweep_alarm._schedule_sweep_webhook(uuid.uuid4(), {})
    assert any("no_running_loop" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_webhook_body_is_failure_isolated():
    with (
        patch("modulo.core.notifier.Notifier", side_effect=RuntimeError("notifier down")),
        patch("modulo.db.session.get_shared_engine", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="secret")),
    ):
        await sweep_alarm._run_sweep_webhook(uuid.uuid4(), {"approve_count": 6})
    assert not sweep_alarm._PENDING_SWEEP_TASKS


@pytest.mark.asyncio
async def test_cancelled_alarm_propagates():
    session = MagicMock()
    with patch.object(sweep_alarm, "count_recent_approves", AsyncMock(side_effect=asyncio.CancelledError())):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=False)
        session.begin_nested.return_value = cm
        with pytest.raises(asyncio.CancelledError):
            await maybe_alarm_approve_sweep(session, org_id=uuid.uuid4(), actor_id=uuid.uuid4(), gate=MagicMock())
