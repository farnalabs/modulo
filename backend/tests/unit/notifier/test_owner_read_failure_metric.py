"""Unit tests for the break-glass owner-read failure counter (review #657 obs 1).

``_record_owner_read_failure`` is the fail-closed observable: it increments a
lazy OTel counter every time ``_reject_break_glass_owned`` suppresses webhook
dispatch because the owner-read DB query failed. The metric must be a silent
no-op when the OTel provider is absent or broken — the notifier dispatch path
must never crash because telemetry wiring is missing.

The existing break-glass tests only *mock* ``_record_owner_read_failure``; this
file exercises the real function across its branches:
  * lazy init + attributed add with a live provider
  * no-op when the provider is None
  * no-op (with warning) when counter creation raises
  * reuse of an already-initialised counter without re-registration
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

import modulo.core.notifier as notifier_mod


class _FakeCounter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.adds: list[tuple[int, object]] = []

    def add(self, value: int, attributes: object = None) -> None:
        self.adds.append((value, attributes))


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: list[_FakeCounter] = []

    def create_counter(self, *, name: str, description: str, unit: str) -> _FakeCounter:
        counter = _FakeCounter(name)
        self.counters.append(counter)
        return counter

    def counter(self, name: str) -> _FakeCounter | None:
        return next((c for c in self.counters if c.name == name), None)


@pytest.fixture(autouse=True)
def _reset_metric_handle() -> Iterator[None]:
    saved = notifier_mod._owner_read_failures_total
    notifier_mod._owner_read_failures_total = None
    yield
    notifier_mod._owner_read_failures_total = saved


def _stub_provider(monkeypatch: pytest.MonkeyPatch, provider: object | None) -> None:
    monkeypatch.setattr("opentelemetry.metrics.get_meter_provider", lambda: provider)


class TestRecordOwnerReadFailure:
    def test_lazily_initialises_counter_and_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        provider = MagicMock()
        provider.get_meter.return_value = meter
        _stub_provider(monkeypatch, provider)

        notifier_mod._record_owner_read_failure()

        counter = meter.counter("modulo_notifier_break_glass_owner_read_failures_total")
        assert counter is not None
        assert counter.adds == [(1, None)]
        assert notifier_mod._owner_read_failures_total is counter

    def test_records_again_without_reinitialising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = _FakeMeter()
        provider = MagicMock()
        provider.get_meter.return_value = meter
        _stub_provider(monkeypatch, provider)

        notifier_mod._record_owner_read_failure()
        notifier_mod._record_owner_read_failure()
        notifier_mod._record_owner_read_failure()

        assert len(meter.counters) == 1
        assert meter.counter("modulo_notifier_break_glass_owner_read_failures_total").adds == [
            (1, None),
            (1, None),
            (1, None),
        ]

    def test_noop_when_provider_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_provider(monkeypatch, None)

        notifier_mod._record_owner_read_failure()

        assert notifier_mod._owner_read_failures_total is None

    def test_noop_when_counter_creation_fails(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = MagicMock()
        meter = MagicMock()
        meter.create_counter.side_effect = RuntimeError("meter unavailable")
        provider.get_meter.return_value = meter
        _stub_provider(monkeypatch, provider)

        notifier_mod._record_owner_read_failure()

        assert notifier_mod._owner_read_failures_total is None
        assert any("notifier.metrics_owner_read_counter_failed" in record.message for record in caplog.records)

    def test_reuses_preinitialised_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        preexisting = _FakeCounter("modulo_notifier_break_glass_owner_read_failures_total")
        notifier_mod._owner_read_failures_total = preexisting
        _stub_provider(monkeypatch, object())  # must not be consulted

        notifier_mod._record_owner_read_failure()

        assert preexisting.adds == [(1, None)]
        assert notifier_mod._owner_read_failures_total is preexisting
