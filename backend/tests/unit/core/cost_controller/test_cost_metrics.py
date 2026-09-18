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

from modulo.core.cost_controller.breakdown import metrics


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


_ALL_HANDLE_NAMES = (
    "_eval_errors_total",
    "_clamped_total",
    "_out_of_band_high_total",
    "_settings_warning_total",
    "_fallback_legacy_total",
    "_ledger_clamped_total",
    "_ledger_refused_clamped_total",
    "_finalize_deferred_total",
    "_limit_refused_total",
    "_duplicate_terminal_total",
    "_probe_mismatch_runs_total",
    "_probe_total_eq_mismatch_total",
    "_probe_clamped_skip_total",
    "_probe_missing_ledger_row_total",
    "_probe_last_success_ts",
    "_schema_drift_total",
)


@pytest.fixture(autouse=True)
def _reset_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_HANDLE_NAMES:
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


# ---------------------------------------------------------------------------
# record_* ledger + probe counters and the gauge — with a live meter
# ---------------------------------------------------------------------------


def test_record_fallback_legacy_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_fallback_legacy()
    counter = fake_meter.counter("modulo_cost_components_fallback_legacy_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}]
    assert metrics._fallback_legacy_total is counter


def test_record_ledger_clamped_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_ledger_clamped()
    counter = fake_meter.counter("modulo_cost_ledger_clamped_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}]
    assert metrics._ledger_clamped_total is counter


def test_record_ledger_refused_clamped_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_ledger_refused_clamped()
    counter = fake_meter.counter("modulo_cost_ledger_refused_clamped_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}]
    assert metrics._ledger_refused_clamped_total is counter


def test_record_finalize_deferred_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_finalize_deferred("write_failure", "team-a")
    counter = fake_meter.counter("modulo_cost_ledger_finalize_deferred_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": {"reason": "write_failure", "team": "team-a"}}]
    assert metrics._finalize_deferred_total is counter


def test_record_limit_refused_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_limit_refused("team-a")
    counter = fake_meter.counter("modulo_cost_ledger_limit_refused_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": {"team": "team-a"}}]
    assert metrics._limit_refused_total is counter


def test_record_duplicate_terminal_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_duplicate_terminal()
    counter = fake_meter.counter("modulo_cost_ledger_duplicate_terminal_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}]
    assert metrics._duplicate_terminal_total is counter


def test_record_probe_mismatch_runs_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_probe_mismatch_runs()
    metrics.record_probe_mismatch_runs(3)
    counter = fake_meter.counter("modulo_cost_probe_mismatch_runs_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}, {"value": 3, "attributes": None}]
    assert metrics._probe_mismatch_runs_total is counter


def test_record_probe_total_eq_mismatch_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_probe_total_eq_mismatch()
    counter = fake_meter.counter("modulo_cost_probe_total_eq_mismatch_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}]
    assert metrics._probe_total_eq_mismatch_total is counter


def test_record_probe_clamped_skip_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_probe_clamped_skip()
    metrics.record_probe_clamped_skip(2)
    counter = fake_meter.counter("modulo_cost_probe_clamped_skip_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}, {"value": 2, "attributes": None}]
    assert metrics._probe_clamped_skip_total is counter


def test_record_probe_missing_ledger_row_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_probe_missing_ledger_row()
    metrics.record_probe_missing_ledger_row(5)
    counter = fake_meter.counter("modulo_cost_probe_missing_ledger_row_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}, {"value": 5, "attributes": None}]
    assert metrics._probe_missing_ledger_row_total is counter


def test_set_probe_last_success_ts_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.set_probe_last_success_ts(1712345678.5)
    gauge = fake_meter.gauge("modulo_cost_probe_last_success_ts")
    assert gauge is not None
    assert gauge.values == [1712345678.5]
    assert metrics._probe_last_success_ts is gauge


def test_record_schema_drift_live(monkeypatch: pytest.MonkeyPatch, fake_meter: _FakeMeter) -> None:
    _stub_meter(monkeypatch, fake_meter)
    metrics.record_schema_drift()
    counter = fake_meter.counter("modulo_cost_opencode_schema_drift_total")
    assert counter is not None
    assert counter.calls == [{"value": 1, "attributes": None}]
    assert metrics._schema_drift_total is counter


# ---------------------------------------------------------------------------
# record_*/set_* ledger + probe with no meter — silent no-op
# ---------------------------------------------------------------------------


def test_ledger_record_functions_noop_without_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_meter(monkeypatch, None)
    metrics.record_fallback_legacy()
    metrics.record_ledger_clamped()
    metrics.record_ledger_refused_clamped()
    metrics.record_finalize_deferred("write_failure", "team-a")
    metrics.record_limit_refused("team-a")
    metrics.record_duplicate_terminal()
    assert metrics._fallback_legacy_total is None
    assert metrics._ledger_clamped_total is None
    assert metrics._ledger_refused_clamped_total is None
    assert metrics._finalize_deferred_total is None
    assert metrics._limit_refused_total is None
    assert metrics._duplicate_terminal_total is None


def test_probe_record_functions_noop_without_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_meter(monkeypatch, None)
    metrics.record_probe_mismatch_runs()
    metrics.record_probe_total_eq_mismatch()
    metrics.record_probe_clamped_skip()
    metrics.record_probe_missing_ledger_row()
    metrics.set_probe_last_success_ts(1.0)
    metrics.record_schema_drift()
    assert metrics._probe_mismatch_runs_total is None
    assert metrics._probe_total_eq_mismatch_total is None
    assert metrics._probe_clamped_skip_total is None
    assert metrics._probe_missing_ledger_row_total is None
    assert metrics._probe_last_success_ts is None
    assert metrics._schema_drift_total is None
