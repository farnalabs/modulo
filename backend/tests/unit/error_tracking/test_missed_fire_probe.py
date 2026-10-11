"""Unit tests for the missed-fire probe's pure per-row gates."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from modulo.core.error_tracking import (
    SAQ_MISSED_FIRE_GRACE_SECONDS,
    _row_missed_period,
    _trigger_period_seconds,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _row(
    *,
    trigger_type: str = "polling",
    config_json: dict[str, Any] | None = None,
    last_fired_at: datetime | None = None,
    created_at: datetime | None = None,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        trigger_type=trigger_type,
        cron_expression=cron_expression,
        cron_timezone=cron_timezone,
        config_json=config_json,
        last_fired_at=last_fired_at,
        created_at=created_at,
    )


def test_row_missed_period_skips_sub_hour_cadence() -> None:
    row = _row(
        trigger_type="polling",
        config_json={"poll_interval_seconds": 60},
        last_fired_at=None,
        created_at=_NOW - timedelta(hours=5),
    )
    assert _row_missed_period(row, _NOW, SAQ_MISSED_FIRE_GRACE_SECONDS) is None


def test_row_missed_period_skips_when_fired_recently() -> None:
    row = _row(
        trigger_type="polling",
        config_json={"poll_interval_seconds": 7200},
        last_fired_at=_NOW - timedelta(seconds=10),
        created_at=_NOW - timedelta(days=2),
    )
    assert _row_missed_period(row, _NOW, SAQ_MISSED_FIRE_GRACE_SECONDS) is None


def test_row_missed_period_skips_brand_new_trigger() -> None:
    row = _row(
        trigger_type="polling",
        config_json={"poll_interval_seconds": 7200},
        last_fired_at=None,
        created_at=_NOW - timedelta(seconds=10),
    )
    assert _row_missed_period(row, _NOW, SAQ_MISSED_FIRE_GRACE_SECONDS) is None


def test_row_missed_period_returns_period_when_long_overdue_never_fired() -> None:
    row = _row(
        trigger_type="polling",
        config_json={"poll_interval_seconds": 7200},
        last_fired_at=None,
        created_at=_NOW - timedelta(days=2),
    )
    assert _row_missed_period(row, _NOW, SAQ_MISSED_FIRE_GRACE_SECONDS) == 7200


def test_row_missed_period_returns_period_when_fired_long_ago() -> None:
    row = _row(
        trigger_type="polling",
        config_json={"poll_interval_seconds": 7200},
        last_fired_at=_NOW - timedelta(hours=5),
        created_at=_NOW - timedelta(days=2),
    )
    assert _row_missed_period(row, _NOW, SAQ_MISSED_FIRE_GRACE_SECONDS) == 7200


@pytest.mark.parametrize(
    ("trigger_type", "config_json", "cron_expression"),
    [
        ("polling", {}, None),
        ("polling", {"poll_interval_seconds": 0}, None),
        ("cron", None, None),
    ],
    ids=["polling_missing_interval", "polling_zero_interval", "cron_missing_expression"],
)
def test_trigger_period_seconds_uncomputable_returns_none(
    trigger_type: str, config_json: dict[str, Any] | None, cron_expression: str | None
) -> None:
    assert _trigger_period_seconds(trigger_type, cron_expression, None, config_json, _NOW) is None


def test_trigger_period_seconds_cron_hourly_is_3600() -> None:
    assert _trigger_period_seconds("cron", "0 * * * *", "UTC", None, _NOW) == 3600


def test_trigger_period_seconds_polling_floor_is_at_least_one() -> None:
    # ``max(int(interval), 1)`` floors any non-zero sub-second interval to 1.
    assert _trigger_period_seconds("polling", None, None, {"poll_interval_seconds": 0.5}, _NOW) == 1
