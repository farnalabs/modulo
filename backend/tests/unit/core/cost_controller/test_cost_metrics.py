"""Unit tests for the cost-breakdown metric counters (breakdown/metrics).

The counters are lazy-initialised against the OTel meter provider and must be
no-ops when the provider is missing or unavailable — the cost path must never
break because telemetry wiring is absent. ``_ensure`` initialises ALL four
handles in one call, so each record function is exercised both with a live
fake meter (asserting the matching handle + attributes) and with a None meter
(asserting the silent no-op guard).
"""

from __future__ import annotations

import pytest

import modulo.core.cost_controller.breakdown.metrics as metrics


class _FakeCounter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict] = []

    def add(self, value: int, attributes: dict | None = None) -> None:
        self.calls.append({"value": value, "attributes": attributes})


class _FakeGauge:
    def __init__(self, name: str) -> None:
        self.name = name
        self.values: list[float] = []

    def set(self, value: float) -> None:
        self.values.append(value)


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: list[_FakeCounter] = []
        self.gauges: list[_FakeGauge] = []

    def create_counter(self, *, name: str, description: str, unit: str) -> _FakeCounter:
        counter = _FakeCounter(name)
        self.counters.append(counter)
        return counter

    def counter(self, name: str) -> _FakeCounter | None:
        return next((c for c in self.counters if c.name == name), None)

    def create_gauge(self, *, name: str, description: str, unit: str) -> _FakeGauge:
        gauge = _FakeGauge(name)
        self.gauges.append(gauge)
        return gauge

    def gauge(self, name: str) -> _FakeGauge | None:
        return next((g for g in self.gauges if g.name == name), None)


@pytest.fixture
def fake_meter() -> _FakeMeter:
    return _FakeMeter()


@pytest.fixture(autouse=True)
def _reset_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "_eval_errors_total",
        "_clamped_total",
        "_out_of_band_high_total",
        "_settings_warning_total",
        "_probe_last_success_ts",
    ):
        monkeypatch.setattr(metrics, name, None)


def _stub_meter(monkeypatch: pytest.MonkeyPatch, meter: _FakeMeter | None) -> None:
    monkeypatch.setattr(metrics, "_get_meter", lambda: meter)


# ---------------------------------------------------------------------------
# _get_meter
# ---------------------------------------------------------------------------


def test_get_meter_missing_provider_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opentelemetry.metrics.get_meter_provider", lambda: None)
    assert metrics._get_meter() is None


def test_get_meter_provider_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opentelemetry.metrics.get_meter_provider", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert metrics._get_meter() is None


# ---------------------------------------------------------------------------
# record_* with a live meter — lazy init + attributed add
# ---------------------------------------------------------------------------


def test_record_eval_error_initialises_and_attributes(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_eval_error("sandbox_infra")
    counter = fake_meter.counter("modulo_cost_components_eval_errors_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": {"component": "sandbox_infra"}}]
    assert metrics._eval_errors_total is counter


def test_record_clamped_initialises_and_attributes(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_clamped("total_flat_clamp")
    counter = fake_meter.counter("modulo_cost_components_clamped_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": {"kind": "total_flat_clamp"}}]
    assert metrics._clamped_total is counter


def test_record_out_of_band_initialises_and_attributes(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_out_of_band("high")
    counter = fake_meter.counter("modulo_cost_components_out_of_band_high")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": {"direction": "high"}}]
    assert metrics._out_of_band_high_total is counter


def test_record_settings_warning_initialises(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_settings_warning()
    metrics.record_settings_warning()
    counter = fake_meter.counter("modulo_cost_settings_warning_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}, {"value": 1, "attributes": None}]
    assert metrics._settings_warning_total is counter


def test_lazy_init_is_once_only(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_eval_error("a")
    metrics.record_eval_error("b")
    counter = fake_meter.counter("modulo_cost_components_eval_errors_total")
    assert counter is not None
    assert counter.calls == [
        {"value": 1, "attributes": {"component": "a"}},
        {"value": 1, "attributes": {"component": "b"}},
    ]
    # No re-initialisation: the four handles stay the same objects.
    handles = (
        metrics._eval_errors_total,
        metrics._clamped_total,
        metrics._out_of_band_high_total,
        metrics._settings_warning_total,
    )
    metrics.record_eval_error("c")
    assert (
        metrics._eval_errors_total,
        metrics._clamped_total,
        metrics._out_of_band_high_total,
        metrics._settings_warning_total,
    ) == handles
    assert len(fake_meter.counters) == 15


# ---------------------------------------------------------------------------
# record_* with no meter — silent no-op
# ---------------------------------------------------------------------------


def test_record_functions_noop_without_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_meter(monkeypatch, None)
    metrics.record_eval_error("a")
    metrics.record_clamped("kind")
    metrics.record_out_of_band("high")
    metrics.record_settings_warning()
    assert metrics._eval_errors_total is None
    assert metrics._clamped_total is None
    assert metrics._out_of_band_high_total is None
    assert metrics._settings_warning_total is None


def test_ensure_early_return_when_handles_initialised(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics._ensure()
    metrics._ensure()
    # Only the first call builds handles; the second returns immediately.
    assert len(fake_meter.counters) == 15
    assert len(fake_meter.gauges) == 1
    assert metrics._eval_errors_total is fake_meter.counters[0]
