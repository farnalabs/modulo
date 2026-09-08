"""Residual unit coverage for modulo.core.cron_helpers (FAR-619).

Edge branches the existing suites leave uncovered: cron-iteration timezone/DST
boundaries, the fire-job skip paths (backpressure / paused / shared-budget
unavailable), report formatter/deliverer selection, the catch-up scan helpers,
per-row scan error branches, and the dispatcher-reconcile repair/sweep/telemetry
helpers.

Mock/fake based (no real Postgres/Redis), mirroring the conventions of
``tests/unit/cron_helpers/test_cron_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.connectors._rate_bucket import SharedBudgetUnavailableError
from modulo.core import cron_helpers as ch
from modulo.core.exceptions import TriggersPausedError

ORG = uuid.uuid4()
TRIGGER_A = uuid.uuid4()
REPORT_ID = uuid.uuid4()
PIPELINE = uuid.uuid4()

ONGOING_CAP = ch.ONGOING_MAX_ENQUEUE_PER_TICK


# ---------------------------------------------------------------------------
# Session doubles (mirrors tests/unit/cron_helpers/test_cron_helpers.py)
# ---------------------------------------------------------------------------


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Async session double returning a fixed sequence of execute() results."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self._results = list(results or [])
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

    def begin_nested(self) -> _MockBegin:
        return _MockBegin()

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        return self._get_bind()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        if "set_config" in str(stmt):
            return MagicMock()
        if not self._results:
            return MagicMock()
        return self._results.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def get(self, model: Any, key: Any) -> Any:
        return None


def _mock_result(**kwargs: Any) -> MagicMock:
    result = MagicMock()
    for name, value in kwargs.items():
        getattr(result, name).return_value = value
    return result


def _lock_result(acquired: bool) -> MagicMock:
    return _mock_result(scalar_one=acquired)


def _trigger_result(trigger: Any) -> MagicMock:
    return _mock_result(scalar_one_or_none=trigger)


def _snapshot_rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.__iter__.return_value = iter(rows)
    return r


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "fernet_key": "b" * 44,
        "modulo_telemetry_enabled": False,
    }
    base.update(overrides)
    return MagicMock(**base)


def _make_trigger(**overrides: object) -> MagicMock:
    from modulo.db.models.trigger import Trigger

    defaults: dict[str, object] = {
        "id": TRIGGER_A,
        "organisation_id": ORG,
        "pipeline_id": PIPELINE,
        "active": True,
        "deleted_at": None,
        "max_concurrent_runs": 5,
        "daily_spend_limit": None,
        "config_json": {},
        "cron_timezone": None,
        "run_kind": "run",
        "eval_suite_id": None,
    }
    defaults.update(overrides)
    trigger = MagicMock(spec=Trigger)
    for key, value in defaults.items():
        setattr(trigger, key, value)
    return trigger


def _factory_for(session: _MockSession) -> MagicMock:
    return MagicMock(return_value=session)


def _redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def _org_not_paused() -> Any:
    with patch.object(ch, "org_is_paused", new_callable=AsyncMock, return_value=False):
        yield


# ---------------------------------------------------------------------------
# compute_next_fire / compute_next_send — timezone, DST, defensive type arms
# ---------------------------------------------------------------------------


def test_compute_next_fire_converts_timezone_to_utc():
    """A trigger pinned to a non-UTC zone stores next_fire_at in UTC."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    next_dt = ch.compute_next_fire("0 0 * * * *", after=base, timezone="America/New_York")
    assert next_dt.tzinfo == UTC
    assert next_dt.hour == 4  # 00:00 New York (EDT) == 04:00 UTC


def test_compute_next_fire_skips_nonexistent_dst_hour():
    """A daily 02:30 America/New_York cron lands at 03:30 local on the
    spring-forward day (2026-03-08) — the missing hour is skipped, never fired
    twice or shifted a full day."""
    base = datetime(2026, 3, 8, 0, 0, tzinfo=UTC)  # 19:00 Mar 7 in New York (EST)
    next_dt = ch.compute_next_fire("30 2 * * * *", after=base, timezone="America/New_York")
    local = next_dt.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    assert local.hour == 3
    assert next_dt.tzinfo == UTC


def test_compute_next_fire_uses_zone_offset_for_winter_dates():
    base = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    next_dt = ch.compute_next_fire("0 0 * * * *", after=base, timezone="America/New_York")
    assert next_dt.hour == 5  # 00:00 New York (EST) == 05:00 UTC


def test_compute_next_fire_type_error_on_non_datetime(monkeypatch: pytest.MonkeyPatch):
    class _BadCron:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_next(self, *args: Any, **kwargs: Any) -> Any:
            return "not-a-datetime"

    monkeypatch.setattr(ch, "croniter", _BadCron)
    with pytest.raises(TypeError, match="croniter returned unexpected type"):
        ch.compute_next_fire("*/30 * * * * *", after=datetime.now(UTC))


def test_compute_next_fire_attaches_tz_to_naive_result(monkeypatch: pytest.MonkeyPatch):
    naive = datetime(2026, 6, 2, 0, 0)

    class _NaiveCron:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_next(self, *args: Any, **kwargs: Any) -> Any:
            return naive

    monkeypatch.setattr(ch, "croniter", _NaiveCron)
    next_dt = ch.compute_next_fire("0 0 * * * *", after=datetime.now(UTC), timezone="America/New_York")
    assert next_dt.tzinfo == UTC


def test_compute_next_send_type_error_on_non_datetime(monkeypatch: pytest.MonkeyPatch):
    class _BadCron:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_next(self, *args: Any, **kwargs: Any) -> Any:
            return 42

    monkeypatch.setattr(ch, "croniter", _BadCron)
    with pytest.raises(TypeError, match="croniter returned unexpected type"):
        ch.compute_next_send("0 9 * * *", after=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Trigger-event builders
# ---------------------------------------------------------------------------


def test_build_trigger_event_sanitize_false_preserves_raw_detail():
    trigger = SimpleNamespace(id=TRIGGER_A)
    event = ch._build_trigger_event(
        org_id=ORG,
        trigger=trigger,
        trigger_type="ongoing",
        payload_salt="ongoing:x:failed",
        result="failed",
        run_id=None,
        error_detail="raw password=hunter2 detail",
        sanitize=False,
    )
    assert event.error_detail == "raw password=hunter2 detail"


async def test_log_ongoing_event_delegates_with_ongoing_salt():
    session = _MockSession()
    trigger = SimpleNamespace(id=TRIGGER_A)
    with patch.object(ch, "_add_trigger_event", new_callable=AsyncMock) as add_event:
        await ch._log_ongoing_event(session, trigger=trigger, org_id=ORG, result="accepted", run_id=None)
    add_event.assert_awaited_once()
    kwargs = add_event.await_args.kwargs
    assert kwargs["trigger_type"] == "ongoing"
    assert kwargs["payload_salt"] == f"ongoing:{TRIGGER_A}:accepted"
    assert kwargs["sanitize"] is False


# ---------------------------------------------------------------------------
# _ingest_saq_error — cancellation + failure isolation
# ---------------------------------------------------------------------------


async def test_ingest_saq_error_reraises_cancellation():
    session = _MockSession()

    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch("modulo.core.error_tracking.ErrorIngestionService") as svc_cls,
    ):
        svc_cls.return_value.ingest = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await ch._ingest_saq_error(session, ORG, function="fire_due_triggers", message="boom")


async def test_ingest_saq_error_swallows_ingest_failure(caplog):
    session = _MockSession()

    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch("modulo.core.error_tracking.ErrorIngestionService") as svc_cls,
        caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"),
    ):
        svc_cls.return_value.ingest = AsyncMock(side_effect=RuntimeError("db down"))
        await ch._ingest_saq_error(session, ORG, function="fire_due_triggers", message="boom")
    assert any("ingest_saq_error_failed" in m for m in caplog.messages)


async def test_ingest_saq_error_skips_nil_org(caplog):
    session = _MockSession()
    with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
        await ch._ingest_saq_error(session, ch.SYSTEM_ORG_ID, function="f", message="system error")
    assert any("no tenant context" in m for m in caplog.messages)
    assert not session.added


async def test_write_dispatcher_reconcile_stats_reraises_cancellation():
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch.write_dispatcher_reconcile_stats(redis_client, {"scanned": 1})


# ---------------------------------------------------------------------------
# _spend_limit_skip / _auto_create_snapshot / _run_poll_query
# ---------------------------------------------------------------------------


async def test_spend_limit_skip_none_when_under_limit():
    session = _MockSession([_mock_result(scalar_one=5.0)])
    trigger = _make_trigger()
    result = await ch._spend_limit_skip(session, trigger, ORG, 10.0)
    assert result is None


async def test_spend_limit_skip_returns_skip_when_over_limit():
    session = _MockSession([_mock_result(scalar_one=15.0), MagicMock()])
    trigger = _make_trigger()
    result = await ch._spend_limit_skip(session, trigger, ORG, 10.0)
    assert result is not None
    assert result["reason"] == "spend_limit"
    assert result["today_cost"] == "15.0"


async def test_auto_create_snapshot_returns_new_id():
    session = _MockSession()
    trigger = _make_trigger()
    snapshot_id = uuid.uuid4()
    with patch(
        "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
        new=AsyncMock(return_value=SimpleNamespace(id=snapshot_id)),
    ):
        got = await ch._auto_create_snapshot(session, trigger, ORG, PIPELINE)
    assert got == snapshot_id


async def test_run_poll_query_reraises_shared_budget_unavailable():
    session = _MockSession()
    connector = MagicMock()
    connector.query = AsyncMock(side_effect=SharedBudgetUnavailableError("budget down"))
    with pytest.raises(SharedBudgetUnavailableError):
        await ch._run_poll_query(session, connector, _make_trigger(), ORG, TRIGGER_A, "SELECT 1")


# ---------------------------------------------------------------------------
# fire_cron_trigger — backpressure + paused-race skip
# ---------------------------------------------------------------------------


async def test_fire_cron_trigger_backpressure_skips():
    """FAR-604: a pipeline pending queue over the depth/age limits skips the
    fire (skip-not-defer — last_fired_at stamped) instead of enqueueing."""
    trigger = _make_trigger()
    session = _MockSession([_lock_result(True), _trigger_result(trigger), _mock_result(scalar_one=0), MagicMock()])
    with (
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.run_admission.evaluate_backpressure",
            new_callable=AsyncMock,
            return_value=(True, "pending queue depth 120"),
        ),
    ):
        outcome = await ch.fire_cron_trigger(
            trigger_id=TRIGGER_A,
            org_id=ORG,
            pipeline_id=PIPELINE,
            cron_expression="*/30 * * * * *",
            snapshot_id=uuid.uuid4(),
            factory=_factory_for(session),
        )
    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "backpressure"
    assert outcome["detail"] == "pending queue depth 120"


async def test_fire_cron_trigger_paused_race_backstop_skips():
    trigger = _make_trigger()
    session = _MockSession([_lock_result(True), _trigger_result(trigger), _mock_result(scalar_one=0), MagicMock()])
    with (
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.run_admission.evaluate_backpressure", new_callable=AsyncMock, return_value=(False, "")),
        patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, side_effect=TriggersPausedError()),
    ):
        outcome = await ch.fire_cron_trigger(
            trigger_id=TRIGGER_A,
            org_id=ORG,
            pipeline_id=PIPELINE,
            cron_expression="*/30 * * * * *",
            snapshot_id=uuid.uuid4(),
            factory=_factory_for(session),
        )
    assert outcome == {"status": "skipped", "reason": ch.PAUSE_SKIP_REASON}


# ---------------------------------------------------------------------------
# Polling fire job — backpressure gate + paused race + shared-budget outage
# ---------------------------------------------------------------------------


async def test_polling_pre_fire_gate_backpressure_skips():
    trigger = _make_trigger()
    session = _MockSession([_lock_result(True), _trigger_result(trigger), _mock_result(scalar_one=0)])
    with patch(
        "modulo.core.run_admission.evaluate_backpressure",
        new_callable=AsyncMock,
        return_value=(True, "pending age > limit"),
    ):
        got_trigger, skip = await ch._polling_pre_fire_gate(session, trigger_id=TRIGGER_A, org_id=ORG)
    assert got_trigger is None
    assert skip == {"status": "skipped", "reason": "backpressure", "detail": "pending age > limit"}


async def test_run_poll_fire_paused_race_backstop_skips():
    trigger = _make_trigger()
    session = _MockSession([_mock_result(scalar_one_or_none=SimpleNamespace(records=[{"a": 1}], total=1))])
    connector = MagicMock()
    connector.query = AsyncMock(return_value=SimpleNamespace(records=[{"a": 1}], total=1))
    with (
        patch("modulo.core.trigger_engine.polling.evaluate_condition", return_value=True),
        patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, side_effect=TriggersPausedError()),
    ):
        outcome = await ch._run_poll_fire(
            session,
            trigger=trigger,
            connector=connector,
            org_id=ORG,
            trigger_id=TRIGGER_A,
            pipeline_id=PIPELINE,
            poll_query="SELECT 1",
            condition_expression=None,
        )
    assert outcome == {"status": "skipped", "reason": ch.PAUSE_SKIP_REASON}


async def test_fire_polling_trigger_shared_budget_outage_resets_epoch():
    """FAR-442: a shared-budget outage during the poll query fails closed and
    RESETS next_fire_at so the next tick re-enqueues the epoch (never dropped)."""
    trigger = _make_trigger(config_json={"connector_instance_id": str(uuid.uuid4()), "poll_query": "SELECT 1"})
    session = _MockSession(
        [
            _lock_result(True),
            _trigger_result(trigger),
            _mock_result(scalar_one=0),
            _mock_result(scalar_one_or_none=SimpleNamespace(id=uuid.uuid4())),
            MagicMock(),  # next_fire_at reset UPDATE
        ]
    )
    connector = MagicMock()
    connector.query = AsyncMock(side_effect=SharedBudgetUnavailableError("redis down"))
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.run_admission.evaluate_backpressure", new_callable=AsyncMock, return_value=(False, "")),
        patch.object(ch, "_build_polling_connector", new_callable=AsyncMock, return_value=(connector, _redis())),
        patch("modulo.core.trigger_engine.polling._close_polling_resources", new_callable=AsyncMock),
    ):
        outcome = await ch.fire_polling_trigger(
            trigger_id=TRIGGER_A,
            org_id=ORG,
            pipeline_id=PIPELINE,
            connector_instance_id=uuid.uuid4(),
            poll_query="SELECT 1",
            condition_expression=None,
        )
    assert outcome["status"] == "error"
    assert outcome["reason"] == "shared_budget_unavailable"
    # The third execute call (after lock/trigger/count/instance) is the reset.
    assert len(session.executed) >= 5


# ---------------------------------------------------------------------------
# fire_report_trigger — formatter / deliverer selection + cancellation
# ---------------------------------------------------------------------------


def _report_row() -> SimpleNamespace:
    return SimpleNamespace(
        id=REPORT_ID,
        report_type="ops",
        config_json={},
        recipient_config={"email": "ops@example.com"},
        active=True,
    )


async def test_fire_report_trigger_uses_formatter_and_deliverer():
    session = _MockSession([_trigger_result(_report_row()), MagicMock()])
    redis_client = _redis()
    formatted = {"body": "formatted"}
    delivered = [{"status": "sent"}]
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis") as redis_cls,
        patch("modulo.core.reports.scheduler.get_generator", return_value=AsyncMock(return_value={"raw": 1})),
        patch("modulo.core.reports.scheduler.get_formatter", return_value=lambda data: formatted),
        patch("modulo.core.reports.scheduler.get_deliverer", return_value=AsyncMock(return_value=delivered)),
    ):
        redis_cls.from_url.return_value = redis_client
        outcome = await ch.fire_report_trigger(report_id=REPORT_ID, org_id=ORG)
    assert outcome["status"] == "sent"
    assert outcome["delivery_results"] == delivered
    redis_client.delete.assert_awaited_once()


async def test_fire_report_trigger_falls_back_to_config_delivery():
    session = _MockSession([_trigger_result(_report_row()), MagicMock()])
    redis_client = _redis()
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis") as redis_cls,
        patch("modulo.core.reports.scheduler.get_generator", return_value=AsyncMock(return_value={"raw": 1})),
        patch("modulo.core.reports.scheduler.get_formatter", return_value=None),
        patch("modulo.core.reports.scheduler.get_deliverer", return_value=None),
        patch("modulo.core.reports.scheduler._deliver_via_config", new_callable=AsyncMock, return_value=[{"ok": True}]),
    ):
        redis_cls.from_url.return_value = redis_client
        outcome = await ch.fire_report_trigger(report_id=REPORT_ID, org_id=ORG)
    assert outcome["status"] == "sent"


async def test_fire_report_trigger_generator_cancellation_reraises():
    session = _MockSession([_trigger_result(_report_row())])
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = _redis()
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis", redis_cls),
        patch(
            "modulo.core.reports.scheduler.get_generator", return_value=AsyncMock(side_effect=asyncio.CancelledError())
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch.fire_report_trigger(report_id=REPORT_ID, org_id=ORG)


async def test_handle_report_failure_reraises_cancellation():
    redis_client = _redis()
    redis_client.incr = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._handle_report_failure(_MockSession(), redis_client, REPORT_ID, datetime.now(UTC))


async def test_clear_report_failure_counter_reraises_cancellation():
    redis_client = _redis()
    redis_client.delete = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._clear_report_failure_counter(redis_client, REPORT_ID)


# ---------------------------------------------------------------------------
# Ongoing top-up helpers
# ---------------------------------------------------------------------------


async def test_bump_ongoing_failure_reraises_cancellation():
    redis_client = _redis()
    redis_client.incr = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._bump_ongoing_failure(_MockSession(), redis_client, TRIGGER_A)


async def test_bump_ongoing_failure_swallows_redis_failure(caplog):
    redis_client = _redis()
    redis_client.incr = AsyncMock(side_effect=RuntimeError("redis down"))
    with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
        await ch._bump_ongoing_failure(_MockSession(), redis_client, TRIGGER_A)
    assert any("ongoing_failure_counter_unavailable" in m for m in caplog.messages)


async def test_clear_ongoing_failure_swallows_redis_failure(caplog):
    redis_client = _redis()
    redis_client.delete = AsyncMock(side_effect=RuntimeError("redis down"))
    with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
        await ch._clear_ongoing_failure(redis_client, TRIGGER_A)
    assert any("clear_ongoing_failure failed" in m for m in caplog.messages)


async def test_clear_ongoing_failure_reraises_cancellation():
    redis_client = _redis()
    redis_client.delete = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._clear_ongoing_failure(redis_client, TRIGGER_A)


async def test_clear_ongoing_failure_none_client_noop():
    cleared = await ch._clear_ongoing_failure(None, TRIGGER_A)
    assert cleared is None


async def test_ongoing_topup_raises_when_snapshot_unresolved():
    """Internal invariant: after the skip check, a None snapshot_id is a bug —
    raise a loud RuntimeError instead of creating a snapshot-less run."""
    trigger = _make_trigger()
    session = _MockSession()
    with (
        patch.object(ch, "_acquire_ongoing_trigger", new_callable=AsyncMock, return_value=trigger),
        patch.object(ch, "_ongoing_shortfall", new_callable=AsyncMock, return_value=1),
        patch.object(ch, "_resolve_ongoing_snapshot", new_callable=AsyncMock, return_value=(None, None)),
        pytest.raises(RuntimeError, match="snapshot_id unresolved"),
    ):
        await ch._ongoing_topup(
            session,
            trigger_id=TRIGGER_A,
            org_id=ORG,
            pipeline_id=PIPELINE,
            now=datetime.now(UTC),
        )


async def test_dispatch_ongoing_runs_reraises_cancellation():
    with (
        patch.object(ch, "get_settings", return_value=_settings()),
        patch("modulo.core.dispatch.dispatch_run", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._dispatch_ongoing_runs(None, ORG, [uuid.uuid4()])


async def test_dispatch_ongoing_runs_collects_per_run_errors():
    with (
        patch.object(ch, "get_settings", return_value=_settings()),
        patch("modulo.core.dispatch.dispatch_run", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
    ):
        outcomes = await ch._dispatch_ongoing_runs(None, ORG, [uuid.uuid4(), uuid.uuid4()])
    assert len(outcomes) == 2
    for entry in outcomes:
        assert entry["outcome"] == "dispatch_error"
        assert entry["job_id"] is None


async def test_fire_ongoing_trigger_bad_snapshot_and_redis_failure():
    """An unparseable latest_snapshot_id degrades to None and a failed stats
    write never fails the fire job."""
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=RuntimeError("redis down"))
    with (
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "_open_factory", return_value=_factory_for(_MockSession())),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "_ongoing_topup", new_callable=AsyncMock, return_value=[]),
        patch.object(ch, "AsyncRedis") as redis_cls,
    ):
        redis_cls.from_url.return_value = redis_client
        summary = await ch.fire_ongoing_trigger(
            trigger_id=TRIGGER_A,
            org_id=ORG,
            pipeline_id=PIPELINE,
            latest_snapshot_id="not-a-uuid",
        )
    assert summary["status"] == "noop"
    assert summary["created"] == 0


async def test_fire_ongoing_trigger_stats_persist_reraises_cancellation():
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=asyncio.CancelledError())
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client
    with (
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "_open_factory", return_value=_factory_for(_MockSession())),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "_ongoing_topup", new_callable=AsyncMock, return_value=[]),
        patch.object(ch, "AsyncRedis", redis_cls),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch.fire_ongoing_trigger(trigger_id=TRIGGER_A, org_id=ORG, pipeline_id=PIPELINE)


# ---------------------------------------------------------------------------
# Suite-run fire job — gate skips
# ---------------------------------------------------------------------------


async def test_suite_run_trigger_lock_denied_skips():
    session = _MockSession([_lock_result(False)])
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
    ):
        outcome = await ch.fire_suite_run_trigger(trigger_id=TRIGGER_A, org_id=ORG, pipeline_id=PIPELINE)
    assert outcome == {"status": "skipped", "reason": "trigger_busy"}


async def test_suite_run_trigger_inactive_or_missing_skips():
    session = _MockSession([_lock_result(True), _trigger_result(None)])
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
    ):
        outcome = await ch.fire_suite_run_trigger(trigger_id=TRIGGER_A, org_id=ORG, pipeline_id=PIPELINE)
    assert outcome == {"status": "skipped", "reason": "trigger_inactive_or_missing"}


async def test_suite_run_trigger_not_suite_run_skips():
    trigger = _make_trigger(run_kind="run")
    session = _MockSession([_lock_result(True), _trigger_result(trigger)])
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
    ):
        outcome = await ch.fire_suite_run_trigger(trigger_id=TRIGGER_A, org_id=ORG, pipeline_id=PIPELINE)
    assert outcome == {"status": "skipped", "reason": "not_suite_run_trigger"}


async def test_suite_run_trigger_paused_skips():
    trigger = _make_trigger(run_kind="suite_run", eval_suite_id=uuid.uuid4())
    session = _MockSession([_lock_result(True), _trigger_result(trigger)])
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "org_is_paused", new_callable=AsyncMock, return_value=True),
    ):
        outcome = await ch.fire_suite_run_trigger(trigger_id=TRIGGER_A, org_id=ORG, pipeline_id=PIPELINE)
    assert outcome == {"status": "skipped", "reason": ch.PAUSE_SKIP_REASON}


async def test_suite_run_trigger_missing_config_skips():
    trigger = _make_trigger(run_kind="suite_run", eval_suite_id=uuid.uuid4())
    session = _MockSession([_lock_result(True), _trigger_result(trigger)])
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
    ):
        outcome = await ch.fire_suite_run_trigger(trigger_id=TRIGGER_A, org_id=ORG, pipeline_id=PIPELINE)
    assert outcome == {"status": "skipped", "reason": "missing_suite_run_config"}


async def test_suite_run_trigger_build_skip_propagates():
    trigger = _make_trigger(
        run_kind="suite_run",
        eval_suite_id=uuid.uuid4(),
        config_json={"dataset_id": str(uuid.uuid4()), "model_backend_id": str(uuid.uuid4())},
    )
    session = _MockSession([_lock_result(True), _trigger_result(trigger)])
    skip = {"status": "skipped", "reason": "empty_dataset"}
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "_suite_run_fire_gates", new_callable=AsyncMock, return_value=None),
        patch.object(ch, "_suite_run_llm_judge_guard", new_callable=AsyncMock, return_value=None),
        patch.object(ch, "_build_suite_run_or_skip", new_callable=AsyncMock, return_value=(None, skip)),
    ):
        outcome = await ch.fire_suite_run_trigger(trigger_id=TRIGGER_A, org_id=ORG, pipeline_id=PIPELINE)
    assert outcome is skip


def test_resolve_suite_run_config_missing_ids():
    dataset_id, model_backend_id, skip = ch._resolve_suite_run_config({})
    assert dataset_id is None
    assert model_backend_id is None
    assert skip == {"status": "skipped", "reason": "missing_suite_run_config"}


def test_resolve_suite_run_config_invalid_ids():
    dataset_id, model_backend_id, skip = ch._resolve_suite_run_config(
        {"dataset_id": "bad", "model_backend_id": "also-bad"}
    )
    assert dataset_id is None
    assert model_backend_id is None
    assert skip == {"status": "skipped", "reason": "invalid_suite_run_config"}


async def test_build_suite_run_empty_dataset_skips():
    from modulo.core.eval_engine.execute_suite_run import SuiteRunEmptyDatasetError

    trigger = _make_trigger(eval_suite_id=uuid.uuid4())
    session = _MockSession()
    with patch(
        "modulo.core.eval_engine.execute_suite_run.build_suite_run",
        new=AsyncMock(side_effect=SuiteRunEmptyDatasetError("no rows")),
    ):
        run, skip = await ch._build_suite_run_or_skip(session, trigger, ORG, uuid.uuid4(), uuid.uuid4(), {}, PIPELINE)
    assert run is None
    assert skip is not None
    assert skip["reason"] == "empty_dataset"


async def test_build_suite_run_execution_error_skips():
    from modulo.core.eval_engine.execute_suite_run import SuiteRunExecutionError

    trigger = _make_trigger(eval_suite_id=uuid.uuid4())
    session = _MockSession()
    with patch(
        "modulo.core.eval_engine.execute_suite_run.build_suite_run",
        new=AsyncMock(side_effect=SuiteRunExecutionError("bad config")),
    ):
        run, skip = await ch._build_suite_run_or_skip(session, trigger, ORG, uuid.uuid4(), uuid.uuid4(), {}, PIPELINE)
    assert run is None
    assert skip is not None
    assert skip["reason"] == "suite_run_config_error"


# ---------------------------------------------------------------------------
# Catch-up scan helpers
# ---------------------------------------------------------------------------


async def test_claim_catchup_marker_reraises_cancellation():
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._claim_catchup_marker(redis_client, TRIGGER_A, 12345)


async def test_claim_catchup_marker_redis_failure_fails_open(caplog):
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=RuntimeError("redis down"))
    with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
        claimed = await ch._claim_catchup_marker(redis_client, TRIGGER_A, 12345)
    assert claimed is True


async def test_claim_catchup_marker_lost_race_returns_false():
    redis_client = _redis()
    redis_client.set = AsyncMock(return_value=False)
    claimed = await ch._claim_catchup_marker(redis_client, TRIGGER_A, 12345)
    assert claimed is False


async def test_mark_catchup_fired_reraises_cancellation():
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._mark_catchup_fired(redis_client, TRIGGER_A, 12345)


async def test_mark_catchup_fired_swallows_redis_failure(caplog):
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=RuntimeError("redis down"))
    with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
        await ch._mark_catchup_fired(redis_client, TRIGGER_A, 12345)
    assert any("catchup_marker_write_failed" in m for m in caplog.messages)


_DAILY_CRON = "0 0 * * * *"


def _catchup_row(now: datetime, *, hours_since_fire: float = 30.0) -> SimpleNamespace:
    return SimpleNamespace(
        id=TRIGGER_A,
        pipeline_id=PIPELINE,
        config_json={"snapshot_id": str(uuid.uuid4())},
        cron_expression=_DAILY_CRON,
        cron_timezone=None,
        next_fire_at=now + timedelta(hours=1),
        last_fired_at=now - timedelta(hours=hours_since_fire),
        run_kind="run",
    )


async def test_advance_catchup_epoch_reraises_cancellation():
    now = datetime.now(UTC)
    session = _MockSession()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._advance_catchup_epoch(session, _catchup_row(now), now)


async def test_advance_catchup_epoch_failure_returns_not_advanced(caplog):
    now = datetime.now(UTC)
    session = _MockSession()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
        next_nf, advanced = await ch._advance_catchup_epoch(session, _catchup_row(now), now)
    assert advanced is False
    assert next_nf > now
    assert any("catch-up advance failed" in m for m in caplog.messages)


async def test_advance_catchup_epoch_win_and_loss():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    session = _MockSession([_mock_result(fetchone=("won",))])
    _next_nf, advanced = await ch._advance_catchup_epoch(session, row, now)
    assert advanced is True
    session2 = _MockSession([_mock_result(fetchone=None)])
    _next_nf2, advanced2 = await ch._advance_catchup_epoch(session2, row, now)
    assert advanced2 is False


async def test_enqueue_catchup_fire_suite_run_branch():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    row.run_kind = "suite_run"
    session = _MockSession()
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0, "enqueue_failures": 0}
    enqueue = AsyncMock(return_value="job-1")
    with (
        patch.object(ch, "_enqueue_fire_job_async", enqueue),
        patch.object(ch, "_mark_catchup_fired", new_callable=AsyncMock),
    ):
        await ch._enqueue_catchup_fire(session, _redis(), q, ORG, row, now, None, now, 42, summary)
    assert summary["cron_catchup_enqueued"] == 1
    fn = enqueue.await_args.args[1]
    assert fn == "modulo.core.saq_worker.fire_suite_run_trigger"


async def test_enqueue_catchup_fire_enqueue_failure_rolls_back():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    session = _MockSession()
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0, "enqueue_failures": 0}
    rollback = AsyncMock()
    ingest = AsyncMock()
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
        patch.object(ch, "_rollback_catchup_advance", rollback),
        patch.object(ch, "_ingest_saq_error", ingest),
    ):
        await ch._enqueue_catchup_fire(session, _redis(), q, ORG, row, now, None, now, 42, summary)
    assert summary["enqueue_failures"] == 1
    rollback.assert_awaited_once()
    ingest.assert_awaited_once()


async def test_enqueue_catchup_fire_reraises_cancellation():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    session = _MockSession()
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0, "enqueue_failures": 0}
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._enqueue_catchup_fire(session, _redis(), q, ORG, row, now, None, now, 42, summary)


async def test_fire_missed_cron_epochs_read_failure_returns(caplog):
    session = _MockSession()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0}
    with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
        await ch._fire_missed_cron_epochs(
            session, _redis(), MagicMock(), ORG, datetime.now(UTC), summary=summary, advanced_this_tick=set()
        )
    assert summary["cron_catchup_enqueued"] == 0
    assert any("catch-up read failed" in m for m in caplog.messages)


async def test_fire_missed_cron_epochs_read_reraises_cancellation():
    session = _MockSession()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._fire_missed_cron_epochs(
            session, _redis(), MagicMock(), ORG, datetime.now(UTC), summary={}, advanced_this_tick=set()
        )


async def test_fire_missed_cron_epochs_skips_epoch_when_unresolvable():
    """A row that IS eligible but whose missed epoch cannot be computed is
    skipped (left for the missed-fire alert), never fired."""
    now = datetime.now(UTC)
    row = _catchup_row(now)
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0}
    enqueue = AsyncMock()
    session = _MockSession([_mock_result(all=[row])])
    with (
        patch.object(ch, "_missed_epoch_of", return_value=None),
        patch.object(ch, "_claim_catchup_marker", new_callable=AsyncMock) as claim,
        patch.object(ch, "_enqueue_catchup_fire", enqueue),
    ):
        await ch._fire_missed_cron_epochs(
            session, _redis(), MagicMock(), ORG, now, summary=summary, advanced_this_tick=set()
        )
    claim.assert_not_awaited()
    enqueue.assert_not_awaited()


async def test_fire_missed_cron_epochs_end_to_end_refire():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    session = _MockSession([_mock_result(all=[row]), _mock_result(fetchone=("won",))])
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0, "enqueue_failures": 0}
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-1"),
        patch.object(ch, "_claim_catchup_marker", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_mark_catchup_fired", new_callable=AsyncMock),
    ):
        await ch._fire_missed_cron_epochs(session, _redis(), q, ORG, now, summary=summary, advanced_this_tick=set())
    assert summary["cron_catchup_enqueued"] == 1


async def test_fire_missed_cron_epochs_skips_uncomputable_epoch():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    row.next_fire_at = None
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0}
    enqueue = AsyncMock()
    session = _MockSession([_mock_result(all=[row])])
    with (
        patch.object(ch, "_claim_catchup_marker", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_catchup_fire", enqueue),
    ):
        await ch._fire_missed_cron_epochs(
            session, _redis(), MagicMock(), ORG, now, summary=summary, advanced_this_tick=set()
        )
    enqueue.assert_not_awaited()


async def test_fire_missed_cron_epochs_skips_unelected_epoch():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    summary: dict[str, Any] = {"cron_catchup_enqueued": 0}
    advance = AsyncMock(return_value=(now, False))
    session = _MockSession([_mock_result(all=[row])])
    with (
        patch.object(ch, "_claim_catchup_marker", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_advance_catchup_epoch", advance),
        patch.object(ch, "_enqueue_catchup_fire", AsyncMock()),
    ):
        await ch._fire_missed_cron_epochs(
            session, _redis(), MagicMock(), ORG, now, summary=summary, advanced_this_tick=set()
        )
    assert summary["cron_catchup_enqueued"] == 0


def test_is_catchup_eligible_rejects_none_timestamps_and_uncomputable_cadence():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    row.next_fire_at = None
    assert ch._is_catchup_eligible(row, set(), now) is False
    row2 = _catchup_row(now)
    row2.cron_expression = "not a cron"
    assert ch._is_catchup_eligible(row2, set(), now) is False


def test_missed_epoch_of_none_when_not_catchable():
    now = datetime.now(UTC)
    row = _catchup_row(now)
    row.next_fire_at = None
    assert ch._missed_epoch_of(row, now) is None


async def test_rollback_catchup_advance_swallows_failure(caplog):
    session = _MockSession()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    now = datetime.now(UTC)
    with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
        await ch._rollback_catchup_advance(session, TRIGGER_A, now, now + timedelta(hours=1))
    assert any("rollback failed" in m for m in caplog.messages)


async def test_rollback_catchup_advance_reraises_cancellation():
    session = _MockSession()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    now = datetime.now(UTC)
    with pytest.raises(asyncio.CancelledError):
        await ch._rollback_catchup_advance(session, TRIGGER_A, now, now + timedelta(hours=1))


# ---------------------------------------------------------------------------
# fire_due_triggers scan + per-row helpers
# ---------------------------------------------------------------------------


async def test_write_cron_liveness_reraises_cancellation():
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._write_cron_liveness(redis_client)


async def test_write_cron_liveness_swallows_failure(caplog):
    redis_client = _redis()
    redis_client.set = AsyncMock(side_effect=RuntimeError("redis down"))
    with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
        await ch._write_cron_liveness(redis_client)
    assert any("liveness heartbeat write failed" in m for m in caplog.messages)


@pytest.mark.parametrize(
    "scan",
    ["_process_due_cron_scan", "_process_due_polling_scan", "_process_due_report_scan", "_process_due_ongoing_scan"],
)
async def test_scans_read_failure_degrades_to_empty(scan: str, caplog):
    session = _MockSession()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {}
    now = datetime.now(UTC)
    with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
        if scan == "_process_due_cron_scan":
            advanced = await ch._process_due_cron_scan(session, q, _redis(), now, ORG, False, summary)
            assert not advanced
        elif scan == "_process_due_polling_scan":
            await ch._process_due_polling_scan(session, q, now, ORG, False, summary)
        elif scan == "_process_due_report_scan":
            await ch._process_due_report_scan(session, q, now, ORG, summary)
        else:
            await ch._process_due_ongoing_scan(session, q, now, ORG, False, summary)
    assert any("read failed" in m for m in caplog.messages)


@pytest.mark.parametrize(
    "scan",
    ["_process_due_cron_scan", "_process_due_polling_scan", "_process_due_report_scan", "_process_due_ongoing_scan"],
)
async def test_scans_reraise_cancellation(scan: str):
    session = _MockSession()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    q = MagicMock()
    q.name = "runs"
    now = datetime.now(UTC)
    calls = {
        "_process_due_cron_scan": lambda: ch._process_due_cron_scan(session, q, _redis(), now, ORG, False, {}),
        "_process_due_polling_scan": lambda: ch._process_due_polling_scan(session, q, now, ORG, False, {}),
        "_process_due_report_scan": lambda: ch._process_due_report_scan(session, q, now, ORG, {}),
        "_process_due_ongoing_scan": lambda: ch._process_due_ongoing_scan(session, q, now, ORG, False, {}),
    }
    with pytest.raises(asyncio.CancelledError):
        await calls[scan]()


async def test_resolve_latest_snapshots_empty_pids_returns_empty():
    got = await ch._resolve_latest_snapshots(_MockSession(), set())
    assert not got


async def test_resolve_latest_snapshots_maps_pipeline_to_snapshot():
    pid = uuid.uuid4()
    sid = uuid.uuid4()
    session = _MockSession([_snapshot_rows_result([(pid, sid)])])
    got = await ch._resolve_latest_snapshots(session, {pid})
    assert got == {pid: sid}


async def test_enqueue_cron_fire_reraises_cancellation():
    q = MagicMock()
    q.name = "runs"
    now = datetime.now(UTC)
    row = SimpleNamespace(id=TRIGGER_A, cron_expression=_DAILY_CRON, next_fire_at=now, pipeline_id=PIPELINE)
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._enqueue_cron_fire(q, _redis(), now, ORG, row, None, set(), {"cron_enqueued": 0})


async def test_enqueue_cron_fire_marker_write_failure_is_isolated():
    q = MagicMock()
    q.name = "runs"
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A, cron_expression=_DAILY_CRON, next_fire_at=now, pipeline_id=PIPELINE, run_kind="run"
    )
    summary: dict[str, Any] = {"cron_enqueued": 0, "enqueue_failures": 0}
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-1"),
        patch.object(ch, "_mark_catchup_fired", new_callable=AsyncMock, side_effect=RuntimeError("redis down")),
    ):
        enqueued = await ch._enqueue_cron_fire(q, _redis(), now, ORG, row, None, set(), summary)
    assert enqueued is True
    assert summary["cron_enqueued"] == 1


async def test_enqueue_cron_fire_marker_write_reraises_cancellation():
    q = MagicMock()
    q.name = "runs"
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A, cron_expression=_DAILY_CRON, next_fire_at=now, pipeline_id=PIPELINE, run_kind="run"
    )
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value="job-1"),
        patch.object(ch, "_mark_catchup_fired", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._enqueue_cron_fire(q, _redis(), now, ORG, row, None, set(), {"cron_enqueued": 0})


async def test_process_one_due_cron_row_advance_failure_variants():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A,
        cron_expression=_DAILY_CRON,
        cron_timezone=None,
        next_fire_at=now,
        pipeline_id=PIPELINE,
        config_json={},
        run_kind="run",
    )
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"cron_due": 0, "cron_enqueued": 0}
    with (
        patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_due_cron_row(_MockSession(), q, _redis(), now, ORG, False, row, {}, set(), summary)
    summary2: dict[str, Any] = {"cron_due": 0, "cron_enqueued": 0}
    with patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, side_effect=RuntimeError("db down")):
        await ch._process_one_due_cron_row(_MockSession(), q, _redis(), now, ORG, False, row, {}, set(), summary2)
    assert summary2["cron_due"] == 1


async def test_process_one_due_cron_row_enqueue_failure_rolls_back():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A,
        cron_expression=_DAILY_CRON,
        cron_timezone=None,
        next_fire_at=now,
        pipeline_id=PIPELINE,
        config_json={},
        run_kind="run",
    )
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"cron_due": 0, "cron_enqueued": 0, "enqueue_failures": 0}
    ingest = AsyncMock()
    rollback = AsyncMock(side_effect=asyncio.CancelledError())
    with (
        patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_cron_fire", new_callable=AsyncMock, return_value=False),
        patch.object(ch, "_rollback_cron_advance", rollback),
        patch.object(ch, "_ingest_saq_error", ingest),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_due_cron_row(_MockSession(), q, _redis(), now, ORG, False, row, {}, set(), summary)
    summary2: dict[str, Any] = {"cron_due": 0, "cron_enqueued": 0, "enqueue_failures": 0}
    rollback2 = AsyncMock(side_effect=RuntimeError("db down"))
    with (
        patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_cron_fire", new_callable=AsyncMock, return_value=False),
        patch.object(ch, "_rollback_cron_advance", rollback2),
        patch.object(ch, "_ingest_saq_error", ingest),
    ):
        await ch._process_one_due_cron_row(_MockSession(), q, _redis(), now, ORG, False, row, {}, set(), summary2)
    ingest.assert_awaited()


async def test_process_one_due_cron_row_not_advanced_returns():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A,
        cron_expression=_DAILY_CRON,
        cron_timezone=None,
        next_fire_at=now,
        pipeline_id=PIPELINE,
        config_json={},
        run_kind="run",
    )
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"cron_due": 0, "cron_enqueued": 0}
    with (
        patch.object(ch, "_advance_cron_next_fire", new_callable=AsyncMock, return_value=False),
        patch.object(ch, "_enqueue_cron_fire", new_callable=AsyncMock) as enqueue,
    ):
        await ch._process_one_due_cron_row(_MockSession(), q, _redis(), now, ORG, False, row, {}, set(), summary)
    enqueue.assert_not_awaited()


async def test_enqueue_polling_fire_cancellation_and_failure():
    q = MagicMock()
    q.name = "runs"
    now = datetime.now(UTC)
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE, run_kind="run")
    config = {"poll_query": "SELECT 1"}
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._enqueue_polling_fire(q, now, ORG, row, config, uuid.uuid4(), {"polling_enqueued": 0})
    summary: dict[str, Any] = {"polling_enqueued": 0, "enqueue_failures": 0}
    with patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=RuntimeError("queue down")):
        enqueued = await ch._enqueue_polling_fire(q, now, ORG, row, config, uuid.uuid4(), summary)
    assert enqueued is False
    assert summary["enqueue_failures"] == 1


async def test_enqueue_suite_run_fire_cancellation_and_failure():
    q = MagicMock()
    q.name = "runs"
    now = datetime.now(UTC)
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE, run_kind="suite_run")
    with (
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._enqueue_suite_run_fire(q, now, ORG, row, {"polling_enqueued": 0}, "polling_enqueued")
    summary: dict[str, Any] = {"polling_enqueued": 0, "enqueue_failures": 0}
    with patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=RuntimeError("queue down")):
        enqueued = await ch._enqueue_suite_run_fire(q, now, ORG, row, summary, "polling_enqueued")
    assert enqueued is False


async def test_process_one_due_polling_row_missing_connector_advances():
    now = datetime.now(UTC)
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE, config_json={}, run_kind="run")
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"polling_due": 0}
    with patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock) as advance:
        await ch._process_one_due_polling_row(_MockSession(), q, now, ORG, False, row, summary)
    advance.assert_not_awaited()
    assert summary["polling_due"] == 1


async def test_process_one_due_polling_row_invalid_connector_id_degrades_to_missing():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A, pipeline_id=PIPELINE, config_json={"connector_instance_id": "junk"}, run_kind="run"
    )
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"polling_due": 0}
    with patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock) as advance:
        await ch._process_one_due_polling_row(_MockSession(), q, now, ORG, False, row, summary)
    advance.assert_not_awaited()
    assert summary["polling_due"] == 1


async def test_process_one_due_polling_row_advance_failure_variants():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A, pipeline_id=PIPELINE, config_json={"connector_instance_id": str(uuid.uuid4())}, run_kind="run"
    )
    q = MagicMock()
    q.name = "runs"
    with (
        patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_due_polling_row(_MockSession(), q, now, ORG, False, row, {"polling_due": 0})
    summary: dict[str, Any] = {"polling_due": 0}
    with patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, side_effect=RuntimeError("db down")):
        await ch._process_one_due_polling_row(_MockSession(), q, now, ORG, False, row, summary)
    assert summary["polling_due"] == 1


async def test_process_one_due_polling_row_not_advanced_and_enqueue_failure():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=TRIGGER_A, pipeline_id=PIPELINE, config_json={"connector_instance_id": str(uuid.uuid4())}, run_kind="run"
    )
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"polling_due": 0, "polling_enqueued": 0}
    with (
        patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=False),
        patch.object(ch, "_enqueue_polling_fire", new_callable=AsyncMock) as enqueue,
    ):
        await ch._process_one_due_polling_row(_MockSession(), q, now, ORG, False, row, summary)
    enqueue.assert_not_awaited()
    ingest = AsyncMock()
    summary2: dict[str, Any] = {"polling_due": 0, "polling_enqueued": 0}
    with (
        patch.object(ch, "_advance_polling_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_polling_fire", new_callable=AsyncMock, return_value=False),
        patch.object(ch, "_ingest_saq_error", ingest),
    ):
        await ch._process_one_due_polling_row(_MockSession(), q, now, ORG, False, row, summary2)
    ingest.assert_awaited_once()


async def test_polling_missing_connector_logs_event_and_advances():
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE)
    session = _MockSession()
    summary: dict[str, Any] = {"polling_due": 0}
    await ch._polling_missing_connector(session, ORG, row, 60, summary)
    assert summary["polling_due"] == 1
    assert len(session.added) == 1


async def test_polling_missing_connector_swallows_failure(caplog):
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE)
    session = _MockSession()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    summary: dict[str, Any] = {"polling_due": 0}
    with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
        await ch._polling_missing_connector(session, ORG, row, 60, summary)
    assert any("missing-connector handling failed" in m for m in caplog.messages)


async def test_polling_missing_connector_reraises_cancellation():
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE)
    session = _MockSession()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._polling_missing_connector(session, ORG, row, 60, {"polling_due": 0})


def _report_scan_row(run_kind: str = "run") -> SimpleNamespace:
    return SimpleNamespace(id=REPORT_ID, cron_expression="0 9 * * *", run_kind=run_kind)


async def test_process_one_due_report_row_suite_run_guard_skips():
    now = datetime.now(UTC)
    q = MagicMock()
    q.name = "runs"
    summary: dict[str, Any] = {"report_skipped_suite_run": 0, "report_due": 0}
    with patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock) as advance:
        await ch._process_one_due_report_row(_MockSession(), q, now, ORG, _report_scan_row("suite_run"), summary)
    advance.assert_not_awaited()
    assert summary["report_skipped_suite_run"] == 1


async def test_process_one_due_report_row_advance_variants():
    now = datetime.now(UTC)
    q = MagicMock()
    q.name = "runs"
    row = _report_scan_row()
    with (
        patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_due_report_row(_MockSession(), q, now, ORG, row, {"report_due": 0})
    summary: dict[str, Any] = {"report_due": 0, "report_enqueued": 0, "enqueue_failures": 0}
    with (
        patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, side_effect=RuntimeError("db down")),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
    ):
        await ch._process_one_due_report_row(_MockSession(), q, now, ORG, row, summary)
    enqueue.assert_not_awaited()


async def test_process_one_due_report_row_enqueue_variants():
    now = datetime.now(UTC)
    q = MagicMock()
    q.name = "runs"
    row = _report_scan_row()
    summary: dict[str, Any] = {"report_due": 0, "report_enqueued": 0, "enqueue_failures": 0}
    ingest = AsyncMock()
    with (
        patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_due_report_row(_MockSession(), q, now, ORG, row, summary)
    summary2: dict[str, Any] = {"report_due": 0, "report_enqueued": 0, "enqueue_failures": 0}
    with (
        patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
        patch.object(ch, "_ingest_saq_error", ingest),
    ):
        await ch._process_one_due_report_row(_MockSession(), q, now, ORG, row, summary2)
    ingest.assert_awaited_once()


async def test_process_one_due_report_row_not_advanced_returns():
    now = datetime.now(UTC)
    q = MagicMock()
    q.name = "runs"
    row = _report_scan_row()
    summary: dict[str, Any] = {"report_due": 0, "report_enqueued": 0}
    with (
        patch.object(ch, "_advance_report_next_send", new_callable=AsyncMock, return_value=False),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
    ):
        await ch._process_one_due_report_row(_MockSession(), q, now, ORG, row, summary)
    enqueue.assert_not_awaited()


async def test_process_one_ongoing_row_variants():
    now = datetime.now(UTC)
    q = MagicMock()
    q.name = "runs"
    row = SimpleNamespace(
        id=TRIGGER_A, pipeline_id=PIPELINE, config_json={"scan_interval_seconds": 120}, run_kind="run"
    )
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, {"ongoing_due": 0}, 0)
    summary: dict[str, Any] = {"ongoing_due": 0}
    with patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, side_effect=RuntimeError("db down")):
        got = await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, summary, 0)
    assert got == 0
    cap_summary: dict[str, Any] = {"ongoing_due": 0}
    with patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True):
        got = await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, cap_summary, ONGOING_CAP)
    assert got == 0
    not_advanced: dict[str, Any] = {"ongoing_due": 0}
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=False),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock) as enqueue,
    ):
        got = await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, not_advanced, 0)
    assert got == 0
    enqueue.assert_not_awaited()


async def test_process_one_ongoing_row_suite_branch_enqueue_variants():
    now = datetime.now(UTC)
    q = MagicMock()
    q.name = "runs"
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE, config_json={}, run_kind="suite_run")
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_suite_run_fire", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, {"ongoing_due": 0}, 0)
    summary: dict[str, Any] = {"ongoing_due": 0}
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_suite_run_fire", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
        pytest.raises(RuntimeError, match="queue down"),
    ):
        await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, summary, 0)
    summary2: dict[str, Any] = {"ongoing_due": 0, "ongoing_enqueued": 0}
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_suite_run_fire", new_callable=AsyncMock, return_value=False),
    ):
        got = await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, summary2, 0)
    assert got == 0


async def test_process_one_ongoing_row_enqueue_variants():
    """The non-suite ongoing enqueue: cancellation re-raises; a generic failure
    ingests an error event and returns 0."""
    now = datetime.now(UTC)
    q = MagicMock()
    q.name = "runs"
    row = SimpleNamespace(id=TRIGGER_A, pipeline_id=PIPELINE, config_json={}, run_kind="run")
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, {"ongoing_due": 0}, 0)
    summary: dict[str, Any] = {"ongoing_due": 0, "ongoing_enqueue_failures": 0, "ongoing_enqueued": 0}
    ingest = AsyncMock()
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
        patch.object(ch, "_ingest_saq_error", ingest),
    ):
        got = await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, summary, 0)
    assert got == 0
    ingest.assert_awaited_once()
    summary2: dict[str, Any] = {"ongoing_due": 0, "ongoing_enqueue_failures": 0, "ongoing_enqueued": 0}
    with (
        patch.object(ch, "_advance_ongoing_next_fire", new_callable=AsyncMock, return_value=True),
        patch.object(ch, "_enqueue_fire_job_async", new_callable=AsyncMock, return_value=None),
    ):
        got = await ch._process_one_ongoing_row(_MockSession(), q, now, ORG, False, row, {}, summary2, 0)
    assert got == 0


# ---------------------------------------------------------------------------
# Nodeless zombie terminalizer helpers
# ---------------------------------------------------------------------------


def test_is_nodeless_zombie_row_rejects_missing_started_at():
    row = SimpleNamespace(status="running", node_token_usage=None, outputs_json=None, started_at=None)
    assert ch._is_nodeless_zombie_row(row, 20) is False


async def test_fail_nodeless_run_noop_when_run_missing_or_not_running():
    session = _MockSession()
    await ch._fail_nodeless_run(session, uuid.uuid4(), ORG)
    assert not session.added


# ---------------------------------------------------------------------------
# dispatcher_reconcile helpers
# ---------------------------------------------------------------------------


async def test_record_fact_for_terminalized_run_success():
    session = _MockSession()
    run = SimpleNamespace(id=uuid.uuid4())
    fact = AsyncMock()
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run),
        patch("modulo.core.analytics.record_fact_for_terminal_failed_run", fact),
    ):
        await ch._record_fact_for_terminalized_run(run.id, ORG)
    fact.assert_awaited_once_with(session, run)


async def test_record_fact_for_terminalized_run_missing_run_warns(caplog):
    session = _MockSession()
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=None),
        caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"),
    ):
        await ch._record_fact_for_terminalized_run(uuid.uuid4(), ORG)
    assert any("terminalized_facts_run_missing" in m for m in caplog.messages)


async def test_record_fact_for_terminalized_run_reraises_cancellation():
    session = _MockSession()
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._record_fact_for_terminalized_run(uuid.uuid4(), ORG)


async def test_record_fact_for_terminalized_run_swallows_failure(caplog):
    session = _MockSession()
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=SimpleNamespace(id=uuid.uuid4())),
        patch(
            "modulo.core.analytics.record_fact_for_terminal_failed_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"),
    ):
        await ch._record_fact_for_terminalized_run(uuid.uuid4(), ORG)
    assert any("terminalized_facts_failed" in m for m in caplog.messages)


async def test_reconcile_org_read_failure_returns(caplog):
    session = _MockSession()
    summary: dict[str, Any] = {"mid_graph_wedge_terminalized": 0, "claim_cap_terminalized": 0, "scanned": 0}
    with (
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "_terminalize_mid_graph_wedges", new_callable=AsyncMock, side_effect=RuntimeError("db down")),
        caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"),
    ):
        got = await ch._reconcile_org(
            _factory_for(session),
            MagicMock(),
            _redis(),
            ORG,
            MagicMock(),
            20,
            60,
            3,
            600,
            120,
            0,
            summary,
            [],
        )
    assert got == 0
    assert any("read failed" in m for m in caplog.messages)


async def test_reconcile_org_processes_rows():
    session = _MockSession([_mock_result(all=[]), _mock_result(all=[])])
    summary: dict[str, Any] = {"mid_graph_wedge_terminalized": 0, "claim_cap_terminalized": 0, "scanned": 0}
    with (
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "_terminalize_mid_graph_wedges", new_callable=AsyncMock, return_value=[]),
        patch.object(ch, "_terminalize_claim_cap_exhausted", new_callable=AsyncMock, return_value=[uuid.uuid4()]),
        patch.object(ch, "_reconcile_one_row", new_callable=AsyncMock, return_value=0) as one_row,
        patch.object(ch, "_run_outputs_sweep_for_org", new_callable=AsyncMock),
    ):
        got = await ch._reconcile_org(
            _factory_for(session),
            MagicMock(),
            _redis(),
            ORG,
            ch.text("1 = 1"),
            20,
            60,
            3,
            600,
            120,
            0,
            summary,
            [],
        )
    assert got == 0
    assert summary["claim_cap_terminalized"] == 1
    one_row.assert_not_awaited()


async def test_run_reconcile_sweeps_records_summary(caplog):
    redis_client = _redis()
    summary: dict[str, Any] = {
        "nodeless_failed": 0,
        "claim_cap_terminalized": 0,
        "mid_graph_wedge_terminalized": 0,
        "dispatch_failed_terminalized": 0,
    }
    with (
        patch.object(ch, "_open_factory", return_value=MagicMock()),
        patch.object(ch, "_open_system_factory", return_value=MagicMock()),
        patch.object(
            ch,
            "run_classification_reconcile",
            new_callable=AsyncMock,
            return_value={"classified": 2, "unclassified": 1, "errors": 0},
        ),
        patch.object(
            ch,
            "enforce_no_delivery_streaks",
            new_callable=AsyncMock,
            return_value={"scanned": 3, "deactivated": 1, "capped": 0, "alerts": 1, "notify_failed": 0},
        ),
        patch(
            "modulo.auth.api_key.revoke_run_api_key_sweep",
            new_callable=AsyncMock,
            return_value={"scanned": 4, "revoked": 2, "errors": 0},
        ),
        patch(
            "modulo.core.rollback_thresholds.evaluate_rollback_thresholds",
            new_callable=AsyncMock,
            return_value={"orgs_checked": 1, "flagged_orgs": ["org-1"]},
        ),
        caplog.at_level(logging.INFO, logger="modulo.core.cron_helpers"),
    ):
        await ch._run_reconcile_sweeps(redis_client, summary)
    assert summary["classification_classified"] == 2
    assert summary["streak_deactivated"] == 1
    assert summary["run_api_key_revoked"] == 2
    assert summary["rollback_thresholds_flagged"] == 1


async def test_run_reconcile_sweeps_swallows_each_failure(caplog):
    redis_client = _redis()
    summary: dict[str, Any] = {
        "nodeless_failed": 0,
        "claim_cap_terminalized": 0,
        "mid_graph_wedge_terminalized": 0,
        "dispatch_failed_terminalized": 0,
    }
    with (
        patch.object(ch, "_open_factory", return_value=MagicMock()),
        patch.object(ch, "_open_system_factory", return_value=MagicMock()),
        patch.object(ch, "run_classification_reconcile", new_callable=AsyncMock, side_effect=RuntimeError("db down")),
        patch.object(ch, "enforce_no_delivery_streaks", new_callable=AsyncMock, side_effect=RuntimeError("redis down")),
        patch(
            "modulo.auth.api_key.revoke_run_api_key_sweep", new_callable=AsyncMock, side_effect=RuntimeError("db down")
        ),
        patch(
            "modulo.core.rollback_thresholds.evaluate_rollback_thresholds",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"),
    ):
        await ch._run_reconcile_sweeps(redis_client, summary)
    assert summary["rollback_thresholds_checked"] == 0
    for token in (
        "classification_sweep_failed",
        "streak_sweep_failed",
        "run_api_key_sweep_failed",
        "rollback_thresholds_failed",
    ):
        assert any(token in m for m in caplog.messages)


async def test_update_reconcile_telemetry_records_stall_reasons():
    summary: dict[str, Any] = {
        "nodeless_failed": 1,
        "claim_cap_terminalized": 2,
        "mid_graph_wedge_terminalized": 3,
        "dispatch_failed_terminalized": 4,
    }
    record = MagicMock()
    with (
        patch.object(ch, "get_settings", return_value=_settings(modulo_telemetry_enabled=True)),
        patch.object(ch, "_open_system_factory", return_value=MagicMock()),
        patch("modulo.core.error_tracking.metrics.sample_run_runtime_metrics", new_callable=AsyncMock),
        patch("modulo.core.error_tracking.metrics.sample_error_group_metrics", new_callable=AsyncMock),
        patch("modulo.core.error_tracking.metrics.record_stall_reason", record),
    ):
        await ch._update_reconcile_telemetry(summary)
    recorded = [call.args[0] for call in record.call_args_list]
    assert recorded == ["executor_stalled", "claim_cap_exhausted", "executor_superseded", "dispatch_failed"]


async def test_update_reconcile_telemetry_disabled_returns_early():
    record = MagicMock()
    with (
        patch.object(ch, "get_settings", return_value=_settings(modulo_telemetry_enabled=False)),
        patch("modulo.core.error_tracking.metrics.sample_run_runtime_metrics", new_callable=AsyncMock) as sample_runs,
        patch("modulo.core.error_tracking.metrics.record_stall_reason", record),
    ):
        await ch._update_reconcile_telemetry(
            {
                "nodeless_failed": 0,
                "claim_cap_terminalized": 0,
                "mid_graph_wedge_terminalized": 0,
                "dispatch_failed_terminalized": 0,
            }
        )
    sample_runs.assert_not_awaited()
    record.assert_not_called()


async def test_update_reconcile_telemetry_reraises_cancellation():
    summary: dict[str, Any] = {
        "nodeless_failed": 0,
        "claim_cap_terminalized": 0,
        "mid_graph_wedge_terminalized": 0,
        "dispatch_failed_terminalized": 0,
    }
    with (
        patch.object(ch, "get_settings", return_value=_settings(modulo_telemetry_enabled=True)),
        patch.object(ch, "_open_system_factory", return_value=MagicMock()),
        patch(
            "modulo.core.error_tracking.metrics.sample_run_runtime_metrics",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError(),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._update_reconcile_telemetry(summary)


async def test_update_reconcile_telemetry_swallows_failure(caplog):
    summary: dict[str, Any] = {
        "nodeless_failed": 0,
        "claim_cap_terminalized": 0,
        "mid_graph_wedge_terminalized": 0,
        "dispatch_failed_terminalized": 0,
    }
    with (
        patch.object(ch, "get_settings", return_value=_settings(modulo_telemetry_enabled=True)),
        patch(
            "modulo.core.error_tracking.metrics.sample_run_runtime_metrics",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"),
    ):
        await ch._update_reconcile_telemetry(summary)
    assert any("metrics_update_failed" in m for m in caplog.messages)


async def test_reconcile_enqueue_failed_reraises_cancellation_on_ping():
    now = datetime.now(UTC)
    row = SimpleNamespace(id=uuid.uuid4(), enqueue_failed_at=now - timedelta(hours=2))
    redis_client = _redis()
    redis_client.ping = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._reconcile_enqueue_failed(redis_client, _MockSession(), ORG, row, True, 0, {"skipped": 0}, [])


async def test_reconcile_enqueue_failed_redis_down_keeps_pending():
    now = datetime.now(UTC)
    row = SimpleNamespace(id=uuid.uuid4(), enqueue_failed_at=now - timedelta(hours=2))
    redis_client = _redis()
    redis_client.ping = AsyncMock(side_effect=RuntimeError("redis down"))
    summary: dict[str, Any] = {"skipped": 0}
    got = await ch._reconcile_enqueue_failed(redis_client, _MockSession(), ORG, row, True, 0, summary, [])
    assert got == 0
    assert summary["skipped"] == 1


async def test_reconcile_enqueue_failed_cap_hit_defers():
    now = datetime.now(UTC)
    row = SimpleNamespace(id=uuid.uuid4(), enqueue_failed_at=now - timedelta(seconds=30))
    summary: dict[str, Any] = {"enqueue_failed_capped": 0}
    got = await ch._reconcile_enqueue_failed(
        _redis(), _MockSession(), ORG, row, True, ch.ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK, summary, []
    )
    assert got == ch.ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK
    assert summary["enqueue_failed_capped"] == 1


async def test_read_reconcile_job_reraises_cancellation():
    q = MagicMock()
    q.name = "runs"
    q.job = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._read_reconcile_job(
            _MockSession(), q, ORG, SimpleNamespace(id=uuid.uuid4()), "run:x", "read", {"redis_errors": 0}
        )


async def test_read_reconcile_job_failure_ingests_and_fails_safe():
    q = MagicMock()
    q.name = "runs"
    q.job = AsyncMock(side_effect=RuntimeError("redis down"))
    summary: dict[str, Any] = {"redis_errors": 0}
    ingest = AsyncMock()
    with patch.object(ch, "_ingest_saq_error", ingest):
        job, ok = await ch._read_reconcile_job(
            _MockSession(), q, ORG, SimpleNamespace(id=uuid.uuid4()), "run:x", "read", summary
        )
    assert job is None
    assert ok is False
    assert summary["redis_errors"] == 1
    ingest.assert_awaited_once()


async def test_re_dispatch_reconciled_run_variants():
    row = SimpleNamespace(id=uuid.uuid4())
    session = _MockSession()
    summary: dict[str, Any] = {
        "redis_errors": 0,
        "repaired": 0,
        "skipped": 0,
        "deduped": 0,
        "capacity_deferred": 0,
        "enqueue_failed_redispatched": 0,
    }
    with (
        patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._re_dispatch_reconciled_run(
            session, MagicMock(), ORG, row, "execute_run", "abc", None, False, 0, summary
        )
    summary2: dict[str, Any] = {
        "redis_errors": 0,
        "repaired": 0,
        "skipped": 0,
        "deduped": 0,
        "capacity_deferred": 0,
        "enqueue_failed_redispatched": 0,
    }
    ingest = AsyncMock()
    with (
        patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
        patch.object(ch, "_ingest_saq_error", ingest),
    ):
        got = await ch._re_dispatch_reconciled_run(
            session, MagicMock(), ORG, row, "execute_run", "abc", None, False, 3, summary2
        )
    assert got == 3
    ingest.assert_awaited_once()
    summary3: dict[str, Any] = {
        "redis_errors": 0,
        "repaired": 0,
        "skipped": 0,
        "deduped": 0,
        "capacity_deferred": 0,
        "enqueue_failed_redispatched": 0,
    }
    with patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueue_failed", None)):
        got = await ch._re_dispatch_reconciled_run(
            session, MagicMock(), ORG, row, "execute_run", "abc", None, True, 0, summary3
        )
    assert got == 0
    assert summary3["skipped"] == 1


async def test_re_dispatch_reconciled_run_enqueued_increments_counter():
    row = SimpleNamespace(id=uuid.uuid4())
    session = _MockSession()
    summary: dict[str, Any] = {
        "redis_errors": 0,
        "repaired": 0,
        "skipped": 0,
        "deduped": 0,
        "capacity_deferred": 0,
        "enqueue_failed_redispatched": 0,
    }
    with patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "job-9")):
        got = await ch._re_dispatch_reconciled_run(
            session, MagicMock(), ORG, row, "execute_run", "abc", None, True, 0, summary
        )
    assert got == 1
    assert summary["enqueue_failed_redispatched"] == 1


async def test_redispatch_nodeless_variants():
    row = SimpleNamespace(id=uuid.uuid4())
    session = _MockSession()
    summary: dict[str, Any] = {"redis_errors": 0, "nodeless_redispatched": 0, "nodeless_failed": 0, "skipped": 0}
    with (
        patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._redispatch_nodeless(session, MagicMock(), ORG, row, "execute_run", "abc", summary, [])
    summary2: dict[str, Any] = {"redis_errors": 0, "nodeless_redispatched": 0, "nodeless_failed": 0, "skipped": 0}
    fail = AsyncMock()
    with (
        patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, side_effect=RuntimeError("queue down")),
        patch.object(ch, "_fail_nodeless_run", fail),
        patch.object(ch, "_ingest_saq_error", AsyncMock()),
    ):
        await ch._redispatch_nodeless(session, MagicMock(), ORG, row, "execute_run", "abc", summary2, [])
    assert summary2["nodeless_failed"] == 1
    fail.assert_awaited_once()
    summary3: dict[str, Any] = {"redis_errors": 0, "nodeless_redispatched": 0, "nodeless_failed": 0, "skipped": 0}
    with patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("deferred", None)):
        await ch._redispatch_nodeless(session, MagicMock(), ORG, row, "execute_run", "abc", summary3, [])
    assert summary3["skipped"] == 1
    summary4: dict[str, Any] = {"redis_errors": 0, "nodeless_redispatched": 0, "nodeless_failed": 0, "skipped": 0}
    with patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "job-1")):
        await ch._redispatch_nodeless(session, MagicMock(), ORG, row, "execute_run", "abc", summary4, [])
    assert summary4["nodeless_redispatched"] == 1


def _reconcile_row(status: str = "running") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=PIPELINE,
        status=status,
        dispatched_at=None,
        heartbeat_at=None,
        node_token_usage={"input": 1},
        outputs_json={"done": True},
        started_at=datetime.now(UTC),
        claim_count=0,
        dispatcher=None,
        enqueue_failed_at=None,
        retry_policy=None,
    )


async def test_reconcile_one_row_recheck_variants():
    row = _reconcile_row()
    q = MagicMock()
    q.name = "runs"
    session = _MockSession()
    summary: dict[str, Any] = {
        "scanned": 0,
        "skipped": 0,
        "redis_errors": 0,
        "repaired": 0,
        "deduped": 0,
        "capacity_deferred": 0,
    }
    # Re-check fails -> return unchanged counter.
    with (
        patch.object(ch, "_reconcile_nodeless_repair", new_callable=AsyncMock, return_value=None),
        patch.object(ch, "_reconcile_enqueue_failed", new_callable=AsyncMock, return_value=None),
        patch.object(ch, "_read_reconcile_job", new_callable=AsyncMock, side_effect=[(None, True), (None, False)]),
    ):
        got = await ch._reconcile_one_row(session, q, _redis(), ORG, row, 20, 0, summary, [])
    assert got == 0
    # Re-check finds a job -> skipped.
    summary2: dict[str, Any] = {
        "scanned": 0,
        "skipped": 0,
        "redis_errors": 0,
        "repaired": 0,
        "deduped": 0,
        "capacity_deferred": 0,
    }
    with (
        patch.object(ch, "_reconcile_nodeless_repair", new_callable=AsyncMock, return_value=None),
        patch.object(ch, "_reconcile_enqueue_failed", new_callable=AsyncMock, return_value=None),
        patch.object(ch, "_read_reconcile_job", new_callable=AsyncMock, side_effect=[(None, True), ("job-1", True)]),
    ):
        got = await ch._reconcile_one_row(session, q, _redis(), ORG, row, 20, 0, summary2, [])
    assert got == 0
    assert summary2["skipped"] == 1


async def test_capacity_defer_pending_run_no_pipeline_false():
    row = _reconcile_row("pending")
    row.dispatched_at = None
    session = _MockSession()
    deferred = await ch._capacity_defer_pending_run(session, row, {"capacity_deferred": 0})
    assert deferred is False


async def test_re_enqueue_run_gates_on_dispatch_run():
    dispatch = AsyncMock(return_value=("enqueued", "job-1"))
    with patch("modulo.core.dispatch.dispatch_run", dispatch):
        outcome, job_id = await ch._re_enqueue_run("runs", str(uuid.uuid4()), str(ORG), "execute_run", key_suffix="abc")
    assert outcome == "enqueued"
    assert job_id == "job-1"
    assert dispatch.await_args.kwargs["key_suffix"] == "abc"


# ---------------------------------------------------------------------------
# F6a decision-payload guards
# ---------------------------------------------------------------------------


def test_decision_gate_identity_prefers_stamped_gate_id():
    payload = json.dumps({"gate_id": "stamped-gate"})
    identity = ch._decision_gate_identity((None, payload, "row-gate"))
    assert identity == "stamped-gate"


def test_decision_gate_identity_falls_back_to_row_gate_id():
    identity = ch._decision_gate_identity((None, "not json", "row-gate"))
    assert identity == "row-gate"


def test_decision_gate_identity_none_when_unstamped():
    identity = ch._decision_gate_identity((None, json.dumps({"action": "approved"}), None))
    assert identity is None


async def test_awaiting_human_guard_corrupted_payload_skips():
    """A committed decision whose payload cannot be parsed cannot be verified —
    the guard and the resume-data reconstruction must agree (both skip)."""
    with (
        patch.object(
            ch,
            "_latest_committed_decision_row",
            new_callable=AsyncMock,
            return_value=("approved", "not-json{", "gate-1"),
        ),
        patch.object(ch, "_pending_claimed_gate_id", new_callable=AsyncMock, return_value=None),
        patch.object(ch, "_has_any_undecided_claim_row", new_callable=AsyncMock, return_value=False),
    ):
        has_decision = await ch._awaiting_human_has_committed_decision(_MockSession(), ORG, uuid.uuid4())
    assert has_decision is False


async def test_committed_decision_resume_data_corrupted_payload_recovers_action():
    """A corrupted payload degrades to ``{"action": <decision>}`` stamped with
    the decision row's own gate id — never None for a committed decision."""
    with patch.object(
        ch, "_latest_committed_decision_row", new_callable=AsyncMock, return_value=("approved", "not-json{", "gate-1")
    ):
        resume = await ch._committed_decision_resume_data(_MockSession(), ORG, uuid.uuid4())
    assert resume == {"action": "approved", "gate_id": "gate-1"}


async def test_committed_decision_resume_data_no_decision_returns_none():
    with patch.object(ch, "_latest_committed_decision_row", new_callable=AsyncMock, return_value=None):
        resume = await ch._committed_decision_resume_data(_MockSession(), ORG, uuid.uuid4())
    assert resume is None


async def test_committed_decision_resume_data_unstamped_payload_gets_row_gate():
    payload = json.dumps({"action": "rejected"})
    with patch.object(
        ch, "_latest_committed_decision_row", new_callable=AsyncMock, return_value=("rejected", payload, "gate-2")
    ):
        resume = await ch._committed_decision_resume_data(_MockSession(), ORG, uuid.uuid4())
    assert resume == {"action": "rejected", "gate_id": "gate-2"}


# ---------------------------------------------------------------------------
# Reconcile sweep cancellation arms
# ---------------------------------------------------------------------------


async def test_reconcile_org_reraises_cancellation():
    session = _MockSession()
    summary: dict[str, Any] = {"mid_graph_wedge_terminalized": 0, "claim_cap_terminalized": 0, "scanned": 0}
    with (
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "_terminalize_mid_graph_wedges", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._reconcile_org(
            _factory_for(session), MagicMock(), _redis(), ORG, ch.text("1 = 1"), 20, 60, 3, 600, 120, 0, summary, []
        )


@pytest.mark.parametrize(
    "sweep_patch",
    [
        ("ch.run_classification_reconcile", False),
        ("ch.enforce_no_delivery_streaks", False),
        ("modulo.auth.api_key.revoke_run_api_key_sweep", True),
        ("modulo.core.rollback_thresholds.evaluate_rollback_thresholds", True),
    ],
)
async def test_run_reconcile_sweeps_reraises_cancellation(sweep_patch: tuple[str, bool]):
    target, needs_factories = sweep_patch
    redis_client = _redis()
    summary: dict[str, Any] = {
        "nodeless_failed": 0,
        "claim_cap_terminalized": 0,
        "mid_graph_wedge_terminalized": 0,
        "dispatch_failed_terminalized": 0,
    }
    cancelled = AsyncMock(side_effect=asyncio.CancelledError())
    if target.startswith("ch."):
        patcher = patch.object(ch, target.split(".", 1)[1], cancelled)
    else:
        patcher = patch(target, new=cancelled)
    with ExitStack() as stack:
        stack.enter_context(patcher)
        if needs_factories:
            stack.enter_context(patch.object(ch, "_open_factory", return_value=MagicMock()))
            stack.enter_context(patch.object(ch, "_open_system_factory", return_value=MagicMock()))
        with pytest.raises(asyncio.CancelledError):
            await ch._run_reconcile_sweeps(redis_client, summary)


# ---------------------------------------------------------------------------
# Polling fire-path residual arms — spend gate, re-raise propagation
# ---------------------------------------------------------------------------


async def test_polling_spend_gate_skip_none_when_under_limit():
    """The polling spend gate mirrors the cron gate: today's cost under the
    trigger's daily limit returns None (fire proceeds), not a skip outcome."""
    session = _MockSession([_mock_result(scalar_one=5.0)])
    trigger = _make_trigger(daily_spend_limit=10.0)
    result = await ch._polling_spend_gate_skip(session, trigger, ORG, TRIGGER_A)
    assert result is None


async def test_polling_pre_fire_gate_lock_denied_skips_busy():
    """When the advisory lock is held elsewhere the gate returns a
    ``trigger_busy`` skip — the fire job must never double-fire a trigger
    another worker is already firing."""
    session = _MockSession([_lock_result(False)])
    trigger, skip = await ch._polling_pre_fire_gate(session, trigger_id=TRIGGER_A, org_id=ORG)
    assert trigger is None
    assert skip == {"status": "skipped", "reason": "trigger_busy"}


async def test_build_polling_connector_reraises_cancellation():
    """A cancellation while wiring the polling connector must propagate —
    the fire job is being cancelled, not failing (never downgraded to
    ``(None, None)``)."""
    with (
        patch(
            "modulo.core.trigger_engine.polling._build_polling_connector_from_instance",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._build_polling_connector(_MockSession(), MagicMock(), _make_trigger(), ORG, TRIGGER_A)


async def test_build_polling_connector_reraises_shared_budget_unavailable():
    """FAR-442 fail-closed: a configured-but-unresolvable shared budget must
    propagate out of the connector build, never downgrade to the per-process
    local bucket."""
    with (
        patch(
            "modulo.core.trigger_engine.polling._build_polling_connector_from_instance",
            new=AsyncMock(side_effect=SharedBudgetUnavailableError("redis down")),
        ),
        pytest.raises(SharedBudgetUnavailableError),
    ):
        await ch._build_polling_connector(_MockSession(), MagicMock(), _make_trigger(), ORG, TRIGGER_A)


async def test_run_poll_query_reraises_cancellation():
    """A cancellation during the poll query re-raises — never misreported as a
    query timeout/failure skip."""
    connector = MagicMock()
    connector.query = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ch._run_poll_query(_MockSession(), connector, _make_trigger(), ORG, TRIGGER_A, "SELECT 1")


async def test_evaluate_poll_condition_reraises_cancellation():
    """A cancellation while evaluating the poll condition re-raises instead of
    being swallowed into a condition-error outcome."""
    with (
        patch(
            "modulo.core.trigger_engine.polling.evaluate_condition",
            MagicMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch._evaluate_poll_condition(_MockSession(), {"rows": []}, _make_trigger(), ORG, TRIGGER_A, None)


async def test_fire_polling_trigger_reraises_cancellation_from_poll():
    """A cancellation surfacing from the poll-fire body must propagate through
    the connector-release ``finally`` — the epoch bookkeeping is left to the
    next tick (cancelled, not failed)."""
    trigger = _make_trigger(config_json={"connector_instance_id": str(uuid.uuid4()), "poll_query": "SELECT 1"})
    session = _MockSession(
        [
            _lock_result(True),
            _trigger_result(trigger),
            _mock_result(scalar_one=0),
            _mock_result(scalar_one_or_none=SimpleNamespace(id=uuid.uuid4())),
        ]
    )
    with (
        patch.object(ch, "_open_factory", return_value=_factory_for(session)),
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.run_admission.evaluate_backpressure", new_callable=AsyncMock, return_value=(False, "")),
        patch.object(ch, "_build_polling_connector", new_callable=AsyncMock, return_value=(MagicMock(), _redis())),
        patch.object(ch, "_run_poll_query", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
        patch("modulo.core.trigger_engine.polling._close_polling_resources", new_callable=AsyncMock),
        pytest.raises(asyncio.CancelledError),
    ):
        await ch.fire_polling_trigger(
            trigger_id=TRIGGER_A,
            org_id=ORG,
            pipeline_id=PIPELINE,
            connector_instance_id=uuid.uuid4(),
            poll_query="SELECT 1",
            condition_expression=None,
        )


# ---------------------------------------------------------------------------
# Skipped-by-design branches (defensive arms with no observable difference)
# ---------------------------------------------------------------------------
# 1. `fire_cron_trigger` / `fire_polling_trigger` / `fire_report_trigger`
#    function-tail returns (1272, 1538, 1626): every body path inside the
#    ``async with`` block returns or raises, so the trailing
#    ``{"status": "error", "reason": "unexpected"}`` is unreachable.
