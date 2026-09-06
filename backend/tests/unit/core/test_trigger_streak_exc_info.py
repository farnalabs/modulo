"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.trigger_streak`` (PR #97).

Each test forces the best-effort ``except Exception`` branch to execute by
making the underlying dependency (redis client, DB session factory, settings)
raise, then asserts the warning is emitted with ``exc_info=True``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import trigger_streak as ts


def _ch_mock(**raised: object) -> MagicMock:
    """Return a fake ``cron_helpers`` module object used by ``_ch()``."""
    ch = MagicMock()
    for attr, exc in raised.items():
        getattr(ch, attr).side_effect = exc
    return ch


def _redis_mock(method: str) -> MagicMock:
    client = MagicMock()
    setattr(client, method, AsyncMock(side_effect=RuntimeError("boom")))
    return client


async def test_clear_trigger_streak_after_reenable_settings_failure() -> None:
    ch = _ch_mock(get_settings=RuntimeError("boom"))
    trigger_id = uuid.uuid4()
    with patch.object(ts, "_ch", return_value=ch), patch.object(ts, "_log") as log:
        await ts.clear_trigger_streak_after_reenable(trigger_id)
    log.warning.assert_called_once_with(
        "cron_helpers.clear_streak_after_reenable failed trigger=%s", trigger_id, exc_info=True
    )


async def test_count_recent_streak_deactivations_factory_failure() -> None:
    org_id = uuid.uuid4()
    factory = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(ts, "_log") as log:
        result = await ts._count_recent_streak_deactivations(factory, org_id, hours=24)
    assert result == 0
    log.warning.assert_called_once_with("streak.deactivation_count_failed org=%s", org_id, exc_info=True)


async def test_write_streak_notify_pending_redis_failure() -> None:
    org_id = uuid.uuid4()
    client = _redis_mock("sadd")
    with patch.object(ts, "_log") as log:
        await ts._write_streak_notify_pending(client, org_id, data={}, threshold=1, pipeline_name="p")
    log.warning.assert_called_once_with("streak.notify_pending_write_failed org=%s", org_id, exc_info=True)


async def test_record_streak_notify_failed_factory_failure() -> None:
    org_id = uuid.uuid4()
    ch = _ch_mock()
    ch._open_factory.return_value.side_effect = RuntimeError("boom")
    with patch.object(ts, "_ch", return_value=ch), patch.object(ts, "_log") as log:
        await ts._record_streak_notify_failed(org_id, data={}, threshold=1, reason="r")
    log.warning.assert_called_once_with("streak.notify_failed_audit_write_failed org=%s", org_id, exc_info=True)


async def test_trigger_active_state_factory_failure() -> None:
    org_id = uuid.uuid4()
    trigger_id = uuid.uuid4()
    ch = _ch_mock()
    ch._open_factory.return_value.side_effect = RuntimeError("boom")
    with patch.object(ts, "_ch", return_value=ch), patch.object(ts, "_log") as log:
        result = await ts._trigger_active_state(org_id, trigger_id)
    assert result is None
    log.warning.assert_called_once_with(
        "streak.trigger_active_read_failed org=%s trigger=%s", org_id, trigger_id, exc_info=True
    )


async def test_srem_streak_member_redis_failure() -> None:
    client = _redis_mock("srem")
    with patch.object(ts, "_log") as log:
        await ts._srem_streak_member(client, "key", "raw")
    log.warning.assert_called_once_with("streak.notify_pending_remove_failed key=%s", "key", exc_info=True)


async def test_read_streak_pending_members_redis_failure() -> None:
    org_id = uuid.uuid4()
    client = _redis_mock("smembers")
    with patch.object(ts, "_log") as log:
        result = await ts._read_streak_pending_members(client, org_id)
    assert result is None
    log.warning.assert_called_once_with("streak.notify_pending_read_failed org=%s", org_id, exc_info=True)


async def test_record_streak_mass_cascade_factory_failure() -> None:
    org_id = uuid.uuid4()
    ch = _ch_mock()
    ch._open_factory.return_value.side_effect = RuntimeError("boom")
    with patch.object(ts, "_ch", return_value=ch), patch.object(ts, "_log") as log:
        await ts._record_streak_mass_cascade(org_id, 5)
    log.warning.assert_called_once_with("streak.mass_cascade_audit_write_failed org=%s", org_id, exc_info=True)


async def test_streak_mass_cascade_alerted_factory_failure() -> None:
    org_id = uuid.uuid4()
    factory = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(ts, "_log") as log:
        result = await ts._streak_mass_cascade_alerted_this_window(factory, org_id)
    assert result is False
    log.warning.assert_called_once_with("streak.mass_cascade_dedup_check_failed org=%s", org_id, exc_info=True)


async def test_maybe_alert_mass_cascade_ingest_failure() -> None:
    org_id = uuid.uuid4()
    ch = _ch_mock()
    ch._open_factory.return_value.side_effect = RuntimeError("boom")
    ch._ingest_saq_error.side_effect = RuntimeError("boom")
    with (
        patch.object(ts, "_ch", return_value=ch),
        patch.object(ts, "_count_recent_streak_deactivations", return_value=99),
        patch.object(ts, "_record_streak_mass_cascade", new=AsyncMock(return_value=None)),
        patch.object(ts, "_streak_mass_cascade_alerted_this_window", new=AsyncMock(return_value=False)),
        patch.object(ts, "_log") as log,
    ):
        result = await ts._maybe_alert_mass_cascade(MagicMock(), org_id)
    assert result is False
    log.warning.assert_called_once_with("streak.mass_cascade_check_failed org=%s", org_id, exc_info=True)


async def test_pipeline_name_factory_failure() -> None:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    factory = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(ts, "_log") as log:
        result = await ts._pipeline_name(factory, org_id, pipeline_id)
    assert result == ""
    log.warning.assert_called_once_with("streak.pipeline_name_read_failed pipeline=%s", pipeline_id, exc_info=True)


async def test_retry_one_pending_member_dispatch_failure() -> None:
    org_id = uuid.uuid4()
    client = MagicMock()
    client.srem = AsyncMock()
    valid_uuid = str(uuid.uuid4())
    with (
        patch.object(ts, "_decode_pending_member", return_value={"trigger_id": valid_uuid, "pipeline_id": None}),
        patch.object(ts, "_trigger_active_state", return_value=False),
        patch.object(ts, "_dispatch_pending_member_notify", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ts, "_log") as log,
    ):
        status, _attempted = await ts._retry_one_pending_member(
            org_id, client, "key", "raw", attempted=0, max_retries=10
        )
    assert status == "failed"
    log.warning.assert_called_once_with("streak.notify_pending_retry_failed org=%s", org_id, exc_info=True)
