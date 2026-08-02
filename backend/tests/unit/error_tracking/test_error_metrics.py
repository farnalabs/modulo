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
from unittest.mock import MagicMock, patch

import pytest

import modulo.core.error_tracking.metrics as metrics_mod


@pytest.fixture(autouse=True)
def _reset_metric_handles() -> None:
    """Save/restore module-level metric handles so tests never leak state."""
    saved = (
        metrics_mod._errors_total,
        metrics_mod._error_groups_active,
        metrics_mod._error_alerts_total,
    )
    metrics_mod._errors_total = None
    metrics_mod._error_groups_active = None
    metrics_mod._error_alerts_total = None
    yield
    metrics_mod._errors_total, metrics_mod._error_groups_active, metrics_mod._error_alerts_total = saved


def _make_meter() -> MagicMock:
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock()
    meter.create_gauge.return_value = MagicMock()
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
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock()
    meter.create_gauge.return_value = MagicMock()
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
        assert kwargs["attributes"]["source"] == ""
        assert kwargs["attributes"]["level"] == ""


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
