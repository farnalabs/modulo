"""Unit tests for the analytics period-over-period delta computation (ADR 020).

The delta (value + period arrow) is period-scoped and same-source/same-window:
``compute_delta(prev, curr)`` returns the 1dp percent change, ``None`` when
there is no meaningful baseline (prev zero/absent, both zero).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modulo.core.analytics import compute_delta


@pytest.mark.parametrize(
    ("prev", "curr", "expected"),
    [
        # prev absent/zero → no baseline → null
        (None, 100.0, None),
        (0, 100.0, None),
        (0.0, 0.0, None),
        (0, 0, None),
        # negative deltas returned as-is (a drop is negative, never clamped)
        (100.0, 50.0, -50.0),
        (100.0, 0.0, -100.0),
        # positive growth
        (100.0, 133.0, 33.0),
        (200.0, 233.0, 16.5),
        # 1dp rounding
        (3.0, 4.0, 33.3),
        (3.0, 4.01, 33.7),
        (1.0, 2.0, 100.0),
        # unchanged
        (100.0, 100.0, 0.0),
        # curr absent → treated as 0
        (100.0, None, -100.0),
    ],
)
def test_compute_delta_table(prev: float | None, curr: float | None, expected: float | None) -> None:
    assert compute_delta(prev, curr) == expected


def test_compute_delta_injected_now_unused_signature() -> None:
    # The delta computation is pure; callers anchor "now" for window selection,
    # and compute_delta itself only needs the two window values.
    now = datetime.now(UTC)
    assert compute_delta(100.0, 110.0) == 10.0
    assert now is not None


def test_compute_delta_never_returns_nan_or_inf() -> None:
    assert compute_delta(0, float("inf")) is None
    assert compute_delta(float("inf"), 100.0) is None
