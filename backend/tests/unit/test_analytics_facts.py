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
    session.begin = MagicMock(return_value=_acm())
    session.begin_nested = MagicMock(return_value=_acm())
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

        update_keys = set(captured["set_"])
        assert {"status", "total_cost_usd", "total_tokens", "duration_ms", "run_date"} <= update_keys

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
        set_rows = MagicMock()
        set_ts = MagicMock()
        monkeypatch.setattr(maintenance_mod, "set_backfill_rows", set_rows)
        monkeypatch.setattr(maintenance_mod, "set_backfill_last_run_ts", set_ts)

        session = _session(execute_side_effect=[None] * 40)

        result = await maintenance_mod.backfill_batches(session, max_batches=3)

        assert result == {"backfill_rows": 12, "backfill_batches": 3}
        assert maintenance_mod.backfill_facts.await_count == 3
        set_rows.assert_called_once_with(12)
        set_ts.assert_called_once()

    async def test_does_not_need_more_batches_than_days(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(UTC).date()
        monkeypatch.setattr(maintenance_mod, "_subtract_months", lambda *a, **k: today - timedelta(days=1))
        monkeypatch.setattr(maintenance_mod, "backfill_facts", AsyncMock(return_value=1))
        set_rows = MagicMock()
        monkeypatch.setattr(maintenance_mod, "set_backfill_rows", set_rows)

        session = _session(execute_side_effect=[None] * 40)
        result = await maintenance_mod.backfill_batches(session, max_batches=30)

        assert result == {"backfill_rows": 2, "backfill_batches": 2}
        set_rows.assert_called_once_with(2)


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

    async def test_repairs_when_ledger_exceeds_facts_within_retention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
