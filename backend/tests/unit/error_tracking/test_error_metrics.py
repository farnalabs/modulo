"""Unit tests for error-tracking Prometheus metrics (modulo.core.error_tracking.metrics).

Covers OTel meter discovery, lazy counter/gauge registration, idempotency,
gauge-unavailable fallback, and the ingest/alert record helpers — all without a
meter provider or DB (``_get_meter`` is patched / OTel is stubbed via
``sys.modules``).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.error_tracking.metrics as metrics_mod


@pytest.fixture(autouse=True)
def _reset_metric_handles() -> None:
    """Save/restore module-level metric handles so tests never leak state."""
    saved = (
        metrics_mod._errors_total,
        metrics_mod._error_groups_active,
        metrics_mod._error_alerts_total,
        metrics_mod._runs_running_gauge,
        metrics_mod._runs_oldest_running_gauge,
        metrics_mod._runs_stall_reason_total,
        metrics_mod._runs_claim_count_histogram,
    )
    metrics_mod._errors_total = None
    metrics_mod._error_groups_active = None
    metrics_mod._error_alerts_total = None
    metrics_mod._runs_running_gauge = None
    metrics_mod._runs_oldest_running_gauge = None
    metrics_mod._runs_stall_reason_total = None
    metrics_mod._runs_claim_count_histogram = None
    yield
    (
        metrics_mod._errors_total,
        metrics_mod._error_groups_active,
        metrics_mod._error_alerts_total,
        metrics_mod._runs_running_gauge,
        metrics_mod._runs_oldest_running_gauge,
        metrics_mod._runs_stall_reason_total,
        metrics_mod._runs_claim_count_histogram,
    ) = saved


def _make_meter() -> MagicMock:
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock()
    meter.create_gauge.return_value = MagicMock()
    meter.create_histogram.return_value = MagicMock()
    return meter


@pytest.fixture
def fake_otel() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Inject a fake ``opentelemetry`` / ``opentelemetry.metrics`` into ``sys.modules``
    for the duration of the test and clean up afterwards so the stubs never shadow the
    real OTel package for other test modules running in the same process.

    Yields ``(meter, fake_metrics)`` where ``fake_metrics.get_meter_provider`` returns
    ``None`` by default; tests may override it to point at a fake provider.
    """
    fake_metrics = types.ModuleType("opentelemetry.metrics")
    meter = _make_meter()
    fake_metrics.get_meter_provider = MagicMock(return_value=None)
    fake_otel = types.ModuleType("opentelemetry")
    fake_otel.metrics = fake_metrics
    patcher = patch.dict(
        sys.modules,
        {"opentelemetry": fake_otel, "opentelemetry.metrics": fake_metrics},
    )
    patcher.start()
    try:
        yield meter, fake_metrics
    finally:
        patcher.stop()


# =========================================================================
# _get_meter — OTel discovery
# =========================================================================


class TestGetMeter:
    def test_returns_none_when_provider_is_none(self, fake_otel: tuple[MagicMock, MagicMock]) -> None:
        assert metrics_mod._get_meter() is None

    def test_returns_meter_from_provider(self, fake_otel: tuple[MagicMock, MagicMock]) -> None:
        meter, fake_metrics = fake_otel
        provider = MagicMock()
        provider.get_meter.return_value = meter
        fake_metrics.get_meter_provider.return_value = provider
        assert metrics_mod._get_meter() is meter
        provider.get_meter.assert_called_once_with("modulo.error_tracking", version="0.1.0")

    def test_returns_none_when_import_fails(self) -> None:
        with patch("builtins.__import__", side_effect=ImportError("no otel")):
            assert metrics_mod._get_meter() is None


# =========================================================================
# init_metrics
# =========================================================================


class TestInitMetrics:
    def test_no_meter_provider_leaves_handles_unset(self) -> None:
        with patch.object(metrics_mod, "_get_meter", return_value=None), patch.object(metrics_mod, "_log") as log:
            metrics_mod.init_metrics()
        assert metrics_mod._errors_total is None
        assert metrics_mod._error_groups_active is None
        log.warning.assert_called_once_with("metrics.no_meter_provider — OTel metrics disabled")

    def test_registers_counter_and_gauge(self) -> None:
        meter = _make_meter()
        with patch.object(metrics_mod, "_get_meter", return_value=meter), patch.object(metrics_mod, "_log") as log:
            metrics_mod.init_metrics()
        assert metrics_mod._errors_total is meter.create_counter.return_value
        assert metrics_mod._error_groups_active is meter.create_gauge.return_value
        meter.create_counter.assert_called_once()
        meter.create_gauge.assert_called_once()
        log.info.assert_called_once_with("metrics.registered")

    def test_idempotent_when_already_initialized(self) -> None:
        metrics_mod._errors_total = MagicMock()
        metrics_mod._error_groups_active = MagicMock()
        with patch.object(metrics_mod, "_get_meter") as get_meter, patch.object(metrics_mod, "_log") as log:
            metrics_mod.init_metrics()
        get_meter.assert_not_called()
        log.info.assert_not_called()

    def test_gauge_attribute_error_keeps_counter_only(self) -> None:
        meter = _make_meter()
        meter.create_gauge.side_effect = AttributeError("gauge unsupported")
        with patch.object(metrics_mod, "_get_meter", return_value=meter), patch.object(metrics_mod, "_log") as log:
            metrics_mod.init_metrics()
        assert metrics_mod._errors_total is meter.create_counter.return_value
        assert metrics_mod._error_groups_active is None
        log.warning.assert_called_once_with(
            "metrics.gauge_not_supported — OTel SDK version does not support create_gauge"
        )
        log.info.assert_called_once_with("metrics.registered")


# =========================================================================
# record_error_ingest
# =========================================================================


class TestRecordErrorIngest:
    def test_noop_when_counter_uninitialized(self) -> None:
        counter = MagicMock()
        with patch.object(metrics_mod, "_get_meter", return_value=None):
            metrics_mod.record_error_ingest("critical", "sdk", "prod")
        metrics_mod._errors_total = counter
        metrics_mod.record_error_ingest("critical", "sdk", "prod")
        assert metrics_mod._errors_total is counter

    def test_records_attributes(self) -> None:
        counter = MagicMock()
        metrics_mod._errors_total = counter
        metrics_mod.record_error_ingest("error", "sdk", "staging")
        counter.add.assert_called_once_with(
            1,
            attributes={"level": "error", "source": "sdk", "environment": "staging"},
        )

    def test_none_environment_maps_to_unknown(self) -> None:
        counter = MagicMock()
        metrics_mod._errors_total = counter
        metrics_mod.record_error_ingest("warning", "frontend", None)
        _, kwargs = counter.add.call_args
        assert kwargs["attributes"]["environment"] == "unknown"

    def test_empty_source_and_level_preserved(self) -> None:
        counter = MagicMock()
        metrics_mod._errors_total = counter
        metrics_mod.record_error_ingest("", "", None)
        _, kwargs = counter.add.call_args
        assert not kwargs["attributes"]["source"]
        assert not kwargs["attributes"]["level"]


# =========================================================================
# set_active_groups
# =========================================================================


class TestSetActiveGroups:
    def test_noop_when_gauge_uninitialized(self) -> None:
        gauge = MagicMock()
        metrics_mod.set_active_groups(3, "error")
        metrics_mod._error_groups_active = gauge
        metrics_mod.set_active_groups(3, "error")
        gauge.set.assert_called_once_with(3, attributes={"level": "error"})

    def test_sets_gauge_with_level(self) -> None:
        gauge = MagicMock()
        metrics_mod._error_groups_active = gauge
        metrics_mod.set_active_groups(0, "critical")
        gauge.set.assert_called_once_with(0, attributes={"level": "critical"})


# =========================================================================
# sample_error_group_metrics
# =========================================================================


class TestSampleErrorGroupMetrics:
    @pytest.mark.asyncio
    async def test_updates_gauge_per_level(self) -> None:
        """Active groups (new/acknowledged) are counted per level_peak and
        pushed into the gauge — resolved/archived groups never contribute."""
        from sqlalchemy.ext.asyncio import AsyncSession

        result = MagicMock()
        result.all.return_value = [("error", 4), ("critical", 1), ("warning", 0)]
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=result)
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        gauge = MagicMock()
        metrics_mod._error_groups_active = gauge

        await metrics_mod.sample_error_group_metrics(factory)

        assert gauge.set.call_count == 3
        gauge.set.assert_any_call(4, attributes={"level": "error"})
        gauge.set.assert_any_call(1, attributes={"level": "critical"})
        gauge.set.assert_any_call(0, attributes={"level": "warning"})
        # The query must restrict to active statuses.
        stmt = session.execute.call_args.args[0]
        assert "IN" in str(stmt)

    @pytest.mark.asyncio
    async def test_zero_levels_explicitly_zeroed(self) -> None:
        """Levels with no active groups are set to 0 so a drained level doesn't
        leave a stale gauge reading."""
        from sqlalchemy.ext.asyncio import AsyncSession

        result = MagicMock()
        result.all.return_value = []
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=result)
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        gauge = MagicMock()
        metrics_mod._error_groups_active = gauge

        await metrics_mod.sample_error_group_metrics(factory)

        assert gauge.set.call_count == 3
        gauge.set.assert_any_call(0, attributes={"level": "warning"})
        gauge.set.assert_any_call(0, attributes={"level": "error"})
        gauge.set.assert_any_call(0, attributes={"level": "critical"})

    @pytest.mark.asyncio
    async def test_failure_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        session = MagicMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        metrics_mod._error_groups_active = MagicMock()

        await metrics_mod.sample_error_group_metrics(factory)
        assert "metrics.sample_error_groups_failed" in caplog.text

    @pytest.mark.asyncio
    async def test_noop_when_gauge_uninitialized(self) -> None:
        """Without a gauge handle the sampler must not touch the DB at all."""
        with patch.object(metrics_mod, "init_metrics") as init:
            await metrics_mod.sample_error_group_metrics(MagicMock())
        init.assert_called_once()

    @pytest.mark.asyncio
    async def test_runs_against_sqlite_in_memory(self) -> None:
        """The sampler's query must compile and execute on SQLite — the same
        dialect used for unit-level DB tests. Exercises the real ORM path
        end-to-end, including the status filter and GROUP BY."""
        import uuid

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from modulo.db.models.base import Base
        from modulo.db.models.error_event import ErrorEvent
        from modulo.db.models.error_group import ErrorGroup

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(
                        sync_conn, tables=[ErrorEvent.__table__, ErrorGroup.__table__]
                    )
                )

            org_id = uuid.uuid4()
            async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
                for level, status in (("error", "new"), ("error", "acknowledged"), ("critical", "new")):
                    session.add(
                        ErrorGroup(
                            organisation_id=org_id,
                            fingerprint=uuid.uuid4().hex,
                            level_peak=level,
                            status=status,
                        )
                    )
                # Resolved/archived groups must not contribute to the active count.
                for status in ("resolved", "archived"):
                    session.add(
                        ErrorGroup(
                            organisation_id=org_id,
                            fingerprint=uuid.uuid4().hex,
                            level_peak="error",
                            status=status,
                        )
                    )

            gauge = MagicMock()
            metrics_mod._error_groups_active = gauge

            factory = async_sessionmaker(engine, expire_on_commit=False)
            await metrics_mod.sample_error_group_metrics(factory)

            assert gauge.set.call_count == 3
            gauge.set.assert_any_call(2, attributes={"level": "error"})
            gauge.set.assert_any_call(1, attributes={"level": "critical"})
            gauge.set.assert_any_call(0, attributes={"level": "warning"})
        finally:
            await engine.dispose()


# =========================================================================
# _init_alert_counter
# =========================================================================


class TestInitAlertCounter:
    def test_returns_when_already_initialized(self) -> None:
        metrics_mod._error_alerts_total = MagicMock()
        with patch.object(metrics_mod, "_get_meter") as get_meter:
            metrics_mod._init_alert_counter()
        get_meter.assert_not_called()

    def test_returns_when_no_meter(self) -> None:
        with patch.object(metrics_mod, "_get_meter", return_value=None), patch.object(metrics_mod, "_log"):
            metrics_mod._init_alert_counter()
        assert metrics_mod._error_alerts_total is None

    def test_creates_counter(self) -> None:
        meter = _make_meter()
        with patch.object(metrics_mod, "_get_meter", return_value=meter), patch.object(metrics_mod, "_log"):
            metrics_mod._init_alert_counter()
        assert metrics_mod._error_alerts_total is meter.create_counter.return_value
        meter.create_counter.assert_called_once_with(
            name="modulo_error_alerts_total",
            description="Total number of error alerts dispatched",
            unit="1",
        )

    def test_exception_leaves_counter_unset(self) -> None:
        meter = _make_meter()
        meter.create_counter.side_effect = RuntimeError("boom")
        with patch.object(metrics_mod, "_get_meter", return_value=meter), patch.object(metrics_mod, "_log") as log:
            metrics_mod._init_alert_counter()
        assert metrics_mod._error_alerts_total is None
        log.warning.assert_called_once_with("metrics.alert_counter_failed")


# =========================================================================
# record_error_alert
# =========================================================================


class TestRecordErrorAlert:
    def test_lazily_initializes_and_records(self) -> None:
        meter = _make_meter()
        with patch.object(metrics_mod, "_get_meter", return_value=meter):
            metrics_mod.record_error_alert("error", "email")
        counter = meter.create_counter.return_value
        counter.add.assert_called_once_with(
            1,
            attributes={"level": "error", "action_type": "email"},
        )

    def test_noop_when_no_meter_available(self) -> None:
        with patch.object(metrics_mod, "_get_meter", return_value=None):
            metrics_mod.record_error_alert("warning", "in_app")
        assert metrics_mod._error_alerts_total is None

    def test_records_without_reinitializing(self) -> None:
        counter = MagicMock()
        metrics_mod._error_alerts_total = counter
        with patch.object(metrics_mod, "_get_meter") as get_meter:
            metrics_mod.record_error_alert("critical", "webhook")
        get_meter.assert_not_called()
        counter.add.assert_called_once_with(
            1,
            attributes={"level": "critical", "action_type": "webhook"},
        )


# =========================================================================
# Run-runtime instruments (D1) — gauges/counter/histogram + sampling
# =========================================================================


class TestInitRuntimeMetrics:
    def test_no_meter_leaves_handles_unset(self) -> None:
        with patch.object(metrics_mod, "_get_meter", return_value=None):
            metrics_mod.init_runtime_metrics()
        assert metrics_mod._runs_running_gauge is None
        assert metrics_mod._runs_oldest_running_gauge is None
        assert metrics_mod._runs_stall_reason_total is None
        assert metrics_mod._runs_claim_count_histogram is None

    def test_registers_all_instruments(self) -> None:
        meter = _make_meter()
        with patch.object(metrics_mod, "_get_meter", return_value=meter):
            metrics_mod.init_runtime_metrics()
        assert metrics_mod._runs_running_gauge is meter.create_gauge.return_value
        assert metrics_mod._runs_oldest_running_gauge is meter.create_gauge.return_value
        assert metrics_mod._runs_stall_reason_total is meter.create_counter.return_value
        assert metrics_mod._runs_claim_count_histogram is meter.create_histogram.return_value
        names = [call.kwargs["name"] for call in meter.create_gauge.call_args_list]
        assert "runs_running_count" in names
        assert "runs_oldest_running_age_seconds" in names
        meter.create_counter.assert_called_once()
        assert meter.create_counter.call_args.kwargs["name"] == "runs_stall_reason_total"
        meter.create_histogram.assert_called_once()
        assert meter.create_histogram.call_args.kwargs["name"] == "runs_claim_count_total"

    def test_idempotent(self) -> None:
        metrics_mod._runs_running_gauge = MagicMock()
        metrics_mod._runs_oldest_running_gauge = MagicMock()
        metrics_mod._runs_stall_reason_total = MagicMock()
        metrics_mod._runs_claim_count_histogram = MagicMock()
        with patch.object(metrics_mod, "_get_meter") as get_meter:
            metrics_mod.init_runtime_metrics()
        get_meter.assert_not_called()

    def test_unsupported_instrument_skipped(self) -> None:
        meter = _make_meter()
        meter.create_histogram.side_effect = AttributeError("histogram unsupported")
        with patch.object(metrics_mod, "_get_meter", return_value=meter):
            metrics_mod.init_runtime_metrics()
        assert metrics_mod._runs_running_gauge is meter.create_gauge.return_value
        assert metrics_mod._runs_claim_count_histogram is None


class TestRecordStallReason:
    def test_lazily_initializes_and_records(self) -> None:
        meter = _make_meter()
        with patch.object(metrics_mod, "_get_meter", return_value=meter):
            metrics_mod.record_stall_reason("executor_superseded", 3)
        counter = meter.create_counter.return_value
        counter.add.assert_called_once_with(
            3,
            attributes={"stall_reason": "executor_superseded"},
        )

    def test_zero_count_noop(self) -> None:
        counter = MagicMock()
        metrics_mod._runs_stall_reason_total = counter
        metrics_mod.record_stall_reason("claim_cap_exhausted", 0)
        counter.add.assert_not_called()

    def test_noop_when_no_meter(self) -> None:
        with patch.object(metrics_mod, "_get_meter", return_value=None):
            metrics_mod.record_stall_reason("executor_stalled")
        assert metrics_mod._runs_stall_reason_total is None


class TestUpdateRunsLiveness:
    def test_sets_gauges(self) -> None:
        running_gauge = MagicMock()
        oldest_gauge = MagicMock()
        metrics_mod._runs_running_gauge = running_gauge
        metrics_mod._runs_oldest_running_gauge = oldest_gauge
        metrics_mod.update_runs_liveness(4, 123.5)
        running_gauge.set.assert_called_once_with(4)
        oldest_gauge.set.assert_called_once_with(123.5)

    def test_none_age_skips_oldest_gauge(self) -> None:
        running_gauge = MagicMock()
        oldest_gauge = MagicMock()
        metrics_mod._runs_running_gauge = running_gauge
        metrics_mod._runs_oldest_running_gauge = oldest_gauge
        metrics_mod.update_runs_liveness(0, None)
        running_gauge.set.assert_called_once_with(0)
        oldest_gauge.set.assert_not_called()


class TestSampleRunRuntimeMetrics:
    @pytest.mark.asyncio
    async def test_updates_gauges_and_histogram(self) -> None:
        """The sampler reads the runs table (running count, oldest age,
        per-run claim counts) and pushes them into the instruments."""
        from datetime import UTC, datetime

        from sqlalchemy.ext.asyncio import AsyncSession

        count_result = MagicMock()
        count_result.scalar_one.return_value = 2
        now_and_max_result = MagicMock()
        now_and_max_result.one.return_value = (
            datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        claim_rows = MagicMock()
        claim_rows.scalars.return_value = iter([1, 3])

        session = MagicMock(spec=AsyncSession)
        _results = [count_result, now_and_max_result, claim_rows]
        session.execute = AsyncMock(side_effect=lambda stmt, *a, **k: _results.pop(0))
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        running_gauge = MagicMock()
        oldest_gauge = MagicMock()
        histogram = MagicMock()
        metrics_mod._runs_running_gauge = running_gauge
        metrics_mod._runs_oldest_running_gauge = oldest_gauge
        metrics_mod._runs_claim_count_histogram = histogram

        await metrics_mod.sample_run_runtime_metrics(factory)

        running_gauge.set.assert_called_once_with(2)
        oldest_gauge.set.assert_called_once_with(30.0)
        assert histogram.record.call_count == 2

    @pytest.mark.asyncio
    async def test_failure_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        session = MagicMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        await metrics_mod.sample_run_runtime_metrics(factory)
        assert "metrics.sample_run_runtime_failed" in caplog.text

    @pytest.mark.asyncio
    async def test_runs_against_sqlite_in_memory(self) -> None:
        """The sampler's queries must compile and execute on SQLite — the old
        Postgres-only ``extract(epoch from (now() - MAX(started_at)))`` raw
        text query threw on every 60s dispatcher tick (only the broad except
        kept the tick alive). Exercises the real ORM path end-to-end."""
        import uuid
        from datetime import UTC, datetime, timedelta

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from modulo.db.models.base import Base
        from modulo.db.models.run import Run

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Run.__table__]))

            org_id = uuid.uuid4()
            now = datetime.now(UTC)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
                for idx, started in enumerate((now - timedelta(seconds=30), now - timedelta(seconds=10))):
                    session.add(
                        Run(
                            id=uuid.uuid4(),
                            organisation_id=org_id,
                            pipeline_id=uuid.uuid4(),
                            snapshot_id=uuid.uuid4(),
                            trigger_type="manual",
                            status="running",
                            run_number=idx + 1,
                            input_hash="a" * 64,
                            langgraph_thread_id=f"thread-{uuid.uuid4()}",
                            started_at=started,
                            claim_count=idx + 1,
                        )
                    )
                # A terminal run must not contribute to running count / liveness.
                session.add(
                    Run(
                        id=uuid.uuid4(),
                        organisation_id=org_id,
                        pipeline_id=uuid.uuid4(),
                        snapshot_id=uuid.uuid4(),
                        trigger_type="manual",
                        status="complete",
                        run_number=3,
                        input_hash="a" * 64,
                        langgraph_thread_id=f"thread-{uuid.uuid4()}",
                        started_at=now - timedelta(hours=1),
                    )
                )

            running_gauge = MagicMock()
            oldest_gauge = MagicMock()
            histogram = MagicMock()
            metrics_mod._runs_running_gauge = running_gauge
            metrics_mod._runs_oldest_running_gauge = oldest_gauge
            metrics_mod._runs_claim_count_histogram = histogram

            factory = async_sessionmaker(engine, expire_on_commit=False)
            await metrics_mod.sample_run_runtime_metrics(factory)

            running_gauge.set.assert_called_once_with(2)
            age = oldest_gauge.set.call_args[0][0]
            # The sampler computes ``current_timestamp - MAX(started_at)`` (the
            # age of the most recently started running run) — here ~10s since
            # the newest running run started 10s before ``now``.
            assert 0 < age < 40
            assert histogram.record.call_count == 2
        finally:
            await engine.dispose()
