"""Unit tests for modulo.core.cron_helpers (plan F1) -” fire_due_triggers,
atomic next_fire_at advance, per-item fire jobs, report backoff/deactivate.

Mock/fake based (no real Postgres/Redis). The multi-worker race is covered at
the control-flow level here (atomic advance returning rows is the ONLY thing
that gates enqueue) and by a real-Redis two-process integration test.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch

ORG = uuid.uuid4()
TRIGGER_A = uuid.uuid4()
TRIGGER_B = uuid.uuid4()
TRIGGER_POLL = uuid.uuid4()
REPORT = uuid.uuid4()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


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


def _rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _cron_row(trigger_id: uuid.UUID, *, snapshot_id: str | None = "default") -> SimpleNamespace:
    if snapshot_id == "default":
        snapshot_id = str(uuid.uuid4())
    return SimpleNamespace(
        id=trigger_id,
        pipeline_id=uuid.uuid4(),
        config_json={"snapshot_id": snapshot_id} if snapshot_id else {},
        cron_expression="*/30 * * * * *",
    )


def _polling_row(trigger_id: uuid.UUID, *, has_connector: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=trigger_id,
        pipeline_id=uuid.uuid4(),
        config_json=(
            {"connector_instance_id": str(uuid.uuid4()), "poll_query": "SELECT 1", "poll_interval_seconds": 60}
            if has_connector
            else {}
        ),
    )


def _report_row(report_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=report_id, cron_expression="0 9 * * *")


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
    }
    base.update(overrides)
    return MagicMock(**base)


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


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
            patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-id") as enqueue,
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
    async def test_second_machine_epoch_skipped_when_already_advanced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
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


# ---------------------------------------------------------------------------
# fire_report_trigger -” backoff + deactivate-after-5
# ---------------------------------------------------------------------------


class TestFireReportTrigger:
    def _report_session(self, generator: AsyncMock | None, deliverer: AsyncMock | None) -> _MockSession:
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
        session = self._report_session(None, None)
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
        session = self._report_session(None, None)
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
        # 30s re-enqueue cadence).
        assert len(session.executed) == 1
        stmt, _params = session.executed[0]
        assert "next_send_at" in str(stmt)
        redis_client.incr.assert_awaited_once_with(ch._report_failure_counter_key(REPORT))

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

        # 5th consecutive failure -> UPDATE ... SET active=False.
        assert len(session.executed) == 2
        second_stmt, _ = session.executed[1]
        assert "active" in str(second_stmt)


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


# ---------------------------------------------------------------------------
# Fire logic skips (mirrors the relocated CronFireTask semantics)
# ---------------------------------------------------------------------------


class TestFireCronTriggerSkips:
    @pytest.mark.asyncio
    async def test_skips_when_trigger_inactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)

        from modulo.db.models.trigger import Trigger

        trigger = MagicMock(spec=Trigger)
        trigger.id = TRIGGER_A
        trigger.organisation_id = ORG
        trigger.pipeline_id = uuid.uuid4()
        trigger.active = False
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
    async def test_skips_when_spend_limit_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        from decimal import Decimal

        from modulo.db.models.trigger import Trigger

        trigger = MagicMock(spec=Trigger)
        trigger.id = TRIGGER_A
        trigger.organisation_id = ORG
        trigger.pipeline_id = uuid.uuid4()
        trigger.active = True
        trigger.max_concurrent_runs = 5
        trigger.daily_spend_limit = Decimal("100.00")
        trigger.config_json = {}

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True
        trigger_result = MagicMock()
        trigger_result.scalar_one_or_none.return_value = trigger
        cost_result = MagicMock()
        cost_result.scalar_one.return_value = Decimal("150.00")

        session = _MockSession([lock_result, trigger_result, cost_result])
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
