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
