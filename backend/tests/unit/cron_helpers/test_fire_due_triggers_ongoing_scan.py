"""Unit tests for the ``ongoing`` scan section inside ``fire_due_triggers`` (FAR-158).

The ongoing scan is the LAST read in the per-org tick (after cron / polling /
reports, before the catch-up), wrapped in its own try/except so existing
fixed-order ``_MockSession`` sequences stay green. These tests drive
``fire_due_triggers()`` with a mocked session shaped like the existing
fixed-order tests, but with the ongoing read positioned LAST.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch
from tests.unit.cron_helpers.test_cron_helpers import (
    _cron_row,
    _MockSession,
    _org_result,
    _patch_env,
    _pause_result,
    _polling_row,
    _report_row,
    _rows_result,
    _settings,
)

ORG = uuid.uuid4()
TRIGGER_A = uuid.uuid4()
TRIGGER_POLL = uuid.uuid4()
REPORT = uuid.uuid4()
TRIGGER_ONGOING = uuid.uuid4()


def _ongoing_row(
    trigger_id: uuid.UUID,
    *,
    config_json: dict[str, Any] | None = None,
    next_fire_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=trigger_id,
        pipeline_id=uuid.uuid4(),
        config_json=config_json or {"snapshot_id": str(uuid.uuid4()), "scan_interval_seconds": 120},
        next_fire_at=next_fire_at,
    )


class TestFireDueTriggersOngoingScan:
    @pytest.mark.asyncio
    async def test_ongoing_scan_is_last_and_sums_counters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A, next_fire_at=now)]),
                _rows_result([_polling_row(TRIGGER_POLL)]),
                _rows_result([_report_row(REPORT)]),
                _rows_result([]),  # catch-up candidates read (org not paused)
                _rows_result([_ongoing_row(TRIGGER_ONGOING)]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id") as enqueue,
            patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True) as adv_ongoing,
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True),
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        # The summary exposes the ongoing counters alongside the existing ones.
        assert summary["cron_due"] == 1
        assert summary["cron_enqueued"] == 1
        assert summary["polling_due"] == 1
        assert summary["polling_enqueued"] == 1
        assert summary["report_due"] == 1
        assert summary["report_enqueued"] == 1
        assert summary["ongoing_due"] == 1
        assert summary["ongoing_enqueued"] == 1
        assert summary["ongoing_skipped_paused"] == 0
        assert summary["ongoing_enqueue_failures"] == 0
        adv_ongoing.assert_awaited_once()

        # The enqueued ongoing job targets the SAQ wrapper with the snapshot.
        ongoing_calls = [
            c for c in enqueue.await_args_list if c.args[1] == "modulo.core.saq_worker.fire_ongoing_trigger"
        ]
        assert len(ongoing_calls) == 1
        assert "latest_snapshot_id" in ongoing_calls[0].kwargs

        # The ongoing select is the LAST trigger-family read in the tick.
        trigger_selects = [(i, s) for i, (s, _p) in enumerate(session.executed) if "from triggers" in str(s).lower()]
        assert trigger_selects
        ongoing_index = next(
            i for i, s in trigger_selects if "ongoing" in str(s.compile(compile_kwargs={"literal_binds": True}))
        )
        report_index = next(
            i for i, (s, _p) in enumerate(session.executed) if "from scheduled_reports" in str(s).lower()
        )
        assert report_index < ongoing_index, "ongoing scan must run after the report read"

    @pytest.mark.asyncio
    async def test_ongoing_read_filters_soft_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ongoing trigger whose deleted_at is set must NOT be selected as
        due — the scan's WHERE carries deleted_at IS NULL."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),  # catch-up candidates read (org not paused)
                _rows_result([]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True),
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        ongoing_select = next(
            (
                s
                for s, _p in session.executed
                if "from triggers" in str(s).lower()
                and "ongoing" in str(s.compile(compile_kwargs={"literal_binds": True}))
            ),
            None,
        )
        assert ongoing_select is not None
        assert "deleted_at IS NULL" in str(ongoing_select)
        assert summary["ongoing_due"] == 0

    @pytest.mark.asyncio
    async def test_ongoing_read_exhausted_mock_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Existing fixed-order tests (whose result sequences end at the report
        read) hit an exhausted MagicMock for the ongoing read -> it iterates
        empty and the tick survives with zero ongoing counters."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A)]),
                _rows_result([]),
                _rows_result([]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id"),
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["ongoing_due"] == 0
        assert summary["ongoing_enqueued"] == 0
        assert summary["cron_enqueued"] == 1

    @pytest.mark.asyncio
    async def test_org_paused_skips_ongoing_enqueue_but_advances(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SKIP-not-defer: a paused org consumes the ongoing epoch (advance)
        but enqueues nothing; the summary exposes ongoing_skipped_paused."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG, paused=True),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),
                _rows_result([_ongoing_row(TRIGGER_ONGOING)]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
            patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True) as adv_ongoing,
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True),
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["ongoing_due"] == 1
        assert summary["ongoing_skipped_paused"] == 1
        assert summary["ongoing_enqueued"] == 0
        adv_ongoing.assert_awaited_once()
        ongoing_calls = [
            c for c in enqueue.await_args_list if c.args[1] == "modulo.core.saq_worker.fire_ongoing_trigger"
        ]
        assert ongoing_calls == []
