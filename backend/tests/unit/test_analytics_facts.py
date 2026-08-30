"""Unit tests for the analytics facts writer + metrics + maintenance (ADR 020).

The live writer (``record_run_facts``), the facts metric inventory
(``modulo.core.analytics.metrics``) and the maintenance pass (backfill /
reconcile / retention) previously had unit coverage only through the Postgres
integration suite. These tests cover the fail-open contract, the lazy metric
handles, the cooldown-keyed alert path and the maintenance loops with mocked
sessions so the semantics are pinned without a database.
"""

from __future__ import annotations

import asyncio
import builtins
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import opentelemetry.metrics as _otel_metrics
import pytest

import modulo.core.analytics as analytics_mod
from modulo.core.analytics import maintenance as maintenance_mod
from modulo.core.analytics import metrics as metrics_mod

# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.kind = kind
        self.calls: list[tuple] = []

    def add(self, value: int, attributes: dict | None = None) -> None:
        self.calls.append(("add", value, attributes))

    def set(self, value: float) -> None:
        self.calls.append(("set", value))


class _FakeMeter:
    def __init__(self) -> None:
        self.handles: dict[str, _FakeHandle] = {}

    def create_counter(self, name: str, description: str, unit: str) -> _FakeHandle:
        handle = _FakeHandle(name, "counter")
        self.handles[name] = handle
        return handle

    def create_gauge(self, name: str, description: str, unit: str) -> _FakeHandle:
        handle = _FakeHandle(name, "gauge")
        self.handles[name] = handle
        return handle


def _acm() -> AsyncMock:
    """An async context manager double (``async with x():``)."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _session(*, execute_side_effect) -> SimpleNamespace:
    session = SimpleNamespace()
    session.execute = AsyncMock(side_effect=execute_side_effect)
    # record_run_facts refreshes the run at the start (inside its fail-open
    # guard) so the fact snapshots the terminal row after a fenced status
    # write — the double mirrors a real AsyncSession.
    session.refresh = AsyncMock()
    session.begin = MagicMock(return_value=_acm())
    session.begin_nested = MagicMock(return_value=_acm())
    # backfill_facts / run_maintenance probe the dialect via
    # ``session.connection().dialect.name`` (e.g. to pick jsonb_* vs json_*
    # functions) — provide a generic (non-Postgres) connection double so those
    # code paths can run without a live engine.
    session.connection = AsyncMock(return_value=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    return session


def _scalar_one_result(value) -> SimpleNamespace:
    return SimpleNamespace(scalar_one=lambda: value, scalar_one_or_none=lambda: value)


# ---------------------------------------------------------------------------
# Facts writer (modulo.core.analytics.__init__)
# ---------------------------------------------------------------------------


def _make_run(**overrides) -> SimpleNamespace:
    defaults = {
        "id": "11111111-1111-4111-8111-111111111111",
        "organisation_id": "22222222-2222-4222-8222-222222222222",
        "started_at": datetime(2026, 8, 6, 10, 30, tzinfo=UTC),
        "created_at": datetime(2026, 8, 6, 10, 20, tzinfo=UTC),
        "completed_at": datetime(2026, 8, 6, 10, 31, 0, tzinfo=UTC),
        "owner_team_id": None,
        "pipeline_id": None,
        "trigger_type": "manual",
        "status": "complete",
        "total_cost_usd": Decimal("1.25"),
        "total_tokens": 500,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestFactRunDate:
    def test_uses_started_at_when_present(self) -> None:
        run = _make_run(
            started_at=datetime(2026, 8, 6, 23, 30, tzinfo=UTC),
            created_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
        )
        assert analytics_mod._fact_run_date(run) == date(2026, 8, 6)

    def test_falls_back_to_created_at_when_not_started(self) -> None:
        run = _make_run(started_at=None, created_at=datetime(2026, 8, 5, 1, 0, tzinfo=UTC))
        assert analytics_mod._fact_run_date(run) == date(2026, 8, 5)

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        run = _make_run(started_at=datetime(2026, 8, 6, 23, 0))
        assert analytics_mod._fact_run_date(run) == date(2026, 8, 6)

    def test_non_utc_offset_is_converted_to_utc(self) -> None:
        run = _make_run(started_at=datetime(2026, 8, 7, 1, 30, tzinfo=timezone(timedelta(hours=2))))
        assert analytics_mod._fact_run_date(run) == date(2026, 8, 6)

    def test_without_any_timestamp_returns_today(self) -> None:
        run = _make_run(started_at=None, created_at=None)
        assert analytics_mod._fact_run_date(run) == datetime.now(UTC).date()


class TestFactDurationMs:
    def test_computes_completed_minus_started(self) -> None:
        run = _make_run(
            started_at=datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
            completed_at=datetime(2026, 8, 6, 10, 1, 30, tzinfo=UTC),
        )
        assert analytics_mod._fact_duration_ms(run) == 90_000

    def test_subsecond_precision_is_kept(self) -> None:
        run = _make_run(
            started_at=datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC),
            completed_at=datetime(2026, 8, 6, 10, 0, 0, 123_000, tzinfo=UTC),
        )
        assert analytics_mod._fact_duration_ms(run) == 123

    def test_none_when_completed_missing(self) -> None:
        assert analytics_mod._fact_duration_ms(_make_run(completed_at=None)) is None

    def test_none_when_started_missing(self) -> None:
        assert analytics_mod._fact_duration_ms(_make_run(started_at=None)) is None


class TestSnapshotDimensions:
    async def test_resolves_team_and_pipeline(self) -> None:
        folder_id = "33333333-3333-4333-8333-333333333333"
        run = _make_run(
            owner_team_id="44444444-4444-4444-8444-444444444444",
            pipeline_id="55555555-5555-4555-8555-555555555555",
        )
        session = _session(
            execute_side_effect=[
                _scalar_one_result("Platform"),
                SimpleNamespace(first=lambda: ("CI", folder_id)),
            ]
        )
        team_name, pipeline_name, folder = await analytics_mod._snapshot_dimensions(session, run)
        assert (team_name, pipeline_name, folder) == ("Platform", "CI", folder_id)

    async def test_no_team_no_pipeline_returns_nones(self) -> None:
        run = _make_run()
        session = _session(execute_side_effect=[])
        team_name, pipeline_name, folder = await analytics_mod._snapshot_dimensions(session, run)
        assert (team_name, pipeline_name, folder) == (None, None, None)
        session.execute.assert_not_awaited()

    async def test_missing_pipeline_row_falls_back_to_none(self) -> None:
        run = _make_run(pipeline_id="55555555-5555-4555-8555-555555555555")
        session = _session(execute_side_effect=[SimpleNamespace(first=lambda: None)])
        team_name, pipeline_name, folder = await analytics_mod._snapshot_dimensions(session, run)
        assert (team_name, pipeline_name, folder) == (None, None, None)


class TestRecordRunFacts:
    def _capturing_insert(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        captured: dict = {}

        class _FakeInsert:
            _UPDATED_COLUMNS = (
                "status",
                "total_cost_usd",
                "total_tokens",
                "trigger_type",
                "team_id",
                "team_name",
                "pipeline_id",
                "pipeline_name",
                "folder_id",
                "run_date",
                "created_at",
                "duration_ms",
                "error_code",
                "claim_count",
                "queue_wait_ms",
                "final_idle_ms",
                "cancellation_requested",
                "dispatcher",
                "node_count",
                "sandbox_agent_node_count",
                "max_node_timeout_seconds",
                "parent_run_id",
                "snapshot_id",
                "batch_id",
                "run_number",
                "output_bytes",
                "telemetry_bytes",
                "rate_limited",
                "dispatched_at",
                "started_at",
                "completed_at",
                "total_queue_wait_ms",
            )

            def __init__(self, model) -> None:
                captured["model"] = model
                self.excluded = SimpleNamespace(**{col: col for col in self._UPDATED_COLUMNS})

            def values(self, **values) -> _FakeInsert:
                captured["values"] = values
                return self

            def on_conflict_do_update(self, index_elements=None, set_=None) -> _FakeInsert:
                captured["index_elements"] = index_elements
                captured["set_"] = set_
                return self

        monkeypatch.setattr(analytics_mod, "pg_insert", _FakeInsert)
        return captured

    async def test_writes_fact_with_expected_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._capturing_insert(monkeypatch)
        run = _make_run(
            owner_team_id="44444444-4444-4444-8444-444444444444",
            pipeline_id="55555555-5555-4555-8555-555555555555",
        )
        session = _session(
            execute_side_effect=[
                _scalar_one_result("Platform"),
                SimpleNamespace(first=lambda: ("CI", None)),
                SimpleNamespace(),
            ]
        )
        monkeypatch.setattr(analytics_mod, "record_facts_write_failed", MagicMock())

        await analytics_mod.record_run_facts(session, run)

        session.begin_nested.assert_called_once()
        assert session.execute.await_count == 3
        assert captured["model"] is analytics_mod.RunDailyFact
        assert len(captured["index_elements"]) == 1
        assert captured["index_elements"][0].key == "run_id"

        values = captured["values"]
        assert values["run_id"] == run.id
        assert values["organisation_id"] == run.organisation_id
        assert values["run_date"] == date(2026, 8, 6)
        assert values["team_id"] == run.owner_team_id
        assert values["team_name"] == "Platform"
        assert values["pipeline_id"] == run.pipeline_id
        assert values["pipeline_name"] == "CI"
        assert values["status"] == "complete"
        assert values["total_cost_usd"] == run.total_cost_usd
        assert values["duration_ms"] == 60_000
        # FAR-134 concurrency columns — the absolute instants pass through and
        # total_queue_wait_ms = started_at - created_at.
        assert values["dispatched_at"] is None  # not set on the fixture run
        assert values["started_at"] == run.started_at
        assert values["completed_at"] == run.completed_at
        assert values["total_queue_wait_ms"] == 600_000  # started 10:30 - created 10:20

        update_keys = set(captured["set_"])
        assert {"status", "total_cost_usd", "total_tokens", "duration_ms", "run_date"} <= update_keys
        assert {"dispatched_at", "started_at", "completed_at", "total_queue_wait_ms"} <= update_keys
        # FAR-332 — batch_id dimension is part of the fact write + update path.
        assert "batch_id" in update_keys

    async def test_writes_batch_id_dimension(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The run's batch_id is carried into the fact so batches are filterable."""
        captured = self._capturing_insert(monkeypatch)
        batch_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        run = _make_run(batch_id=batch_id)
        session = _session(
            execute_side_effect=[
                _scalar_one_result(None),
                SimpleNamespace(first=lambda: (None, None)),
                SimpleNamespace(),
            ]
        )
        monkeypatch.setattr(analytics_mod, "record_facts_write_failed", MagicMock())

        await analytics_mod.record_run_facts(session, run)

        assert captured["values"]["batch_id"] == batch_id

    async def test_batch_id_none_for_legacy_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run without a batch_id records a NULL batch_id (non-variant run)."""
        captured = self._capturing_insert(monkeypatch)
        run = _make_run()  # no batch_id attribute
        session = _session(
            execute_side_effect=[
                _scalar_one_result(None),
                SimpleNamespace(first=lambda: (None, None)),
                SimpleNamespace(),
            ]
        )
        monkeypatch.setattr(analytics_mod, "record_facts_write_failed", MagicMock())

        await analytics_mod.record_run_facts(session, run)

        assert captured["values"]["batch_id"] is None

    async def test_failure_is_swallowed_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = _make_run()
        session = _session(execute_side_effect=[RuntimeError("simulated facts insert failure")])
        write_failed = MagicMock()
        monkeypatch.setattr(analytics_mod, "record_facts_write_failed", write_failed)
        monkeypatch.setattr(analytics_mod, "_log", MagicMock())

        await analytics_mod.record_run_facts(session, run)  # must not raise

        write_failed.assert_called_once()
        analytics_mod._log.warning.assert_called_once()

    async def test_cancellation_is_not_swallowed(self) -> None:
        run = _make_run()
        session = _session(execute_side_effect=[asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await analytics_mod.record_run_facts(session, run)


class TestRecordFactForTerminalFailedRun:
    """P6' shared helper — the fail-open wrapper every raw terminal writer
    (SAQ task_failure hook, stale-run sweep, dispatcher_reconcile,
    fail_run_terminal) uses to record the compensating daily fact."""

    async def test_delegates_to_record_run_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = _make_run()
        session = _session(execute_side_effect=[])
        record = AsyncMock()
        monkeypatch.setattr(analytics_mod, "record_run_facts", record)
        await analytics_mod.record_fact_for_terminal_failed_run(session, run)
        record.assert_awaited_once_with(session, run)

    async def test_none_run_is_skipped(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        session = _session(execute_side_effect=[])
        record = AsyncMock()
        monkeypatch.setattr(analytics_mod, "record_run_facts", record)
        with caplog.at_level("WARNING", logger="modulo.core.analytics"):
            await analytics_mod.record_fact_for_terminal_failed_run(session, None)
        record.assert_not_awaited()
        assert any("run_missing" in m for m in caplog.messages)

    async def test_write_failure_is_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        run = _make_run()
        session = _session(execute_side_effect=[])

        async def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("facts boom")

        monkeypatch.setattr(analytics_mod, "record_run_facts", _boom)
        with caplog.at_level("ERROR", logger="modulo.core.analytics"):
            await analytics_mod.record_fact_for_terminal_failed_run(session, run)  # must not raise
        assert any("terminal_failed_facts_failed" in m for m in caplog.messages)

    async def test_cancellation_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = _make_run()
        session = _session(execute_side_effect=[])

        async def _cancel(*args: object, **kwargs: object) -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(analytics_mod, "record_run_facts", _cancel)
        with pytest.raises(asyncio.CancelledError):
            await analytics_mod.record_fact_for_terminal_failed_run(session, run)


# ---------------------------------------------------------------------------
# Metrics (modulo.core.analytics.metrics)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics_handles() -> None:
    yield
    for name in (
        "_facts_write_failed_total",
        "_backfill_last_run_ts",
        "_backfill_rows",
        "_reconcile_alert_total",
        "_retention_lag",
        "_facts_skip_non_pg_total",
    ):
        setattr(metrics_mod, name, None)


class TestGetMeter:
    def test_returns_none_when_no_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_otel_metrics, "get_meter_provider", lambda: None)
        assert metrics_mod._get_meter() is None

    def test_returns_meter_from_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = SimpleNamespace(name="modulo.analytics")
        monkeypatch.setattr(
            _otel_metrics, "get_meter_provider", lambda: SimpleNamespace(get_meter=lambda *a, **k: meter)
        )
        assert metrics_mod._get_meter() is meter

    def test_returns_none_when_import_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__

        def _no_opentelemetry(name, *args, **kwargs):
            if name == "opentelemetry":
                raise ImportError("opentelemetry is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_opentelemetry)
        assert metrics_mod._get_meter() is None

    def test_returns_none_when_provider_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args, **kwargs) -> None:
            raise RuntimeError("no telemetry configured")

        monkeypatch.setattr(_otel_metrics, "get_meter_provider", _boom)
        assert metrics_mod._get_meter() is None


class TestEnsure:
    def test_noop_when_meter_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: None)
        metrics_mod._ensure()
        assert metrics_mod._facts_write_failed_total is None

    def test_creates_all_handles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod._ensure()
        assert metrics_mod._facts_write_failed_total is meter.handles["modulo_facts_write_failed_total"]
        assert metrics_mod._backfill_last_run_ts is meter.handles["modulo_facts_backfill_last_run_ts"]
        assert metrics_mod._backfill_rows is meter.handles["modulo_facts_backfill_rows"]
        assert metrics_mod._reconcile_alert_total is meter.handles["modulo_facts_reconcile_alert_total"]
        assert metrics_mod._retention_lag is meter.handles["modulo_facts_retention_lag"]
        assert metrics_mod._facts_skip_non_pg_total is meter.handles["modulo_facts_skip_non_pg_total"]

    def test_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod._ensure()
        metrics_mod._ensure()
        assert len(meter.handles) == 6, "handles must not be re-created on the second ensure"


class TestRecorders:
    def test_noop_when_handles_uninitialised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: None)
        metrics_mod.record_facts_write_failed()
        metrics_mod.set_backfill_last_run_ts(1234.5)
        metrics_mod.set_backfill_rows(7)
        metrics_mod.record_reconcile_alert("org-1", "ledger_exceeds_facts")
        metrics_mod.set_retention_lag(2.0)
        metrics_mod.record_facts_skip_non_pg()
        assert metrics_mod._facts_write_failed_total is None
        assert metrics_mod._backfill_last_run_ts is None
        assert metrics_mod._backfill_rows is None
        assert metrics_mod._reconcile_alert_total is None
        assert metrics_mod._retention_lag is None
        assert metrics_mod._facts_skip_non_pg_total is None

    def test_record_facts_write_failed_lazily_initialises_and_adds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod.record_facts_write_failed()
        assert metrics_mod._facts_write_failed_total.calls == [("add", 1, None)]

    def test_set_backfill_last_run_ts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod.set_backfill_last_run_ts(1234.5)
        assert metrics_mod._backfill_last_run_ts.calls == [("set", 1234.5)]

    def test_set_backfill_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod.set_backfill_rows(9)
        assert metrics_mod._backfill_rows.calls == [("set", 9)]

    def test_record_reconcile_alert_includes_attributes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod.record_reconcile_alert("org-1", "ledger_exceeds_facts")
        assert metrics_mod._reconcile_alert_total.calls == [
            ("add", 1, {"org_id": "org-1", "drift_type": "ledger_exceeds_facts"})
        ]

    def test_set_retention_lag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod.set_retention_lag(3.0)
        assert metrics_mod._retention_lag.calls == [("set", 3.0)]

    def test_record_facts_skip_non_pg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        monkeypatch.setattr(metrics_mod, "_get_meter", lambda: meter)
        metrics_mod.record_facts_skip_non_pg()
        assert metrics_mod._facts_skip_non_pg_total.calls == [("add", 1, None)]


# ---------------------------------------------------------------------------
# Maintenance (modulo.core.analytics.maintenance)
# ---------------------------------------------------------------------------


class TestSubtractMonths:
    @pytest.mark.parametrize(
        ("day", "months", "expected"),
        [
            (date(2026, 8, 7), 1, date(2026, 7, 7)),
            (date(2026, 8, 7), 13, date(2025, 7, 7)),
            (date(2026, 8, 7), 24, date(2024, 8, 7)),
            (date(2026, 1, 15), 2, date(2025, 11, 15)),
            (date(2026, 3, 31), 1, date(2026, 2, 28)),
            (date(2024, 3, 31), 1, date(2024, 2, 29)),
            (date(2026, 5, 31), 1, date(2026, 4, 30)),
            (date(2026, 12, 31), 1, date(2026, 11, 30)),
        ],
        ids=[
            "1m-back",
            "13m-leap-year",
            "24m-back",
            "jan-clamp",
            "mar31-feb",
            "feb29-leap",
            "may31-apr",
            "dec31-nov",
        ],
    )
    def test_subtracts_months_with_day_clamping(self, day: date, months: int, expected: date) -> None:
        assert maintenance_mod._subtract_months(day, months) == expected


class TestDialectName:
    async def test_returns_dialect_name(self) -> None:
        session = SimpleNamespace()
        session.connection = AsyncMock(return_value=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
        assert await maintenance_mod._dialect_name(session) == "postgresql"


class TestBackfillBatches:
    async def test_honours_max_batches_and_reports_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(UTC).date()
        monkeypatch.setattr(maintenance_mod, "_subtract_months", lambda *a, **k: today - timedelta(days=10))
        monkeypatch.setattr(maintenance_mod, "backfill_facts", AsyncMock(return_value=4))
        monkeypatch.setattr(maintenance_mod, "backfill_ledger", AsyncMock(return_value=1))
        set_rows = MagicMock()
        set_ts = MagicMock()
        monkeypatch.setattr(maintenance_mod, "set_backfill_rows", set_rows)
        monkeypatch.setattr(maintenance_mod, "set_backfill_last_run_ts", set_ts)

        session = _session(execute_side_effect=[None] * 40)

        result = await maintenance_mod.backfill_batches(session, max_batches=3)

        assert result == {"backfill_rows": 12, "backfill_batches": 3}
        assert maintenance_mod.backfill_facts.await_count == 3
        # NEWEST-first ordering — the recent window the dashboard reads is
        # filled before any older day: today, today-1, today-2.
        assert maintenance_mod.backfill_facts.await_args_list[0].args[1] == today
        assert maintenance_mod.backfill_facts.await_args_list[1].args[1] == today - timedelta(days=1)
        assert maintenance_mod.backfill_facts.await_args_list[2].args[1] == today - timedelta(days=2)
        # One ledger upsert per batch, same day as the facts backfill.
        assert maintenance_mod.backfill_ledger.await_count == 3
        for i in range(3):
            assert (
                maintenance_mod.backfill_ledger.await_args_list[i].args[1]
                == maintenance_mod.backfill_facts.await_args_list[i].args[1]
            )
        set_rows.assert_called_once_with(12)
        set_ts.assert_called_once()

    async def test_does_not_need_more_batches_than_days(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(UTC).date()
        monkeypatch.setattr(maintenance_mod, "_subtract_months", lambda *a, **k: today - timedelta(days=1))
        monkeypatch.setattr(maintenance_mod, "backfill_facts", AsyncMock(return_value=1))
        monkeypatch.setattr(maintenance_mod, "backfill_ledger", AsyncMock(return_value=1))
        set_rows = MagicMock()
        monkeypatch.setattr(maintenance_mod, "set_backfill_rows", set_rows)

        session = _session(execute_side_effect=[None] * 40)
        result = await maintenance_mod.backfill_batches(session, max_batches=30)

        assert result == {"backfill_rows": 2, "backfill_batches": 2}
        assert maintenance_mod.backfill_facts.await_count == 2
        assert maintenance_mod.backfill_ledger.await_count == 2
        # Newest-first over the 2-day range: today then today-1.
        assert maintenance_mod.backfill_facts.await_args_list[0].args[1] == today
        assert maintenance_mod.backfill_facts.await_args_list[1].args[1] == today - timedelta(days=1)
        for i in range(2):
            assert (
                maintenance_mod.backfill_ledger.await_args_list[i].args[1]
                == maintenance_mod.backfill_facts.await_args_list[i].args[1]
            )
        set_rows.assert_called_once_with(2)


class TestRepairStaleFacts:
    async def test_deletes_stale_non_terminal_facts_and_returns_rowcount(self) -> None:
        result = MagicMock()
        result.rowcount = 3
        session = _session(execute_side_effect=[result])

        deleted = await maintenance_mod.repair_stale_facts(session, date(2026, 8, 12))

        assert deleted == 3
        assert session.execute.await_count == 1
        executed_stmt = session.execute.await_args_list[0].args[0]
        assert executed_stmt.is_delete, "repair must issue a DELETE"
        assert "run_daily_facts" in str(executed_stmt).lower()

    async def test_zero_when_no_stale_rows(self) -> None:
        result = MagicMock()
        result.rowcount = 0
        session = _session(execute_side_effect=[result])

        deleted = await maintenance_mod.repair_stale_facts(session, date(2026, 8, 13))

        assert deleted == 0
        assert session.execute.await_count == 1


class TestBackfillFactsRepairsFirst:
    async def test_repairs_stale_rows_before_inserting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        insert_result = MagicMock()
        insert_result.rowcount = 4
        session = _session(execute_side_effect=[insert_result])
        monkeypatch.setattr(maintenance_mod, "repair_stale_facts", AsyncMock(return_value=2))

        inserted = await maintenance_mod.backfill_facts(session, date(2026, 8, 12))

        assert inserted == 4
        maintenance_mod.repair_stale_facts.assert_awaited_once_with(session, date(2026, 8, 12))
        # The INSERT is the only other statement executed.
        assert session.execute.await_count == 1

    async def test_repair_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _session(execute_side_effect=[None])
        monkeypatch.setattr(maintenance_mod, "repair_stale_facts", AsyncMock(side_effect=RuntimeError("boom")))

        with pytest.raises(RuntimeError, match="boom"):
            await maintenance_mod.backfill_facts(session, date(2026, 8, 12))
        maintenance_mod.repair_stale_facts.assert_awaited_once()
        assert session.execute.await_count == 0, "no INSERT runs when the repair raises"


class TestBackfillLedger:
    @staticmethod
    def _capturing_insert(monkeypatch: pytest.MonkeyPatch) -> dict:
        captured: dict = {}

        class _FakeInsert:
            def __init__(self, model) -> None:
                captured["model"] = model

            def values(self, rows) -> _FakeInsert:
                captured["values"] = rows
                return self

            def on_conflict_do_nothing(self, index_elements=None) -> _FakeInsert:
                captured["index_elements"] = index_elements
                captured["on_conflict"] = "do_nothing"
                return self

        monkeypatch.setattr(maintenance_mod, "pg_insert", _FakeInsert)
        return captured

    async def test_backfills_ledger_per_day(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = date(2026, 8, 6)
        org_a = "22222222-2222-4222-8222-222222222222"
        org_b = "33333333-3333-4333-8333-333333333333"
        captured = self._capturing_insert(monkeypatch)
        session = _session(
            execute_side_effect=[
                SimpleNamespace(all=lambda: [(org_a, 3, Decimal("1.25")), (org_b, 1, Decimal("0.50"))]),
                SimpleNamespace(rowcount=2),
            ]
        )

        result = await maintenance_mod.backfill_ledger(session, day)

        assert result == 2
        assert captured["model"] is maintenance_mod.OrgDailyRunCount
        assert len(captured["values"]) == 2
        assert captured["values"][0]["organisation_id"] == org_a
        assert captured["values"][0]["team_id"] is None  # org-level row
        assert captured["values"][0]["run_date"] == day
        assert captured["values"][0]["run_count"] == 3
        assert captured["values"][0]["total_spend_usd"] == Decimal("1.25")
        assert captured["values"][0]["clamped"] is False
        assert captured["values"][0]["refused_spend_usd"] == Decimal(0)
        assert captured["values"][1]["organisation_id"] == org_b
        assert captured["values"][1]["team_id"] is None
        # ON CONFLICT target (the unique index incl. NULLS NOT DISTINCT) with
        # DO NOTHING — a day with an existing live org-level row is left
        # untouched (no update path exists).
        assert captured["on_conflict"] == "do_nothing"
        assert [c.key for c in captured["index_elements"]] == ["organisation_id", "team_id", "run_date"]
        assert "set_" not in captured
        # Aggregate query mirrors the live writer: terminal-status + UTC-day
        # predicate, grouped by org, EXCLUDING refused and zero-cost runs.
        agg_stmt = session.execute.await_args_list[0].args[0]
        agg_sql = str(agg_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "GROUP BY runs.organisation_id" in agg_sql
        assert "runs.status IN" in agg_sql
        assert "runs.ledger_refused_at IS NULL" in agg_sql
        assert "runs.total_cost_usd > 0" in agg_sql
        assert "date_trunc('day', coalesce(runs.started_at, runs.created_at))" in agg_sql
        assert "'2026-08-06'" in agg_sql

    async def test_returns_zero_when_no_terminal_runs_for_the_day(self) -> None:
        session = _session(execute_side_effect=[SimpleNamespace(all=list)])
        result = await maintenance_mod.backfill_ledger(session, date(2026, 8, 6))
        assert result == 0
        # No orgs to upsert -> the insert statement is never built/executed.
        assert session.execute.await_count == 1

    async def test_does_not_overwrite_a_live_written_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = date(2026, 8, 6)
        org_a = "22222222-2222-4222-8222-222222222222"
        captured = self._capturing_insert(monkeypatch)
        session = _session(
            execute_side_effect=[
                SimpleNamespace(all=lambda: [(org_a, 3, Decimal("1.25"))]),
                # rowcount 0 = the conflict was swallowed by DO NOTHING, so the
                # pre-existing live org-level row is preserved untouched.
                SimpleNamespace(rowcount=0),
            ]
        )

        result = await maintenance_mod.backfill_ledger(session, day)

        assert result == 0
        assert captured["on_conflict"] == "do_nothing"
        assert "set_" not in captured
        assert len(captured["values"]) == 1

    async def test_idempotent_same_day_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = date(2026, 8, 6)
        org_a = "22222222-2222-4222-8222-222222222222"
        captured = self._capturing_insert(monkeypatch)
        for _ in range(2):
            session = _session(
                execute_side_effect=[
                    SimpleNamespace(all=lambda: [(org_a, 3, Decimal("1.25"))]),
                    SimpleNamespace(rowcount=1),
                ]
            )
            assert await maintenance_mod.backfill_ledger(session, day) == 1
        # DO NOTHING: a second pass never duplicates or overwrites — the unique
        # (org, team, run_date) index swallows the conflict by construction.
        assert captured["on_conflict"] == "do_nothing"
        assert [c.key for c in captured["index_elements"]] == ["organisation_id", "team_id", "run_date"]

    async def test_clamps_spend_to_column_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = date(2026, 8, 6)
        org_a = "22222222-2222-4222-8222-222222222222"
        captured = self._capturing_insert(monkeypatch)
        session = _session(
            execute_side_effect=[
                SimpleNamespace(all=lambda: [(org_a, 5, maintenance_mod.COST_COLUMN_CAP + Decimal("0.01"))]),
                SimpleNamespace(rowcount=1),
            ]
        )

        result = await maintenance_mod.backfill_ledger(session, day)

        assert result == 1
        assert captured["values"][0]["total_spend_usd"] == maintenance_mod.COST_COLUMN_CAP
        assert captured["values"][0]["clamped"] is True


class TestReconcileFacts:
    async def _run(self, ledger_rows, facts_totals, monkeypatch: pytest.MonkeyPatch, *, today: date | None = None):
        monkeypatch.setattr(maintenance_mod, "backfill_facts", AsyncMock(return_value=0))
        set_alert = MagicMock()
        monkeypatch.setattr(maintenance_mod, "record_reconcile_alert", set_alert)
        results = [SimpleNamespace(all=lambda: ledger_rows)]
        results += [_scalar_one_result(t) for t in facts_totals]
        session = _session(execute_side_effect=results)
        return await maintenance_mod.reconcile_facts(session, today=today), set_alert

    @staticmethod
    def _ledger_row(org: str, run_date: date, spend) -> tuple:
        return (org, run_date, spend)

    async def test_repairs_when_ledger_exceeds_facts_within_retention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        maintenance_mod._reconcile_cooldown.clear()
        today = date(2026, 8, 7)
        ledger = [self._ledger_row("org-1", today - timedelta(days=1), Decimal(100))]
        stats, set_alert = await self._run(ledger, [Decimal(40)], monkeypatch, today=today)
        assert stats == {"reconcile_alerts": 0, "reconcile_repaired": 1, "reconcile_tolerated": 0}
        maintenance_mod.backfill_facts.assert_awaited_once()
        set_alert.assert_not_called()

    async def test_alerts_when_drift_is_beyond_run_retention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        maintenance_mod._reconcile_cooldown.clear()
        today = date(2026, 8, 7)
        stale_day = today - timedelta(days=maintenance_mod._RUN_RETENTION_DAYS + 1)
        ledger = [self._ledger_row("org-1", stale_day, Decimal(100))]
        stats, set_alert = await self._run(ledger, [Decimal(0)], monkeypatch, today=today)
        assert stats == {"reconcile_alerts": 1, "reconcile_repaired": 0, "reconcile_tolerated": 0}
        maintenance_mod.backfill_facts.assert_not_awaited()
        set_alert.assert_called_once_with("org-1", "ledger_exceeds_facts")

    async def test_alert_is_suppressed_within_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        today = date(2026, 8, 7)
        stale_day = today - timedelta(days=maintenance_mod._RUN_RETENTION_DAYS + 1)
        ledger = [self._ledger_row("org-1", stale_day, Decimal(100))]
        maintenance_mod._reconcile_cooldown[("org-1", "ledger_exceeds_facts")] = time.monotonic()
        stats, set_alert = await self._run(ledger, [Decimal(0)], monkeypatch, today=today)
        assert stats["reconcile_alerts"] == 0
        set_alert.assert_not_called()
        maintenance_mod._reconcile_cooldown.clear()

    async def test_tolerates_facts_exceeding_ledger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date(2026, 8, 7)
        ledger = [self._ledger_row("org-1", today - timedelta(days=1), Decimal(40))]
        stats, _ = await self._run(ledger, [Decimal(100)], monkeypatch, today=today)
        assert stats == {"reconcile_alerts": 0, "reconcile_repaired": 0, "reconcile_tolerated": 1}
        maintenance_mod.backfill_facts.assert_not_awaited()

    async def test_none_ledger_total_is_treated_as_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date(2026, 8, 7)
        ledger = [self._ledger_row("org-1", today - timedelta(days=1), None)]
        stats, _ = await self._run(ledger, [Decimal(10)], monkeypatch, today=today)
        assert stats["reconcile_tolerated"] == 1

    async def test_equal_totals_are_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date(2026, 8, 7)
        ledger = [self._ledger_row("org-1", today - timedelta(days=1), Decimal(50))]
        stats, _ = await self._run(ledger, [Decimal(50)], monkeypatch, today=today)
        assert stats == {"reconcile_alerts": 0, "reconcile_repaired": 0, "reconcile_tolerated": 0}

    async def test_multi_org_drift_counts_each(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date(2026, 8, 7)
        ledger = [
            self._ledger_row("org-1", today - timedelta(days=1), Decimal(100)),
            self._ledger_row("org-2", today - timedelta(days=2), Decimal(80)),
        ]
        stats, _ = await self._run(ledger, [Decimal(40), Decimal(40)], monkeypatch, today=today)
        assert stats["reconcile_repaired"] == 2
        assert maintenance_mod.backfill_facts.await_count == 2


class TestRetentionFacts:
    async def test_noop_when_minimum_is_within_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cutoff = date(2026, 8, 7)
        session = _session(
            execute_side_effect=[
                _scalar_one_result(cutoff),
                _scalar_one_result(cutoff),
            ]
        )
        set_lag = MagicMock()
        monkeypatch.setattr(maintenance_mod, "set_retention_lag", set_lag)

        result = await maintenance_mod.retention_facts(session, cutoff=cutoff)

        assert result == {"retention_deleted": 0}
        set_lag.assert_called_once()

    async def test_deletes_old_days_in_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cutoff = date(2026, 8, 7)
        oldest = cutoff - timedelta(days=20)
        session = _session(
            execute_side_effect=[
                _scalar_one_result(oldest),
                SimpleNamespace(rowcount=2),
                _scalar_one_result(oldest + timedelta(days=7)),
                SimpleNamespace(rowcount=2),
                _scalar_one_result(oldest + timedelta(days=14)),
                SimpleNamespace(rowcount=2),
                _scalar_one_result(cutoff),
                _scalar_one_result(cutoff),
            ]
        )
        set_lag = MagicMock()
        monkeypatch.setattr(maintenance_mod, "set_retention_lag", set_lag)

        result = await maintenance_mod.retention_facts(session, cutoff=cutoff, chunk_days=7)

        assert result == {"retention_deleted": 6}
        set_lag.assert_called_once()

    async def test_uses_settings_derived_cutoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(UTC).date()
        settings = SimpleNamespace(analytics_facts_retention_months="6")
        monkeypatch.setattr(maintenance_mod, "get_settings", lambda: settings)
        subtract_months = MagicMock(return_value=today - timedelta(days=1))
        monkeypatch.setattr(maintenance_mod, "_subtract_months", subtract_months)
        session = _session(
            execute_side_effect=[
                _scalar_one_result(today - timedelta(days=2)),
                SimpleNamespace(rowcount=1),
                _scalar_one_result(None),
                _scalar_one_result(None),
            ]
        )
        result = await maintenance_mod.retention_facts(session)
        assert result == {"retention_deleted": 1}
        assert subtract_months.call_args[0] == (today, 6)

    async def test_invalid_settings_falls_back_to_default_months(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(UTC).date()
        settings = SimpleNamespace(analytics_facts_retention_months="not-a-number")
        monkeypatch.setattr(maintenance_mod, "get_settings", lambda: settings)
        subtract_months = MagicMock(return_value=today - timedelta(days=1))
        monkeypatch.setattr(maintenance_mod, "_subtract_months", subtract_months)
        session = _session(
            execute_side_effect=[
                _scalar_one_result(today - timedelta(days=2)),
                SimpleNamespace(rowcount=1),
                _scalar_one_result(None),
                _scalar_one_result(None),
            ]
        )
        await maintenance_mod.retention_facts(session)
        assert subtract_months.call_args[0] == (today, maintenance_mod._FACTS_RETENTION_MONTHS)

    async def test_missing_settings_attribute_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(UTC).date()
        monkeypatch.setattr(maintenance_mod, "get_settings", lambda: SimpleNamespace())
        subtract_months = MagicMock(return_value=today - timedelta(days=1))
        monkeypatch.setattr(maintenance_mod, "_subtract_months", subtract_months)
        session = _session(
            execute_side_effect=[
                _scalar_one_result(today - timedelta(days=2)),
                SimpleNamespace(rowcount=1),
                _scalar_one_result(None),
                _scalar_one_result(None),
            ]
        )
        await maintenance_mod.retention_facts(session)
        assert subtract_months.call_args[0] == (today, maintenance_mod._FACTS_RETENTION_MONTHS)


class TestRunMaintenance:
    @staticmethod
    def _factory_for(session):
        class _FactoryCM:
            def __init__(self, target) -> None:
                self._target = target

            async def __aenter__(self) -> object:
                return self._target

            async def __aexit__(self, *exc) -> bool:
                return False

        return lambda: _FactoryCM(session)

    def _postgres_session(self) -> SimpleNamespace:
        session = SimpleNamespace()
        session.connection = AsyncMock(return_value=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
        session.begin = MagicMock(return_value=_acm())
        session.execute = AsyncMock()
        return session

    async def test_skips_non_postgres_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = SimpleNamespace()
        session.connection = AsyncMock(return_value=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
        session.begin = MagicMock(return_value=_acm())
        skip = MagicMock()
        monkeypatch.setattr(maintenance_mod, "record_facts_skip_non_pg", skip)

        result = await maintenance_mod.run_maintenance(self._factory_for(session))

        assert result == {"skipped": True, "reason": "non_postgres"}
        skip.assert_called_once()

    async def test_runs_full_pass_on_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = self._postgres_session()
        monkeypatch.setattr(
            maintenance_mod, "backfill_batches", AsyncMock(return_value={"backfill_rows": 5, "backfill_batches": 1})
        )
        monkeypatch.setattr(
            maintenance_mod,
            "reconcile_facts",
            AsyncMock(return_value={"reconcile_alerts": 0, "reconcile_repaired": 0, "reconcile_tolerated": 0}),
        )
        monkeypatch.setattr(maintenance_mod, "retention_facts", AsyncMock(return_value={"retention_deleted": 0}))

        result = await maintenance_mod.run_maintenance(self._factory_for(session))

        assert result["skipped"] is False
        assert result.get("maintenance_failed") is not True
        assert result["backfill_rows"] == 5
        assert result["reconcile_alerts"] == 0
        assert result["retention_deleted"] == 0

    async def test_exception_is_caught_and_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = self._postgres_session()

        def _boom(*args, **kwargs):
            raise RuntimeError("maintenance crashed")

        monkeypatch.setattr(maintenance_mod, "backfill_batches", _boom)
        log = MagicMock()
        monkeypatch.setattr(maintenance_mod, "_log", log)

        result = await maintenance_mod.run_maintenance(self._factory_for(session))

        assert result["skipped"] is False
        assert result["maintenance_failed"] is True
        log.exception.assert_called_once()
