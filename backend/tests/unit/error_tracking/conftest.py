"""Shared fixtures for error-tracking unit tests (FAR-1676).

Carries only what genuinely needs fixture semantics (setup + teardown around
mutated module state). Plain factories live in ``_factories.py`` next to this
file.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

import modulo.core.error_tracking.metrics as metrics_mod

_METRIC_HANDLES = (
    "_errors_total",
    "_error_groups_active",
    "_error_alerts_total",
    "_runs_running_gauge",
    "_runs_oldest_running_gauge",
    "_runs_stall_reason_total",
    "_runs_claim_count_histogram",
    "_alerts_suppressed_total",
    "_alert_delivery_failed_total",
    "_connector_unknown_total",
)


@pytest.fixture
def fake_otel() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Inject a fake ``opentelemetry`` / ``opentelemetry.metrics`` into ``sys.modules``
    for the duration of the test and clean up afterwards so the stubs never shadow the
    real OTel package for other test modules running in the same process.

    Yields ``(meter, fake_metrics)`` where ``fake_metrics.get_meter_provider`` returns
    ``None`` by default; tests may override it to point at a fake provider. The meter
    stubs counter + gauge + histogram creation, so consumers that only need one of
    those create-* methods are covered by the superset.
    """
    fake_metrics = types.ModuleType("opentelemetry.metrics")
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock()
    meter.create_gauge.return_value = MagicMock()
    meter.create_histogram.return_value = MagicMock()
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


@pytest.fixture
def reset_metric_handles() -> Iterator[None]:
    """Null the module-level metric handles around a test so lazy registration
    starts from a clean slate and no test leaks a registered handle into the next.
    """
    saved = {name: getattr(metrics_mod, name) for name in _METRIC_HANDLES}
    for name in _METRIC_HANDLES:
        setattr(metrics_mod, name, None)
    yield
    for name, value in saved.items():
        setattr(metrics_mod, name, value)
