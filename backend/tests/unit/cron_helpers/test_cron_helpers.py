"""Unit tests for modulo.core.cron_helpers (plan F1) -” fire_due_triggers,
atomic next_fire_at advance, per-item fire jobs, report backoff/deactivate.

Mock/fake based (no real Postgres/Redis). The multi-worker race is covered at
the control-flow level here (atomic advance returning rows is the ONLY thing
that gates enqueue) and by a real-Redis two-process integration test.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.core import cron_helpers as ch

ORG = uuid.uuid4()
TRIGGER_A = uuid.uuid4()
TRIGGER_B = uuid.uuid4()
TRIGGER_POLL = uuid.uuid4()
REPORT = uuid.uuid4()

_UNSET_REPORT = object()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockBeginNested(_MockBegin):
    """Savepoint-style context manager — the degraded pause check opens one so
    a ProgrammingError rolls back only the check, never the outer transaction."""


class _MockSession:
    """Async session double returning a fixed sequence of execute() results."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[tuple[Any, Any]] = []
        self.added: list[object] = []
        self.begin_cm = _MockBegin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return self.begin_cm

    def begin_nested(self) -> _MockBeginNested:
        return _MockBeginNested()

    def get_bind(self) -> Any:
        return self._get_bind()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        # RLS set_config is plumbing — do not consume a result slot.
        if "set_config" in str(stmt):
            return MagicMock()
        if not self._results:
            return MagicMock()
        return self._results.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _pause_result(org_id: uuid.UUID, paused: bool = False, status: str = "active") -> MagicMock:
    """Result for the org-wide pause batched read: (id, triggers_paused, status)."""
    r = MagicMock()
    r.all.return_value = [(org_id, paused, status)]
    return r


@pytest.fixture(autouse=True)
def _org_not_paused() -> Generator[None, None, None]:
    """Default the per-fire-job org-pause check to not-paused.

    fire_cron_trigger/fire_polling_trigger now call ``org_is_paused``; the
    mocked sessions would otherwise read a MagicMock org row and fail-closed as
    paused, breaking the existing skip tests. Paused-specific tests override
    with a nested ``return_value=True`` patch.
    """
    with patch.object(ch, "org_is_paused", new_callable=AsyncMock, return_value=False):
        yield


def _rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _cron_row(
    trigger_id: uuid.UUID,
    *,
    snapshot_id: str | None = "default",
    cron_timezone: str | None = None,
    cron_expression: str = "*/30 * * * * *",
    next_fire_at: datetime | None = None,
    last_fired_at: datetime | None = None,
) -> SimpleNamespace:
    if snapshot_id == "default":
        snapshot_id = str(uuid.uuid4())
    return SimpleNamespace(
        id=trigger_id,
        pipeline_id=uuid.uuid4(),
        config_json={"snapshot_id": snapshot_id} if snapshot_id else {},
        cron_expression=cron_expression,
        cron_timezone=cron_timezone,
        next_fire_at=next_fire_at,
        last_fired_at=last_fired_at,
    )


def _polling_row(
    trigger_id: uuid.UUID, *, has_connector: bool = True, config_overrides: dict[str, Any] | None = None
) -> SimpleNamespace:
    config_json = (
        {"connector_instance_id": str(uuid.uuid4()), "poll_query": "SELECT 1", "poll_interval_seconds": 60}
        if has_connector
        else {}
    )
    if config_overrides:
        config_json.update(config_overrides)
    return SimpleNamespace(
        id=trigger_id,
        pipeline_id=uuid.uuid4(),
        config_json=config_json,
    )


def _report_row(report_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=report_id, cron_expression="0 9 * * *")


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "fernet_key": "b" * 44,
    }
    base.update(overrides)
    return MagicMock(**base)


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


def _make_trigger(**overrides: object) -> MagicMock:
    """Build a Trigger-like double with defaults for the per-item fire jobs."""
    from modulo.db.models.trigger import Trigger

    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "organisation_id": ORG,
        "pipeline_id": uuid.uuid4(),
        "active": True,
        "deleted_at": None,
        "max_concurrent_runs": 5,
        "daily_spend_limit": None,
        "config_json": {},
        "cron_timezone": None,
    }
    defaults.update(overrides)
    trigger = MagicMock(spec=Trigger)
    for key, value in defaults.items():
        setattr(trigger, key, value)
    return trigger


def _mock_result(**kwargs: Any) -> MagicMock:
    result = MagicMock()
    for name, value in kwargs.items():
        getattr(result, name).return_value = value
    return result


def _lock_result(acquired: bool) -> MagicMock:
    return _mock_result(scalar_one=acquired)


def _trigger_result(trigger: Any) -> MagicMock:
    return _mock_result(scalar_one_or_none=trigger)


# ---------------------------------------------------------------------------
# fire_due_triggers -” atomic advance + enqueue
# ---------------------------------------------------------------------------


class TestFireDueTriggers:
    @pytest.mark.asyncio
    async def test_enqueues_once_per_type_for_returned_rows_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A), _cron_row(TRIGGER_B, snapshot_id=str(uuid.uuid4()))]),
                _rows_result([_polling_row(TRIGGER_POLL)]),
                _rows_result([_report_row(REPORT)]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, side_effect=[True, False]) as adv_cron,
            patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=True) as adv_poll,
            patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True) as adv_report,
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        # TRIGGER_B's advance returned False (a second machine already advanced)
        # -> it must NOT be enqueued. Enqueue count == RETURNING-returned count.
        assert summary["cron_due"] == 2
        assert summary["cron_enqueued"] == 1
        assert summary["polling_due"] == 1
        assert summary["polling_enqueued"] == 1
        assert summary["report_due"] == 1
        assert summary["report_enqueued"] == 1
        assert summary["enqueue_failures"] == 0

        assert enqueue.await_count == 3
        adv_cron.assert_awaited()
        adv_poll.assert_awaited_once()
        adv_report.assert_awaited_once()
        # The enqueued cron job uses the per-epoch dedupe key.
        cron_key = enqueue.await_args_list[0].args[2]
        assert cron_key.startswith(f"fire:{TRIGGER_A}:")
        redis_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_soft_deleted_triggers_excluded_from_scans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-166: the cron/polling/catch-up scan SELECTs must filter
        ``Trigger.deleted_at IS NULL`` — a soft-deleted trigger (deleted_at set,
        active still True) must never be selected for firing."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A)]),
                _rows_result([_polling_row(TRIGGER_POLL)]),
                _rows_result([_report_row(REPORT)]),
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
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id"),
            patch.object(ch, "_claim_catchup_marker", new_callable=AsyncMock, return_value=False),
            patch.object(ch, "_mark_catchup_fired", new_callable=AsyncMock),
        ):
            redis_cls.from_url.return_value = redis_client
            await ch.fire_due_triggers()

        # Every scan over the triggers table — cron, polling, and the missed-fire
        # catch-up scan — must carry the deleted_at filter.
        trigger_scans = [(s, p) for s, p in session.executed if "FROM triggers" in str(s)]
        assert trigger_scans, "expected cron/polling/catch-up scan statements"
        for stmt, _params in trigger_scans:
            assert "deleted_at IS NULL" in str(stmt), f"scan missing deleted_at filter: {stmt}"

    @pytest.mark.asyncio
    async def test_second_machine_epoch_skipped_when_already_advanced(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            # A concurrent machine already advanced next_fire_at.
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=False),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_due"] == 1
        assert summary["cron_enqueued"] == 0
        enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_enqueue_failure_ingests_error_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=RuntimeError("redis down")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_enqueued"] == 0
        assert summary["enqueue_failures"] == 1
        ingest.assert_awaited_once()
        kwargs = ingest.await_args.kwargs
        assert kwargs["function"] == "fire_due_triggers"
        assert kwargs["context"]["trigger_type"] == "cron"

    @pytest.mark.asyncio
    async def test_cron_advance_passes_row_cron_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fix 2 review: fire_due_triggers must fetch cron_timezone from the
        SELECT and pass it through to the atomic advance."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A, cron_timezone="America/New_York")]),
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
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True) as adv_cron,
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id"),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_enqueued"] == 1
        adv_cron.assert_awaited_once()
        assert adv_cron.await_args.args[3] == "America/New_York"

    @pytest.mark.asyncio
    async def test_malformed_poll_interval_seconds_defaults_to_60(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed ``poll_interval_seconds`` must not kill the global tick.

        Regression test for the ``int()`` guard: a non-numeric value used to
        raise ``ValueError`` straight out of ``fire_due_triggers`` (a global
        tick killer). Now it is caught, logged, and the default 60s interval
        is used so the polling trigger still fires.
        """
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A)]),
                _rows_result([_polling_row(TRIGGER_POLL, config_overrides={"poll_interval_seconds": "not-an-int"})]),
                _rows_result([_report_row(REPORT)]),
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
            patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=True) as adv_poll,
            patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id"),
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        # The tick survives and the polling trigger fires with the 60s default.
        assert summary["polling_due"] == 1
        assert summary["polling_enqueued"] == 1
        adv_poll.assert_awaited_once()
        assert adv_poll.await_args.args[2] == 60

    @pytest.mark.asyncio
    async def test_paused_org_skips_cron_and_polling_enqueue_with_advance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SKIP-not-defer: paused org -> next_fire_at still advanced, fire jobs
        NOT enqueued, skip counters incremented, scheduled reports still enqueued."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG, paused=True),
                _rows_result([_cron_row(TRIGGER_A)]),
                _rows_result([_polling_row(TRIGGER_POLL)]),
                _rows_result([_report_row(REPORT)]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True) as adv_cron,
            patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=True) as adv_poll,
            patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True) as adv_report,
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id") as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        # Cron + polling skipped but ADVANCED (no catch-up storm on unpause).
        assert summary["cron_due"] == 1
        assert summary["cron_skipped_paused"] == 1
        assert summary["cron_enqueued"] == 0
        assert summary["polling_due"] == 1
        assert summary["polling_skipped_paused"] == 1
        assert summary["polling_enqueued"] == 0
        # Reports always enqueue during a pause (documented decision).
        assert summary["report_due"] == 1
        assert summary["report_enqueued"] == 1
        adv_cron.assert_awaited_once()
        adv_poll.assert_awaited_once()
        adv_report.assert_awaited_once()
        # Only the report job is enqueued.
        assert enqueue.await_count == 1

    @pytest.mark.asyncio
    async def test_pause_read_programming_error_degrades_to_not_paused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pre-migration DB (no triggers_paused column): ProgrammingError on the
        batched pause read -> pause_read='degraded', all orgs treated as
        not-paused (legacy behaviour preserved)."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _rows_result([_cron_row(TRIGGER_A)]),
                _rows_result([]),
                _rows_result([]),
            ]
        )
        original_execute = session.execute

        async def _execute_with_failing_pause(stmt: Any, params: dict[str, Any] | None = None) -> Any:
            if "triggers_paused" in str(stmt):
                raise ProgrammingError("stmt", {}, Exception("column triggers_paused does not exist"))
            return await original_execute(stmt, params)

        session.execute = _execute_with_failing_pause
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id") as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["pause_read"] == "degraded"
        assert summary["cron_skipped_paused"] == 0
        assert summary["cron_enqueued"] == 1
        assert enqueue.await_count == 1

    @pytest.mark.asyncio
    async def test_pause_read_sqlalchemy_error_reraises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Other DB error (connection down) on the pause read -> RE-RAISE so the
        tick fails and the SAQ system cron retries. Never fabricate 'paused'
        for every org on a DB blip."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _rows_result([_cron_row(TRIGGER_A)]),
                _rows_result([]),
                _rows_result([]),
            ]
        )
        original_execute = session.execute

        async def _execute_with_failing_pause(stmt: Any, params: dict[str, Any] | None = None) -> Any:
            if "triggers_paused" in str(stmt):
                raise SQLAlchemyError("connection boom")
            return await original_execute(stmt, params)

        session.execute = _execute_with_failing_pause
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id") as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            with pytest.raises(SQLAlchemyError, match="connection boom"):
                await ch.fire_due_triggers()

        enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_orgs_returns_empty_summary_without_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty-org environment: no Redis client is ever created and the
        summary is all-zero — the system cron ticks harmlessly."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([])])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock) as adv_cron,
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
        ):
            summary = await ch.fire_due_triggers()

        redis_cls.from_url.assert_not_called()
        adv_cron.assert_not_awaited()
        enqueue.assert_not_awaited()
        assert summary["orgs_scanned"] == 0
        assert summary["cron_due"] == 0
        assert summary["polling_due"] == 0
        assert summary["report_due"] == 0

    @pytest.mark.asyncio
    async def test_enqueue_failure_rolls_back_advance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2026-08-10 incident (A): when the enqueue fails AFTER the atomic
        advance, the advance is rolled back to the trigger's previous
        ``next_fire_at`` (guarded) so the next tick re-selects the epoch."""
        _patch_env(monkeypatch)
        old = datetime.now(UTC) - timedelta(hours=1)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A, next_fire_at=old)]),
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
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=RuntimeError("redis down")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_enqueued"] == 0
        assert summary["enqueue_failures"] == 1
        ingest.assert_awaited_once()
        rollback = [(s, p) for s, p in session.executed if "UPDATE triggers" in str(s)]
        assert rollback, "compensating rollback UPDATE must run on enqueue failure"
        stmt, params = rollback[0]
        # Guarded: restores ONLY when the row still holds this machine's advance.
        assert "next_fire_at = :advanced" in str(stmt)
        assert params["old"] == old
        assert params["tid"] == str(TRIGGER_A)
        assert params["advanced"] > old

    @pytest.mark.asyncio
    async def test_rollback_advance_is_guarded_and_noops_without_previous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2026-08-10 incident (E): the compensating rollback is a guarded
        UPDATE — it can never clobber a concurrent machine's newer advance,
        and with no previous value it is a no-op."""
        _patch_env(monkeypatch)
        old = datetime.now(UTC) - timedelta(days=1)
        session = _MockSession([])
        await ch._rollback_cron_advance(
            session,
            TRIGGER_A,
            "*/30 * * * * *",
            None,
            previous_next_fire=old,
            now=datetime.now(UTC),
        )
        updates = [(s, p) for s, p in session.executed if "UPDATE triggers" in str(s)]
        assert updates, "guarded rollback UPDATE must run"
        stmt, params = updates[0]
        assert "SET next_fire_at = :old" in str(stmt)
        assert "next_fire_at = :advanced" in str(stmt)  # the guard clause
        assert params["old"] == old
        assert params["tid"] == str(TRIGGER_A)
        assert params["advanced"] > old

        noop_session = _MockSession([])
        await ch._rollback_cron_advance(
            noop_session,
            TRIGGER_A,
            "*/30 * * * * *",
            None,
            previous_next_fire=None,
            now=datetime.now(UTC),
        )
        assert not any("UPDATE triggers" in str(s) for s, _ in noop_session.executed)

    def test_cron_cadence_seconds_reuses_error_tracking_helper(self) -> None:
        """The catch-up cadence is computed by the SAME helper the hourly
        missed-fire alert probe uses, so both agree on 'behind schedule'."""
        now = datetime.now(UTC)
        assert ch._cron_cadence_seconds("0 4 * * *", None, now) == 86400
        assert ch._cron_cadence_seconds("*/5 * * * *", None, now) == 300
        assert ch._cron_cadence_seconds("0 4 * * *", "invalid", now) is None

    @pytest.mark.asyncio
    async def test_catchup_refires_consumed_epoch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2026-08-10 incident (B): a cron trigger whose ``next_fire_at`` is
        already in the FUTURE (epoch consumed) but whose ``last_fired_at`` is
        older than cadence + grace is re-fired exactly once, and its
        ``next_fire_at`` advances."""
        _patch_env(monkeypatch)
        now_val = datetime.now(UTC)
        old = now_val - timedelta(hours=25)  # missed ~1 daily cadence
        future = now_val + timedelta(hours=24)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),
                _rows_result(
                    [_cron_row(TRIGGER_A, cron_expression="0 4 * * *", next_fire_at=future, last_fired_at=old)]
                ),
                _mock_result(fetchone=("advanced-id",)),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.set.return_value = True  # atomic claim wins — no marker yet

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_catchup_enqueued"] == 1
        assert summary["enqueue_failures"] == 0
        enqueue.assert_awaited_once()
        cron_key = enqueue.await_args.args[2]
        assert cron_key.startswith(f"fire:{TRIGGER_A}:")
        assert enqueue.await_args.kwargs["cron_expression"] == "0 4 * * *"
        # The marker is written so the same missed epoch is not re-fired while
        # the fire job is pending.
        redis_client.set.assert_awaited()
        # The catch-up advance used the exact-observed-value guard.
        advance = [(s, p) for s, p in session.executed if "next_fire_at = :expected" in str(s)]
        assert advance, "catch-up advance must use the observed-value guard"
        assert advance[0][1]["tid"] == str(TRIGGER_A)
        assert advance[0][1]["expected"] == future
        assert advance[0][1]["nf"] > now_val

    @pytest.mark.asyncio
    async def test_catchup_skips_fresh_last_fire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2026-08-10 incident (C): a trigger whose last fire is fresh (normal
        state) is NEVER caught up — no double-fire of a healthy trigger."""
        _patch_env(monkeypatch)
        now_val = datetime.now(UTC)
        fresh = now_val - timedelta(minutes=5)
        future = now_val + timedelta(hours=24)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),
                _rows_result(
                    [_cron_row(TRIGGER_A, cron_expression="0 4 * * *", next_fire_at=future, last_fired_at=fresh)]
                ),
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
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_catchup_enqueued"] == 0
        enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_catchup_skips_ancient_stale_trigger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2026-08-10 incident (D): an ancient stale trigger (disabled for
        months, then re-enabled) is NOT caught up — the bounded window caps it."""
        _patch_env(monkeypatch)
        now_val = datetime.now(UTC)
        ancient = now_val - timedelta(days=60)
        future = now_val + timedelta(hours=24)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),
                _rows_result(
                    [_cron_row(TRIGGER_A, cron_expression="0 4 * * *", next_fire_at=future, last_fired_at=ancient)]
                ),
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
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_catchup_enqueued"] == 0
        enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_catchup_skips_epoch_already_fired_this_tick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The catch-up scan must NOT re-fire an epoch the normal due-loop
        already advanced AND enqueued in the same tick (double-fire guard)."""
        _patch_env(monkeypatch)
        now_val = datetime.now(UTC)
        old = now_val - timedelta(hours=25)
        past = now_val - timedelta(minutes=1)
        future = now_val + timedelta(hours=24)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A, next_fire_at=past)]),
                _rows_result([]),
                _rows_result([]),
                _rows_result(
                    [_cron_row(TRIGGER_A, cron_expression="0 4 * * *", next_fire_at=future, last_fired_at=old)]
                ),
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
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id") as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_enqueued"] == 1
        assert summary["cron_catchup_enqueued"] == 0
        enqueue.assert_awaited_once()  # ONLY the normal fire, never the catch-up

    @pytest.mark.asyncio
    async def test_catchup_skips_marked_epoch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missed epoch already caught up (Redis marker present — fire job
        still pending) is NOT re-fired on a later tick."""
        _patch_env(monkeypatch)
        now_val = datetime.now(UTC)
        old = now_val - timedelta(hours=25)
        future = now_val + timedelta(hours=24)
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),
                _rows_result(
                    [_cron_row(TRIGGER_A, cron_expression="0 4 * * *", next_fire_at=future, last_fired_at=old)]
                ),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.set.return_value = None  # SET NX returns None — marker present, claim lost

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_catchup_enqueued"] == 0
        enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# fire_report_trigger -” backoff + deactivate-after-5
# ---------------------------------------------------------------------------


class TestFireReportTrigger:
    def _report_session(
        self,
        *,
        report: SimpleNamespace | None = _UNSET_REPORT,
    ) -> _MockSession:
        if report is _UNSET_REPORT:
            report = SimpleNamespace(
                id=REPORT,
                organisation_id=ORG,
                report_type="quality",
                config_json={"schedule_type": "recurring"},
                recipient_config={"type": "webhook", "urls": ["https://x"]},
                active=True,
                cron_expression="0 9 * * *",
            )
        r = MagicMock()
        r.scalar_one_or_none.return_value = report
        return _MockSession([r])

    @pytest.mark.asyncio
    async def test_success_sets_last_sent_at_and_clears_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = self._report_session()
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        async def _fake_generator(s, org, config):
            return {"rows": 1}

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.reports.scheduler.get_generator", return_value=_fake_generator),
            patch("modulo.core.reports.scheduler.get_formatter", return_value=None),
            patch("modulo.core.reports.scheduler.get_deliverer", return_value=None),
            patch(
                "modulo.core.reports.scheduler._deliver_via_config",
                new_callable=AsyncMock,
                return_value=[{"status": "delivered"}],
            ) as deliver,
            patch.object(ch, "_clear_report_failure_counter", new_callable=AsyncMock) as clear,
        ):
            redis_cls.from_url.return_value = redis_client
            result = await ch.fire_report_trigger(report_id=REPORT, org_id=ORG)

        assert result["status"] == "sent"
        deliver.assert_awaited_once()
        clear.assert_awaited_once()
        redis_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_backs_off_next_send_at_not_reenqueue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = self._report_session()
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.incr = AsyncMock(return_value=1)

        async def _boom_generator(s, org, config):
            raise RuntimeError("delivery boom")

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.reports.scheduler.get_generator", return_value=_boom_generator),
            patch.object(ch, "_handle_report_failure", new_callable=AsyncMock) as handle_failure,
        ):
            redis_cls.from_url.return_value = redis_client
            result = await ch.fire_report_trigger(report_id=REPORT, org_id=ORG)

        assert result["status"] == "failed"
        assert result["reason"] == "generation_or_delivery_failed"
        handle_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_when_report_inactive_or_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deactivated/deleted report is skipped silently — no failure counter,
        no backoff, no re-enqueue loop."""
        _patch_env(monkeypatch)
        session = self._report_session(report=None)
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "_handle_report_failure", new_callable=AsyncMock) as handle_failure,
        ):
            redis_cls.from_url.return_value = redis_client
            result = await ch.fire_report_trigger(report_id=REPORT, org_id=ORG)

        assert result["status"] == "skipped"
        assert result["reason"] == "report_inactive_or_missing"
        handle_failure.assert_not_awaited()
        redis_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fails_when_no_generator_with_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No generator registered for the report type -> back off next_send_at
        (never silently loop on a misconfigured report)."""
        _patch_env(monkeypatch)
        session = self._report_session()
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.incr = AsyncMock(return_value=1)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch("modulo.core.reports.scheduler.get_generator", return_value=None),
            patch.object(ch, "_handle_report_failure", new_callable=AsyncMock) as handle_failure,
        ):
            redis_cls.from_url.return_value = redis_client
            result = await ch.fire_report_trigger(report_id=REPORT, org_id=ORG)

        assert result["status"] == "failed"
        assert result["reason"] == "no_generator_for_quality"
        handle_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_success_deactivates_one_time_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A one_time report is deactivated after delivery — never re-fired by
        the scheduler (next_send_at also cleared)."""
        _patch_env(monkeypatch)
        one_time = SimpleNamespace(
            id=REPORT,
            organisation_id=ORG,
            report_type="quality",
            config_json={"schedule_type": "one_time"},
            recipient_config={"type": "webhook", "urls": ["https://x"]},
            active=True,
            cron_expression="0 9 * * *",
        )
        session = self._report_session(report=one_time)
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        async def _fake_generator(s, org, config):
            return {"rows": 1}

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch("modulo.core.reports.scheduler.get_generator", return_value=_fake_generator),
            patch("modulo.core.reports.scheduler.get_formatter", return_value=None),
            patch("modulo.core.reports.scheduler.get_deliverer", return_value=None),
            patch(
                "modulo.core.reports.scheduler._deliver_via_config",
                new_callable=AsyncMock,
                return_value=[{"status": "delivered"}],
            ) as deliver,
            patch.object(ch, "_clear_report_failure_counter", new_callable=AsyncMock),
        ):
            redis_cls.from_url.return_value = redis_client
            result = await ch.fire_report_trigger(report_id=REPORT, org_id=ORG)

        assert result["status"] == "sent"
        deliver.assert_awaited_once()
        final_stmt, _ = session.executed[-1]
        params = final_stmt.compile().params
        assert params["active"] is False
        assert params["next_send_at"] is None
        redis_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_report_failure_uses_5min_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.incr = AsyncMock(return_value=1)
        redis_client.expire = AsyncMock(return_value=True)

        now = ch.datetime.now(ch.UTC)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
        ):
            redis_cls.from_url.return_value = redis_client
            await ch._handle_report_failure(session, redis_client, REPORT, now)

        # The UPDATE must back off next_send_at by exactly +5min (never the
        # 30s re-enqueue cadence). The 300s literal guards REPORT_BACKOFF_SECONDS
        # itself — asserting against the constant would be tautological.
        assert len(session.executed) == 1
        backoff_param = session.executed[0][0].compile().params["next_send_at"]
        assert backoff_param == now + timedelta(seconds=300)
        redis_client.incr.assert_awaited_once_with(ch._report_failure_counter_key(REPORT))
        redis_client.expire.assert_awaited_once_with(
            ch._report_failure_counter_key(REPORT),
            ch._REPORT_FAILURE_COUNTER_TTL,
        )

    @pytest.mark.asyncio
    async def test_backoff_applied_when_redis_counter_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Redis failure while counting must NOT stop the +5min backoff."""
        _patch_env(monkeypatch)
        session = _MockSession([])
        redis_client = AsyncMock()
        redis_client.incr = AsyncMock(side_effect=RuntimeError("redis down"))

        now = ch.datetime.now(ch.UTC)
        with (
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
        ):
            redis_cls.from_url.return_value = redis_client
            await ch._handle_report_failure(session, redis_client, REPORT, now)

        # Best-effort counter: the next_send_at backoff alone stops the every-30s loop.
        assert len(session.executed) == 1
        backoff_param = session.executed[0][0].compile().params["next_send_at"]
        assert backoff_param == now + timedelta(seconds=300)

    @pytest.mark.asyncio
    async def test_deactivate_after_5_consecutive_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        redis_client = AsyncMock()
        redis_client.incr = AsyncMock(return_value=5)
        redis_client.expire = AsyncMock(return_value=True)

        now = ch.datetime.now(ch.UTC)
        with (
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
        ):
            redis_cls.from_url.return_value = redis_client
            await ch._handle_report_failure(session, redis_client, REPORT, now)

        # 5th consecutive failure -> UPDATE ... SET active=False (not just any
        # UPDATE mentioning the column).
        assert len(session.executed) == 2
        second_stmt, _ = session.executed[1]
        assert second_stmt.compile().params["active"] is False


# ---------------------------------------------------------------------------
# Atomic advance helpers -” SQL shape
# ---------------------------------------------------------------------------


class TestAtomicAdvance:
    def test_advance_stmt_is_conditional_with_returning(self) -> None:
        stmt = ch._atomic_advance_stmt()
        sql = str(stmt)
        assert "RETURNING id" in sql
        assert "next_fire_at <= now()" in sql
        assert "next_fire_at IS NULL" in sql  # never-fired triggers are due too
        assert "trigger_type = :ttype" in sql

    @pytest.mark.asyncio
    async def test_cron_advance_returns_true_when_row_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = (TRIGGER_A,)
        session.execute = AsyncMock(return_value=r)
        ok = await ch._advance_cron_next_fire(session, TRIGGER_A, "*/30 * * * * *")
        assert ok is True
        _stmt, params = session.execute.await_args.args
        assert params["ttype"] == "cron"
        assert params["tid"] == str(TRIGGER_A)

    @pytest.mark.asyncio
    async def test_cron_advance_returns_false_when_not_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = None
        session.execute = AsyncMock(return_value=r)
        ok = await ch._advance_cron_next_fire(session, TRIGGER_A, "*/30 * * * * *")
        assert ok is False

    @pytest.mark.asyncio
    async def test_cron_advance_honors_configured_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fix 2 review: a non-UTC trigger must advance next_fire_at in ITS
        timezone (legacy CronFireTask behaviour), never UTC."""
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = (TRIGGER_A,)
        session.execute = AsyncMock(return_value=r)
        with patch.object(ch, "compute_next_fire") as cnf:
            cnf.return_value = ch.datetime.now(ch.UTC)
            ok = await ch._advance_cron_next_fire(session, TRIGGER_A, "0 9 * * *", "America/New_York")
        assert ok is True
        cnf.assert_called_once()
        assert cnf.call_args.kwargs["timezone"] == "America/New_York"

    @pytest.mark.asyncio
    async def test_cron_advance_defaults_to_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = (TRIGGER_A,)
        session.execute = AsyncMock(return_value=r)
        with patch.object(ch, "compute_next_fire") as cnf:
            cnf.return_value = ch.datetime.now(ch.UTC)
            ok = await ch._advance_cron_next_fire(session, TRIGGER_A, "0 9 * * *", None)
        assert ok is True
        assert cnf.call_args.kwargs["timezone"] == "UTC"

    @pytest.mark.asyncio
    async def test_polling_advance_returns_true_when_row_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = (TRIGGER_POLL,)
        session.execute = AsyncMock(return_value=r)
        before = datetime.now(UTC)
        ok = await ch._advance_polling_next_fire(session, TRIGGER_POLL, 60)
        assert ok is True
        _stmt, params = session.execute.await_args.args
        assert params["ttype"] == "polling"
        assert params["tid"] == str(TRIGGER_POLL)
        # The advanced next_fire_at is now + poll_interval (60s), allowing for
        # the wall-clock drift between the `before` snapshot and the advance.
        assert before + timedelta(seconds=60) <= params["nf"] <= datetime.now(UTC) + timedelta(seconds=60)

    @pytest.mark.asyncio
    async def test_polling_advance_returns_false_when_not_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = None
        session.execute = AsyncMock(return_value=r)
        ok = await ch._advance_polling_next_fire(session, TRIGGER_POLL, 60)
        assert ok is False

    @pytest.mark.asyncio
    async def test_polling_advance_floors_interval_at_one_second(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A poll interval <= 0 must never produce a non-advancing (<= now) nf."""
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = (TRIGGER_POLL,)
        session.execute = AsyncMock(return_value=r)
        await ch._advance_polling_next_fire(session, TRIGGER_POLL, 0)
        params = session.execute.await_args.args[1]
        assert params["nf"] > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_report_advance_returns_true_when_row_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = (REPORT,)
        session.execute = AsyncMock(return_value=r)
        ok = await ch._advance_report_next_send(session, REPORT, "0 9 * * *")
        assert ok is True
        _stmt, params = session.execute.await_args.args
        assert params["rid"] == str(REPORT)
        # next_send_at is a future cron match, never the raw expression.
        assert params["ns"] > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_report_advance_returns_false_when_not_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = None
        session.execute = AsyncMock(return_value=r)
        ok = await ch._advance_report_next_send(session, REPORT, "0 9 * * *")
        assert ok is False

    @pytest.mark.asyncio
    async def test_report_advance_uses_conditional_sql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The report advance must be a conditional UPDATE ... RETURNING id —
        the same multi-machine atomicity guarantee as the trigger advances."""
        _patch_env(monkeypatch)
        session = _MockSession([])
        r = MagicMock()
        r.fetchone.return_value = (REPORT,)
        session.execute = AsyncMock(return_value=r)
        await ch._advance_report_next_send(session, REPORT, "0 9 * * *")
        stmt, _ = session.execute.await_args.args
        sql = str(stmt)
        assert "RETURNING id" in sql
        assert "next_send_at <= now()" in sql
        assert "next_send_at IS NULL" in sql


class TestEnqueueFireJob:
    @pytest.mark.asyncio
    async def test_returns_job_id_with_bounded_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Per-item fire jobs must carry the bounded SAQ knobs — a bug here
        (e.g. retries=0) silently drops a fire forever."""
        _patch_env(monkeypatch)
        q = MagicMock()
        q.enqueue = AsyncMock(return_value=SimpleNamespace(id="job-123"))
        job_id = await ch._enqueue_fire_job_async(
            q,
            "modulo.core.saq_worker.fire_cron_trigger",
            f"fire:{TRIGGER_A}:123",
            trigger_id=str(TRIGGER_A),
            org_id=str(ORG),
        )
        assert job_id == "job-123"
        assert q.enqueue.await_args.kwargs["timeout"] == ch.FIRE_JOB_TIMEOUT
        assert q.enqueue.await_args.kwargs["heartbeat"] == ch.FIRE_JOB_HEARTBEAT
        assert q.enqueue.await_args.kwargs["retries"] == ch.FIRE_JOB_RETRIES
        assert q.enqueue.await_args.kwargs["ttl"] == ch.FIRE_JOB_TTL
        # The per-epoch dedupe key is passed through as the SAQ job key.
        assert q.enqueue.await_args.kwargs["key"] == f"fire:{TRIGGER_A}:123"

    @pytest.mark.asyncio
    async def test_returns_none_when_saq_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A concurrent machine already enqueued the epoch -> SAQ returns None
        and the scheduler must count it as NOT enqueued."""
        _patch_env(monkeypatch)
        q = MagicMock()
        q.enqueue = AsyncMock(return_value=None)
        job_id = await ch._enqueue_fire_job_async(q, "f", "fire:x:1")
        assert job_id is None


# ---------------------------------------------------------------------------
# Fire logic skips (mirrors the relocated CronFireTask semantics)
# ---------------------------------------------------------------------------


class TestFireCronTriggerSkips:
    @pytest.mark.asyncio
    async def test_skips_when_trigger_inactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)

        trigger = _make_trigger(id=TRIGGER_A, active=False)

        session = _MockSession([_lock_result(True), _trigger_result(trigger)])
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=TRIGGER_A,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "trigger_inactive_or_missing"

    @pytest.mark.asyncio
    async def test_skips_when_trigger_soft_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-166: a soft-deleted cron trigger (deleted_at set, active still
        True) is not loaded by the fire job — the load SELECT filters
        ``deleted_at``, so the trigger resolves to None and the job skips as
        inactive/missing."""
        _patch_env(monkeypatch)

        trigger = _make_trigger(id=TRIGGER_A, deleted_at=datetime.now(UTC))

        session = _MockSession([_lock_result(True), _trigger_result(None)])
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=TRIGGER_A,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "trigger_inactive_or_missing"
        # The load SELECT must carry the deleted_at filter — the guard that
        # turns a soft-deleted trigger into "not found" (skip).
        load = [(s, p) for s, p in session.executed if "FROM triggers" in str(s)]
        assert load, "expected the fire-job load SELECT"
        assert "deleted_at IS NULL" in str(load[0][0])

    @pytest.mark.asyncio
    async def test_skips_when_spend_limit_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        from decimal import Decimal

        trigger = _make_trigger(id=TRIGGER_A, daily_spend_limit=Decimal("100.00"))
        cost_result = _mock_result(scalar_one=Decimal("150.00"))

        session = _MockSession([_lock_result(True), _trigger_result(trigger), cost_result])
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=TRIGGER_A,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "spend_limit"

    @pytest.mark.asyncio
    async def test_skips_when_org_triggers_paused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paused org -> fire_cron_trigger returns skipped/triggers_paused with
        NO event row written."""
        _patch_env(monkeypatch)

        from modulo.db.models.trigger import Trigger

        trigger = MagicMock(spec=Trigger)
        trigger.id = TRIGGER_A
        trigger.organisation_id = ORG
        trigger.pipeline_id = uuid.uuid4()
        trigger.active = True
        trigger.deleted_at = None
        trigger.max_concurrent_runs = 5
        trigger.daily_spend_limit = None
        trigger.config_json = {}

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True
        trigger_result = MagicMock()
        trigger_result.scalar_one_or_none.return_value = trigger

        session = _MockSession([lock_result, trigger_result])
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock),
            patch.object(ch, "org_is_paused", new_callable=AsyncMock, return_value=True),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=TRIGGER_A,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "triggers_paused"
        assert not session.added

    @pytest.mark.asyncio
    async def test_cron_fire_degrades_on_pause_read_programming_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pre-migration DB: a ProgrammingError from the org pause read degrades
        to NOT paused (inside a savepoint, so the transaction is never poisoned)
        and the job proceeds to fire — mirroring the scheduler's not-paused
        choice, instead of dead-lettering every cron job."""
        _patch_env(monkeypatch)

        from modulo.db.models.trigger import Trigger

        trigger = MagicMock(spec=Trigger)
        trigger.id = TRIGGER_A
        trigger.organisation_id = ORG
        trigger.pipeline_id = uuid.uuid4()
        trigger.active = True
        trigger.deleted_at = None
        trigger.max_concurrent_runs = 5
        trigger.daily_spend_limit = None
        trigger.config_json = {}

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True
        trigger_result = MagicMock()
        trigger_result.scalar_one_or_none.return_value = trigger

        session = _MockSession([lock_result, trigger_result])
        factory = MagicMock(return_value=session)
        run_mock = MagicMock()
        run_mock.id = uuid.uuid4()
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock) as log_event,
            patch.object(
                ch,
                "org_is_paused",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("column triggers_paused does not exist")),
            ),
            patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, return_value=run_mock),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=TRIGGER_A,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        # Degraded not-paused -> the job proceeds and fires a run.
        assert result["status"] == "fired"
        assert log_event.await_count == 1

    @pytest.mark.asyncio
    async def test_cron_fire_propagates_on_pause_read_sqlalchemy_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plain SQLAlchemyError (DB down / connection error) from the org
        pause read PROPAGATES so the job fails and SAQ retries — never degraded
        to not-paused, never fabricated into paused."""
        _patch_env(monkeypatch)

        from modulo.db.models.trigger import Trigger

        trigger = MagicMock(spec=Trigger)
        trigger.id = TRIGGER_A
        trigger.organisation_id = ORG
        trigger.pipeline_id = uuid.uuid4()
        trigger.active = True
        trigger.deleted_at = None
        trigger.max_concurrent_runs = 5
        trigger.daily_spend_limit = None
        trigger.config_json = {}

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True
        trigger_result = MagicMock()
        trigger_result.scalar_one_or_none.return_value = trigger

        session = _MockSession([lock_result, trigger_result])
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "org_is_paused", new_callable=AsyncMock, side_effect=SQLAlchemyError("connection boom")),
            pytest.raises(SQLAlchemyError, match="connection boom"),
        ):
            await ch.fire_cron_trigger(
                trigger_id=TRIGGER_A,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_polling_skips_when_org_triggers_paused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Paused org -> fire_polling_trigger returns skipped/triggers_paused with
        NO event row written."""
        _patch_env(monkeypatch)

        from modulo.db.models.trigger import Trigger

        trigger = MagicMock(spec=Trigger)
        trigger.id = TRIGGER_POLL
        trigger.organisation_id = ORG
        trigger.pipeline_id = uuid.uuid4()
        trigger.active = True
        trigger.deleted_at = None
        trigger.max_concurrent_runs = 5
        trigger.daily_spend_limit = None
        trigger.config_json = {}

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True
        trigger_result = MagicMock()
        trigger_result.scalar_one_or_none.return_value = trigger

        session = _MockSession([lock_result, trigger_result])
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock),
            patch.object(ch, "org_is_paused", new_callable=AsyncMock, return_value=True),
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=TRIGGER_POLL,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=uuid.uuid4(),
                poll_query="SELECT 1",
                condition_expression=None,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "triggers_paused"
        assert not session.added


# ---------------------------------------------------------------------------
# Cron validation + next-fire computation (relocated from cron_scheduler.py)
# ---------------------------------------------------------------------------


class TestCronValidation:
    def test_valid_expression_returns_none(self) -> None:
        assert ch.validate_cron_expression("*/30 * * * * *") is None

    def test_invalid_expression_returns_error_message(self) -> None:
        err = ch.validate_cron_expression("not a cron expression")
        assert isinstance(err, str)
        assert err

    def test_invalid_timezone_returns_error_message(self) -> None:
        err = ch.validate_cron_expression("*/30 * * * * *", timezone="Mars/Olympus")
        assert "Invalid timezone" in err


class TestComputeNextFire:
    def test_returns_future_timezone_aware_utc(self) -> None:
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        nf = ch.compute_next_fire("0 9 * * *", after=base)
        assert nf.tzinfo == UTC
        assert nf > base

    def test_naive_after_interpreted_as_utc(self) -> None:
        nf = ch.compute_next_fire("0 9 * * *", after=datetime(2026, 1, 1, 8, 0, 0))
        assert nf == datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)

    def test_honors_configured_timezone(self) -> None:
        """09:00 America/New_York in January (UTC-5) == 14:00 UTC — a non-UTC
        trigger must not fire on UTC schedules."""
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        nf = ch.compute_next_fire("0 9 * * *", after=base, timezone="America/New_York")
        assert nf == datetime(2026, 1, 1, 14, 0, 0, tzinfo=UTC)

    def test_compute_next_send_returns_next_cron_match(self) -> None:
        ns = ch.compute_next_send("0 9 * * *", after=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
        assert ns == datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shared helpers (relocated from cron_scheduler.py)
# ---------------------------------------------------------------------------


class TestSetRlsOrg:
    @pytest.mark.asyncio
    async def test_postgres_sets_config_via_sql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        org_id = uuid.uuid4()

        await ch._set_rls_org(session, org_id)

        stmt, params = session.executed[0]
        assert "set_config" in str(stmt)
        assert params["val"] == str(org_id)

    @pytest.mark.asyncio
    async def test_non_postgres_sets_session_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        session.info = {}
        session._get_bind.return_value.dialect.name = "sqlite"
        org_id = uuid.uuid4()

        await ch._set_rls_org(session, org_id)

        assert session.info["org_id"] == org_id


class TestCountActiveRuns:
    @pytest.mark.asyncio
    async def test_counts_active_runs_excluding_cancellation_requested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger_id = uuid.uuid4()
        session = _MockSession([_mock_result(scalar_one=4)])

        count = await ch._count_active_runs(session, trigger_id)

        assert count == 4
        sql = str(session.executed[0][0])
        assert "cancellation_requested" in sql
        # Every active status must be counted, never re-dispatched away.
        statuses = session.executed[0][0].compile().params["status_1"]
        assert set(statuses) == {"running", "pending", "awaiting_human", "claimed"}


class TestLogEvent:
    @pytest.mark.asyncio
    async def test_logs_cron_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        trigger = SimpleNamespace(id=uuid.uuid4())
        org_id = uuid.uuid4()
        run_id = uuid.uuid4()

        event = await ch._log_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="accepted",
            run_id=run_id,
        )

        assert session.added == [event]
        assert event.trigger_type == "cron"
        assert event.trigger_id == trigger.id
        assert event.organisation_id == org_id
        assert event.validation_result == "accepted"
        assert event.run_id == run_id
        assert event.error_detail is None

    @pytest.mark.asyncio
    async def test_logs_poll_event_with_error_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        trigger = SimpleNamespace(id=uuid.uuid4())

        event = await ch._log_poll_event(
            session,
            trigger=trigger,
            org_id=uuid.uuid4(),
            result="poll_error",
            error_detail="boom",
        )

        assert session.added == [event]
        assert event.trigger_type == "polling"
        assert event.validation_result == "poll_error"
        assert event.error_detail == "boom"

    @pytest.mark.asyncio
    async def test_logs_poll_event_sanitizes_error_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        trigger = SimpleNamespace(id=uuid.uuid4())

        event = await ch._log_poll_event(
            session,
            trigger=trigger,
            org_id=uuid.uuid4(),
            result="poll_error",
            error_detail="poll query failed postgresql://user:supersecret@db.example/modulo",
        )

        assert "supersecret" not in event.error_detail
        assert "<redacted>" in event.error_detail

    @pytest.mark.asyncio
    async def test_logs_cron_event_sanitizes_error_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        trigger = SimpleNamespace(id=uuid.uuid4())

        event = await ch._log_event(
            session,
            trigger=trigger,
            org_id=uuid.uuid4(),
            result="poll_error",
            error_detail="cron fire failed with Bearer tok1234567890",
        )

        assert "tok1234567890" not in event.error_detail
        assert "<redacted>" in event.error_detail


class TestIngestSaqError:
    @pytest.mark.asyncio
    async def test_ingests_via_error_tracking_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([])
        factory = MagicMock(return_value=session)
        service = MagicMock()
        service.ingest = AsyncMock()

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock) as set_rls,
            patch("modulo.core.error_tracking.ErrorIngestionService", return_value=service),
        ):
            await ch._ingest_saq_error(
                session,
                ORG,
                function="fire_due_triggers",
                message="enqueue failed",
                context={"trigger_id": str(TRIGGER_A)},
            )

        set_rls.assert_awaited_once_with(session, ORG)
        service.ingest.assert_awaited_once()
        payload = service.ingest.await_args.args[2]
        assert payload["source"] == "saq"
        assert payload["context_json"]["function"] == "fire_due_triggers"
        assert payload["context_json"]["trigger_id"] == str(TRIGGER_A)

    @pytest.mark.asyncio
    async def test_ingest_failure_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Error ingestion must never crash the scheduler tick — but the failure must reach the logs."""
        _patch_env(monkeypatch)
        session = _MockSession([])
        factory = MagicMock(return_value=session)
        service = MagicMock()
        service.ingest = AsyncMock(side_effect=RuntimeError("ingest down"))

        with (
            caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"),
            patch.object(ch, "_open_factory", return_value=factory),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.error_tracking.ErrorIngestionService", return_value=service),
        ):
            await ch._ingest_saq_error(session, ORG, function="fire_due_triggers", message="boom")

        assert any("cron_helpers.ingest_saq_error_failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_system_org_id_skips_db_ingest(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A system error without tenant context (nil org_id) must NOT hit the
        error_events FK — it is logged and skipped."""
        _patch_env(monkeypatch)
        with (
            caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"),
            patch.object(ch, "_open_factory") as open_factory,
            patch("modulo.core.error_tracking.ErrorIngestionService") as service_cls,
        ):
            await ch._ingest_saq_error(
                ch.SYSTEM_ORG_ID,
                ch.SYSTEM_ORG_ID,
                function="fire_due_triggers",
                message="system-wide failure",
            )

        open_factory.assert_not_called()
        service_cls.assert_not_called()
        assert any("SAQ system error" in r.message for r in caplog.records)


class TestResolveSnapshotId:
    def test_uses_config_snapshot_id(self) -> None:
        snapshot_id = uuid.uuid4()
        row = SimpleNamespace(config_json={"snapshot_id": str(snapshot_id)}, pipeline_id=uuid.uuid4())
        assert ch._resolve_snapshot_id(row, {}) == snapshot_id

    def test_invalid_config_snapshot_id_returns_none(self) -> None:
        row = SimpleNamespace(config_json={"snapshot_id": "not-a-uuid"}, pipeline_id=uuid.uuid4())
        assert ch._resolve_snapshot_id(row, {}) is None

    def test_falls_back_to_latest_snapshot_for_pipeline(self) -> None:
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        row = SimpleNamespace(config_json={}, pipeline_id=pipeline_id)
        assert ch._resolve_snapshot_id(row, {pipeline_id: snapshot_id}) == snapshot_id

    def test_no_snapshot_anywhere_returns_none(self) -> None:
        row = SimpleNamespace(config_json={}, pipeline_id=uuid.uuid4())
        assert ch._resolve_snapshot_id(row, {}) is None


# ---------------------------------------------------------------------------
# fire_cron_trigger — success path + remaining skip reasons
# ---------------------------------------------------------------------------


class TestFireCronTrigger:
    @pytest.mark.asyncio
    async def test_fires_run_and_updates_last_fired_at_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger(config_json={"input_template": {"env": "prod"}})
        session = _MockSession([_lock_result(True), _trigger_result(trigger), MagicMock()])
        factory = MagicMock(return_value=session)
        run = SimpleNamespace(id=uuid.uuid4())
        event = SimpleNamespace(id=uuid.uuid4())

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock, return_value=event),
            patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, return_value=run) as create_run,
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "fired"
        assert result["run_id"] == str(run.id)
        assert result["event_id"] == str(event.id)
        assert result["input_payload"] == {"env": "prod"}
        create_run.assert_awaited_once()
        assert create_run.await_args.kwargs["trigger_type"] == "cron"
        # Default (enqueue-time advance): only last_fired_at is written, never
        # next_fire_at (that was advanced atomically in fire_due_triggers).
        final_stmt, _ = session.executed[-1]
        compiled = str(final_stmt.compile())
        assert "last_fired_at" in compiled
        assert "next_fire_at" not in compiled

    @pytest.mark.asyncio
    async def test_skips_when_trigger_busy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        session = _MockSession([_lock_result(False)])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "trigger_busy"

    @pytest.mark.asyncio
    async def test_skips_when_concurrency_limit_reached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger(max_concurrent_runs=2)
        session = _MockSession([_lock_result(True), _trigger_result(trigger)])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=2),
            patch.object(ch, "_log_event", new_callable=AsyncMock) as log_event,
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "concurrency_limit"
        assert result["active_runs"] == 2
        log_event.assert_awaited_once()
        assert log_event.await_args.kwargs["result"] == "concurrency_limit_reached"

    @pytest.mark.asyncio
    async def test_skips_when_pipeline_missing_for_auto_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        session = _MockSession([_lock_result(True), _trigger_result(trigger)])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock) as log_event,
            patch(
                "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
                return_value=None,
            ) as make_snapshot,
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=None,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "pipeline_not_found"
        make_snapshot.assert_awaited_once()
        log_event.assert_awaited_once()
        assert log_event.await_args.kwargs["result"] == "no_pipeline"

    @pytest.mark.asyncio
    async def test_advance_next_fire_at_writes_next_fire_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legacy Celery path: advance_next_fire_at=True recomputes next_fire_at."""
        _patch_env(monkeypatch)
        trigger = _make_trigger(cron_timezone="UTC")
        session = _MockSession([_lock_result(True), _trigger_result(trigger), MagicMock()])
        factory = MagicMock(return_value=session)
        run = SimpleNamespace(id=uuid.uuid4())
        event = SimpleNamespace(id=uuid.uuid4())

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock, return_value=event),
            patch.object(ch, "compute_next_fire", return_value=datetime(2026, 1, 1, tzinfo=UTC)) as compute,
            patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, return_value=run),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="0 9 * * *",
                snapshot_id=uuid.uuid4(),
                advance_next_fire_at=True,
            )

        assert result["status"] == "fired"
        compute.assert_called_once()
        assert compute.call_args.kwargs["timezone"] == "UTC"
        final_stmt, _ = session.executed[-1]
        assert "next_fire_at" in str(final_stmt.compile())


# ---------------------------------------------------------------------------
# fire_polling_trigger — full branch matrix
# ---------------------------------------------------------------------------


class TestFirePollingTrigger:
    def _session(
        self,
        trigger: MagicMock,
        connector_instance: Any,
    ) -> _MockSession:
        conn_result = MagicMock()
        conn_result.scalar_one_or_none.return_value = connector_instance
        return _MockSession([_lock_result(True), _trigger_result(trigger), conn_result, MagicMock()])

    @pytest.mark.asyncio
    async def test_skips_when_trigger_soft_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-166: a soft-deleted polling trigger (deleted_at set, active still
        True) is not loaded by the fire job — the load SELECT filters
        ``deleted_at``, so the trigger resolves to None and the job skips; the
        poll query never runs."""
        _patch_env(monkeypatch)
        trigger = _make_trigger(id=TRIGGER_POLL, deleted_at=datetime.now(UTC))
        session = _MockSession([_lock_result(True), _trigger_result(None)])
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=uuid.uuid4(),
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "trigger_inactive_or_missing"
        # The load SELECT must carry the deleted_at filter — the guard that
        # turns a soft-deleted trigger into "not found" (skip).
        load = [(s, p) for s, p in session.executed if "FROM triggers" in str(s)]
        assert load, "expected the fire-job load SELECT"
        assert "deleted_at IS NULL" in str(load[0][0])

    @pytest.mark.asyncio
    async def test_fires_run_when_condition_met(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger(config_json={"snapshot_id": str(uuid.uuid4())})
        connector_instance = SimpleNamespace(id=uuid.uuid4(), connector_type_id="github", config_json={})
        session = self._session(trigger, connector_instance)
        factory = MagicMock(return_value=session)
        connector = MagicMock()
        connector.query = AsyncMock(return_value=SimpleNamespace(records=[{"id": 1}], total=1))
        backend = MagicMock()
        backend.get_secret = AsyncMock(return_value=json.dumps({"token": "x"}))
        run = SimpleNamespace(id=uuid.uuid4())
        event = SimpleNamespace(id=uuid.uuid4())

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
            patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=connector),
            patch("modulo.core.trigger_engine.polling.evaluate_condition", return_value=True),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock, return_value=event),
            patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, return_value=run) as create_run,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=connector_instance.id,
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "fired"
        assert result["run_id"] == str(run.id)
        create_run.assert_awaited_once()
        assert create_run.await_args.kwargs["trigger_type"] == "polling"
        assert create_run.await_args.kwargs["input_payload"]["records"] == [{"id": 1}]
        # Polling fires set last_fired_at only — next_fire_at was advanced at enqueue time.
        final_stmt, _ = session.executed[-1]
        compiled = str(final_stmt.compile())
        assert "last_fired_at" in compiled
        assert "next_fire_at" not in compiled

    @pytest.mark.asyncio
    async def test_skips_when_concurrency_limit_reached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Polling fire honours max_concurrent_runs — the poll query is never
        run against the external service while the trigger is over budget."""
        _patch_env(monkeypatch)
        trigger = _make_trigger(max_concurrent_runs=2)
        session = self._session(trigger, None)
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=2),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock) as log_poll,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=uuid.uuid4(),
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "concurrency_limit"
        assert result["active_runs"] == 2
        log_poll.assert_awaited_once()
        assert log_poll.await_args.kwargs["result"] == "concurrency_limit_reached"

    @pytest.mark.asyncio
    async def test_skips_when_spend_limit_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An over-budget polling trigger is skipped BEFORE the connector query —
        it must stop hitting the external service every cycle."""
        _patch_env(monkeypatch)
        from decimal import Decimal

        trigger = _make_trigger(daily_spend_limit=Decimal("100.00"))
        cost_result = _mock_result(scalar_one=Decimal("150.00"))
        conn_result = MagicMock()
        conn_result.scalar_one_or_none.return_value = SimpleNamespace(id=uuid.uuid4())
        session = _MockSession([_lock_result(True), _trigger_result(trigger), cost_result, conn_result])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock) as log_poll,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=uuid.uuid4(),
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "spend_limit"
        assert result["daily_spend_limit"] == "100.00"
        assert result["today_cost"] == "150.00"
        log_poll.assert_awaited_once()
        assert log_poll.await_args.kwargs["result"] == "spend_limit_reached"

    @pytest.mark.asyncio
    async def test_returns_error_when_connector_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        session = self._session(trigger, None)
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock) as log_poll,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=uuid.uuid4(),
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "connector_not_found"
        log_poll.assert_awaited_once()
        assert log_poll.await_args.kwargs["result"] == "poll_error"

    @pytest.mark.asyncio
    async def test_returns_error_when_connector_init_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        connector_instance = SimpleNamespace(id=uuid.uuid4(), connector_type_id="github", config_json={})
        session = self._session(trigger, connector_instance)
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.secrets_backend.create_secrets_backend", side_effect=RuntimeError("no backend")),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock) as log_poll,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=connector_instance.id,
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "connector_init_failed"
        log_poll.assert_awaited_once()
        assert "Failed to initialise connector" in log_poll.await_args.kwargs["error_detail"]

    @pytest.mark.asyncio
    async def test_returns_query_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        connector_instance = SimpleNamespace(id=uuid.uuid4(), connector_type_id="github", config_json={})
        session = self._session(trigger, connector_instance)
        factory = MagicMock(return_value=session)
        connector = MagicMock()
        connector.query = AsyncMock(side_effect=TimeoutError("timed out"))
        backend = MagicMock()
        backend.get_secret = AsyncMock(return_value="{}")

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
            patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=connector),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock) as log_poll,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=connector_instance.id,
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "query_timeout"
        log_poll.assert_awaited_once()
        assert "timed out after 60s" in log_poll.await_args.kwargs["error_detail"]

    @pytest.mark.asyncio
    async def test_returns_error_when_query_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        connector_instance = SimpleNamespace(id=uuid.uuid4(), connector_type_id="github", config_json={})
        session = self._session(trigger, connector_instance)
        factory = MagicMock(return_value=session)
        connector = MagicMock()
        connector.query = AsyncMock(side_effect=RuntimeError("boom"))
        backend = MagicMock()
        backend.get_secret = AsyncMock(return_value="{}")

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
            patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=connector),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock),
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=connector_instance.id,
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "query_failed"
        assert result["error"] == "boom"

    @pytest.mark.asyncio
    async def test_returns_no_match_when_condition_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        connector_instance = SimpleNamespace(id=uuid.uuid4(), connector_type_id="github", config_json={})
        session = self._session(trigger, connector_instance)
        factory = MagicMock(return_value=session)
        connector = MagicMock()
        connector.query = AsyncMock(return_value=SimpleNamespace(records=[], total=0))
        backend = MagicMock()
        backend.get_secret = AsyncMock(return_value="{}")

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
            patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=connector),
            patch("modulo.core.trigger_engine.polling.evaluate_condition", return_value=False),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock) as log_poll,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=connector_instance.id,
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "no_match"
        log_poll.assert_awaited_once()
        assert log_poll.await_args.kwargs["result"] == "no_match"

    @pytest.mark.asyncio
    async def test_returns_error_when_condition_eval_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        connector_instance = SimpleNamespace(id=uuid.uuid4(), connector_type_id="github", config_json={})
        session = self._session(trigger, connector_instance)
        factory = MagicMock(return_value=session)
        connector = MagicMock()
        connector.query = AsyncMock(return_value=SimpleNamespace(records=[], total=0))
        backend = MagicMock()
        backend.get_secret = AsyncMock(return_value="{}")

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
            patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=connector),
            patch("modulo.core.trigger_engine.polling.evaluate_condition", side_effect=RuntimeError("bad expr")),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock) as log_poll,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=connector_instance.id,
                poll_query="issues",
                condition_expression="broken",
            )

        assert result["status"] == "error"
        assert result["reason"] == "condition_eval_failed"
        log_poll.assert_awaited_once()
        assert log_poll.await_args.kwargs["result"] == "poll_error"

    @pytest.mark.asyncio
    async def test_invalid_snapshot_id_falls_back_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        trigger = _make_trigger(config_json={"snapshot_id": "not-a-uuid"})
        connector_instance = SimpleNamespace(id=uuid.uuid4(), connector_type_id="github", config_json={})
        session = self._session(trigger, connector_instance)
        factory = MagicMock(return_value=session)
        connector = MagicMock()
        connector.query = AsyncMock(return_value=SimpleNamespace(records=[], total=0))
        backend = MagicMock()
        backend.get_secret = AsyncMock(return_value="{}")
        run = SimpleNamespace(id=uuid.uuid4())
        event = SimpleNamespace(id=uuid.uuid4())

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
            patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=connector),
            patch("modulo.core.trigger_engine.polling.evaluate_condition", return_value=True),
            patch.object(ch, "_log_poll_event", new_callable=AsyncMock, return_value=event),
            patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, return_value=run) as create_run,
        ):
            result = await ch.fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=connector_instance.id,
                poll_query="issues",
                condition_expression=None,
            )

        assert result["status"] == "fired"
        assert create_run.await_args.kwargs["snapshot_id"] == uuid.UUID(int=0)


class TestClearReportFailureCounter:
    @pytest.mark.asyncio
    async def test_deletes_failure_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        redis_client = AsyncMock()

        await ch._clear_report_failure_counter(redis_client, REPORT)

        redis_client.delete.assert_awaited_once_with(ch._report_failure_counter_key(REPORT))

    @pytest.mark.asyncio
    async def test_swallows_redis_errors(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        redis_client.delete = AsyncMock(side_effect=RuntimeError("redis down"))

        with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
            await ch._clear_report_failure_counter(redis_client, REPORT)

        assert any("clear_report_failure_counter failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------


class TestCreateRunPauseClassification:
    """Table test: for every known run trigger_type, the create_run gate's
    pause classification is correct — webhook/cron/polling/agent_signal are
    pause-eligible when trigger_id is set; manual/correction are exempt;
    trigger_id=None bypasses the gate entirely."""

    @pytest.mark.parametrize(
        ("trigger_type", "pause_eligible"),
        [
            ("manual", False),
            ("webhook", True),
            ("cron", True),
            ("polling", True),
            ("agent_signal", True),
            ("correction", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_known_trigger_types_classified(
        self, monkeypatch: pytest.MonkeyPatch, trigger_type: str, pause_eligible: bool
    ) -> None:
        _patch_env(monkeypatch)
        from modulo.core.exceptions import TriggersPausedError
        from modulo.db.crud.run import create_run

        run_number_result = MagicMock()
        run_number_result.scalar_one.return_value = 0
        session = _MockSession([run_number_result])
        with patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True):
            if pause_eligible:
                with pytest.raises(TriggersPausedError):
                    await create_run(
                        session,
                        org_id=ORG,
                        pipeline_id=uuid.uuid4(),
                        snapshot_id=uuid.uuid4(),
                        trigger_type=trigger_type,
                        input_payload={},
                        trigger_id=TRIGGER_A,
                    )
            else:
                run = await create_run(
                    session,
                    org_id=ORG,
                    pipeline_id=uuid.uuid4(),
                    snapshot_id=uuid.uuid4(),
                    trigger_type=trigger_type,
                    input_payload={},
                    trigger_id=TRIGGER_A,
                )
                assert run.id is not None

    @pytest.mark.asyncio
    async def test_trigger_id_none_bypasses_gate_even_when_paused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pause-eligible trigger_type with trigger_id=None (a run created
        without a Trigger row — scheduled reports / variants) bypasses the gate."""
        _patch_env(monkeypatch)
        from modulo.db.crud.run import create_run

        run_number_result = MagicMock()
        run_number_result.scalar_one.return_value = 0
        session = _MockSession([run_number_result])
        with patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=True):
            run = await create_run(
                session,
                org_id=ORG,
                pipeline_id=uuid.uuid4(),
                snapshot_id=uuid.uuid4(),
                trigger_type="cron",
                input_payload={},
                trigger_id=None,
            )
            assert run.id is not None


class _StatsRedis:
    """In-memory redis double for the dispatcher_reconcile shared-stats key."""

    def __init__(self) -> None:
        self._blob: bytes | None = None

    async def get(self, _key: str) -> bytes | None:
        return self._blob

    async def set(self, _key: str, value: str) -> None:
        self._blob = value.encode()


class TestDispatcherReconcileSharedStats:
    """write/read_dispatcher_reconcile_stats — the shared Redis store the WEB
    process health check reads (the in-process stats dict is worker-local)."""

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self) -> None:
        r = _StatsRedis()
        await ch.write_dispatcher_reconcile_stats(r, {"scanned": 2, "repaired": 1, "skipped": 1})
        stats = await ch.read_dispatcher_reconcile_stats(r)
        assert stats is not None
        assert stats["scanned"] == 2
        assert stats["repaired"] == 1
        assert stats["skipped"] == 1
        assert stats["last_run_at"]

    @pytest.mark.asyncio
    async def test_read_before_any_write_is_none(self) -> None:
        r = _StatsRedis()
        assert await ch.read_dispatcher_reconcile_stats(r) is None

    @pytest.mark.asyncio
    async def test_read_unparsable_payload_is_none(self) -> None:
        r = _StatsRedis()
        await r.set(ch.DISPATCHER_RECONCILE_STATS_KEY, "not json {")
        assert await ch.read_dispatcher_reconcile_stats(r) is None

    @pytest.mark.asyncio
    async def test_write_failure_never_raises(self) -> None:
        class _BrokenRedis:
            async def set(self, _key: str, _value: str) -> None:
                raise RuntimeError("redis down")

        assert (await ch.write_dispatcher_reconcile_stats(_BrokenRedis(), {"scanned": 0})) is None


# ---------------------------------------------------------------------------
# Review PR #982 findings 1-3: catch-up marker dedup, skip-attempt recording,
# atomic claim, TTL coverage.
# ---------------------------------------------------------------------------


class TestCatchupReviewFindings:
    """Regression tests for the PR #982 review findings on the missed-fire
    catch-up design."""

    @pytest.mark.asyncio
    async def test_normal_enqueue_writes_catchup_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finding 1: a NORMAL-path successful enqueue writes the catch-up
        marker for the consumed epoch, so the catch-up scan on the NEXT tick
        does not re-fire a legitimately-enqueued fire."""
        _patch_env(monkeypatch)
        now_val = datetime.now(UTC)
        due = now_val - timedelta(seconds=1)  # normal loop fires this due epoch
        session = _MockSession(
            [
                _org_result([ORG]),
                _pause_result(ORG),
                _rows_result([_cron_row(TRIGGER_A, cron_expression="0 4 * * *", next_fire_at=due, last_fired_at=None)]),
                _mock_result(fetchone=("advanced-id",)),
                _rows_result([]),
                _rows_result([]),
                _rows_result([]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.set.return_value = True

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-1"),
        ):
            redis_cls.from_url.return_value = redis_client
            summary = await ch.fire_due_triggers()

        assert summary["cron_enqueued"] == 1
        # The normal path wrote a catch-up marker keyed by the consumed epoch.
        marker_calls = [c for c in redis_client.set.await_args_list if "catchup" in str(c.args)]
        assert marker_calls, "normal enqueue must write the catch-up marker"
        marker_key = str(marker_calls[0].args[0])
        assert f"{ch._CATCHUP_MARKER_PREFIX}:{TRIGGER_A}:" in marker_key
        # The marker epoch equals the consumed epoch (pre-advance next_fire_at).
        assert marker_key.endswith(str(int(due.timestamp())))

    def test_marker_ttl_covers_worst_case_inflight(self) -> None:
        """Finding 3: the marker TTL covers the worst-case fire-job in-flight
        window (timeout x (retries+1)) so a pending job cannot outlive its
        marker and get re-fired."""
        assert ch._CATCHUP_MARKER_TTL >= ch.FIRE_JOB_TIMEOUT * (ch.FIRE_JOB_RETRIES + 1)

    @pytest.mark.asyncio
    async def test_claim_is_atomic_set_nx(self) -> None:
        """Finding 3: the catch-up marker is claimed with a single atomic
        SET NX EX - the first caller wins, concurrent callers lose."""
        redis_client = AsyncMock()
        redis_client.set.return_value = True  # first claim wins
        won = await ch._claim_catchup_marker(redis_client, TRIGGER_A, 1234)
        assert won is True
        call = redis_client.set.await_args
        assert call.kwargs.get("nx") is True, "claim must use SET NX (atomic)"
        assert call.kwargs.get("ex") == ch._CATCHUP_MARKER_TTL
        # second concurrent claim loses
        redis_client.set.return_value = None  # SET NX returns None when key exists
        lost = await ch._claim_catchup_marker(redis_client, TRIGGER_A, 1234)
        assert lost is False

    @pytest.mark.asyncio
    async def test_claim_fails_open_on_redis_error(self) -> None:
        """Finding 3: a Redis failure during the claim fails OPEN (the fire is
        allowed) - the enqueue itself fails loudly if Redis is really down."""
        redis_client = AsyncMock()
        redis_client.set.side_effect = ConnectionError("redis down")
        won = await ch._claim_catchup_marker(redis_client, TRIGGER_A, 1234)
        assert won is True

    @pytest.mark.asyncio
    async def test_concurrency_skip_records_last_fired_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finding 2: a concurrency-limited fire records the attempt
        (last_fired_at) so the catch-up scan never re-fires it forever."""
        _patch_env(monkeypatch)
        trigger = _make_trigger(max_concurrent_runs=2)
        session = _MockSession([_lock_result(True), _trigger_result(trigger)])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=2),
            patch.object(ch, "_log_event", new_callable=AsyncMock),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "concurrency_limit"
        updates = [p for s, p in session.executed if "UPDATE triggers" in str(s) and "last_fired_at" in str(s)]
        assert updates, "concurrency skip must record last_fired_at (attempted state)"

    @pytest.mark.asyncio
    async def test_spend_skip_records_last_fired_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finding 2: a spend-limited fire records the attempt so the catch-up
        scan never re-fires it forever."""
        _patch_env(monkeypatch)
        trigger = _make_trigger(daily_spend_limit=10.0)
        # Execution order inside fire_cron_trigger: advisory lock -> trigger
        # select -> daily-cost SUM. The cost result must be the LAST slot so the
        # lock and trigger reads consume their own results first.
        fake_result = MagicMock()
        fake_result.scalar_one.return_value = 50.0
        session = _MockSession([_lock_result(True), _trigger_result(trigger), fake_result])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock),
            patch(
                "modulo.core.cost_controller.created_at_day_start",
                return_value=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            patch(
                "modulo.db.crud.run.Run",
                MagicMock(),
            ),
            patch.object(ch, "select"),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=uuid.uuid4(),
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "spend_limit"
        updates = [p for s, p in session.executed if "UPDATE triggers" in str(s) and "last_fired_at" in str(s)]
        assert updates, "spend skip must record last_fired_at (attempted state)"

    @pytest.mark.asyncio
    async def test_pipeline_missing_skip_records_last_fired_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finding 2: a pipeline-missing fire records the attempt so the
        catch-up scan never re-fires it forever."""
        _patch_env(monkeypatch)
        trigger = _make_trigger()
        session = _MockSession([_lock_result(True), _trigger_result(trigger)])
        factory = MagicMock(return_value=session)

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_active_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_log_event", new_callable=AsyncMock),
            patch(
                "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await ch.fire_cron_trigger(
                trigger_id=trigger.id,
                org_id=ORG,
                pipeline_id=trigger.pipeline_id,
                cron_expression="* * * * *",
                snapshot_id=None,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "pipeline_not_found"
        updates = [p for s, p in session.executed if "UPDATE triggers" in str(s) and "last_fired_at" in str(s)]
        assert updates, "pipeline-missing skip must record last_fired_at (attempted state)"


class TestGetSystemEngine:
    def test_creates_system_engine_with_ssl_disabled(self) -> None:
        mock_settings = _settings(modulo_system_database_url="postgresql+asyncpg://sys:pass@db:5432/modulo")
        ch._SYSTEM_ENGINE = None  # reset singleton
        try:
            with (
                patch.object(ch, "get_settings", return_value=mock_settings),
                patch("sqlalchemy.ext.asyncio.create_async_engine") as create_engine,
            ):
                result = ch._get_system_engine()

            assert result is create_engine.return_value
            create_engine.assert_called_once()
            _, kwargs = create_engine.call_args
            assert kwargs["connect_args"] == {"ssl": False, "statement_cache_size": 0}
        finally:
            ch._SYSTEM_ENGINE = None

    def test_falls_back_to_regular_engine_when_url_empty(self) -> None:
        regular_engine = MagicMock()
        mock_settings = _settings(modulo_system_database_url="")
        ch._SYSTEM_ENGINE = None  # reset singleton
        try:
            with (
                patch.object(ch, "get_settings", return_value=mock_settings),
                patch.object(ch, "_get_engine", return_value=regular_engine),
            ):
                result = ch._get_system_engine()

            assert result is regular_engine
        finally:
            ch._SYSTEM_ENGINE = None

    def test_logs_warning_and_uses_app_engine_when_unset(self) -> None:
        regular_engine = MagicMock()
        mock_settings = _settings(modulo_system_database_url="")
        ch._SYSTEM_ENGINE = None  # reset singleton
        warnings: list[tuple[object, object]] = []
        try:
            with (
                patch.object(ch, "get_settings", return_value=mock_settings),
                patch.object(ch, "_get_engine", return_value=regular_engine),
                patch.object(ch._log, "warning", lambda msg, extra=None: warnings.append((msg, extra))),
            ):
                result = ch._get_system_engine()

            assert result is regular_engine
            assert len(warnings) == 1
            msg, extra = warnings[0]
            assert msg == "cron_helpers.system_engine_fallback"
            assert "MODULO_SYSTEM_DATABASE_URL not set" in extra["reason"]
        finally:
            ch._SYSTEM_ENGINE = None
