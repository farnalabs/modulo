"""Unit tests for cron expression validation and next-fire computation."""

import datetime
from zoneinfo import ZoneInfoNotFoundError

import pytest

from modulo.core.cron_helpers import compute_next_fire, compute_next_send, validate_cron_expression


class TestValidateCronExpression:
    def test_valid_standard_expression(self):
        assert validate_cron_expression("*/5 * * * *") is None

    def test_valid_hourly(self):
        assert validate_cron_expression("0 * * * *") is None

    def test_valid_daily(self):
        assert validate_cron_expression("0 9 * * *") is None

    def test_valid_complex(self):
        assert validate_cron_expression("30 4,16 * * 1-5") is None

    def test_valid_every_minute(self):
        assert validate_cron_expression("* * * * *") is None

    def test_invalid_expression_returns_error(self):
        # The exact message is croniter-version-sensitive, so only assert a
        # column-count error is returned.
        err = validate_cron_expression("not-a-cron")
        assert err is not None
        assert "columns" in err

    def test_empty_expression_returns_error(self):
        err = validate_cron_expression("")
        assert "columns" in err

    def test_invalid_range_field(self):
        # Hour field "25" is out of range 0-23. The exact wording is
        # croniter-version-sensitive, so only assert an error is returned.
        err = validate_cron_expression("0 25 * * *")
        assert err is not None

    def test_valid_with_timezone(self):
        assert validate_cron_expression("0 9 * * *", "America/New_York") is None

    def test_invalid_timezone_returns_error(self):
        err = validate_cron_expression("0 9 * * *", "Mars/Olympus")
        assert err is not None
        assert err.startswith("Invalid timezone:")


class TestComputeNextFire:
    def test_next_fire_every_minute(self):
        now = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        next_fire = compute_next_fire("* * * * *", after=now)
        assert next_fire > now
        # Next minute
        assert next_fire.minute == 1
        assert next_fire.hour == 12
        assert next_fire.day == 1

    def test_next_fire_hourly(self):
        now = datetime.datetime(2026, 1, 1, 12, 30, 0, tzinfo=datetime.UTC)
        next_fire = compute_next_fire("0 * * * *", after=now)
        assert next_fire > now
        assert next_fire.minute == 0
        assert next_fire.hour == 13

    def test_next_fire_daily_at_9am(self):
        now = datetime.datetime(2026, 1, 1, 8, 0, 0, tzinfo=datetime.UTC)
        next_fire = compute_next_fire("0 9 * * *", after=now)
        assert next_fire > now
        assert next_fire.hour == 9
        assert next_fire.minute == 0

    def test_next_fire_daily_past_today(self):
        now = datetime.datetime(2026, 1, 1, 10, 0, 0, tzinfo=datetime.UTC)
        next_fire = compute_next_fire("0 9 * * *", after=now)
        # Should fire tomorrow at 9am
        assert next_fire.day == 2
        assert next_fire.hour == 9

    def test_next_fire_every_5_minutes(self):
        now = datetime.datetime(2026, 1, 1, 12, 3, 0, tzinfo=datetime.UTC)
        next_fire = compute_next_fire("*/5 * * * *", after=now)
        assert next_fire > now
        assert next_fire.minute in (5, 10, 15)

    def test_next_fire_without_after_defaults_to_now(self):
        result = compute_next_fire("* * * * *")
        assert result > datetime.datetime.now(datetime.UTC)

    def test_naive_after_interpreted_as_utc(self):
        result = compute_next_fire("0 9 * * *", after=datetime.datetime(2026, 1, 1, 8, 0, 0))
        assert result == datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)

    def test_invalid_expression_raises(self):
        with pytest.raises(ValueError, match="columns has to be specified"):
            compute_next_fire("not-a-cron")

    def test_invalid_timezone_raises(self):
        with pytest.raises(ZoneInfoNotFoundError):
            compute_next_fire(
                "0 9 * * *", after=datetime.datetime(2026, 1, 1, 8, tzinfo=datetime.UTC), timezone="Mars/Olympus"
            )

    def test_new_york_winter_and_summer_offsets(self):
        winter = compute_next_fire(
            "0 9 * * *",
            after=datetime.datetime(2026, 1, 15, 12, tzinfo=datetime.UTC),
            timezone="America/New_York",
        )
        summer = compute_next_fire(
            "0 9 * * *",
            after=datetime.datetime(2026, 7, 15, 12, tzinfo=datetime.UTC),
            timezone="America/New_York",
        )

        assert winter == datetime.datetime(2026, 1, 15, 14, tzinfo=datetime.UTC)
        assert summer == datetime.datetime(2026, 7, 15, 13, tzinfo=datetime.UTC)

    def test_nonexistent_dst_time_advances_to_first_valid_instant(self):
        result = compute_next_fire(
            "30 2 * * *",
            after=datetime.datetime(2026, 3, 7, 12, tzinfo=datetime.UTC),
            timezone="America/New_York",
        )

        assert result == datetime.datetime(2026, 3, 8, 7, tzinfo=datetime.UTC)

    def test_ambiguous_dst_time_uses_first_occurrence(self):
        result = compute_next_fire(
            "30 1 * * *",
            after=datetime.datetime(2026, 10, 31, 12, tzinfo=datetime.UTC),
            timezone="America/New_York",
        )

        assert result == datetime.datetime(2026, 11, 1, 5, 30, tzinfo=datetime.UTC)

    def test_default_timezone_is_utc(self):
        result = compute_next_fire(
            "0 9 * * *",
            after=datetime.datetime(2026, 1, 1, 8, tzinfo=datetime.UTC),
        )

        assert result == datetime.datetime(2026, 1, 1, 9, tzinfo=datetime.UTC)

    def test_next_fire_strictly_after_exact_match(self):
        """An ``after`` exactly on a fire time must return the NEXT occurrence."""
        now = datetime.datetime(2026, 1, 1, 9, 0, 0, tzinfo=datetime.UTC)
        next_fire = compute_next_fire("0 9 * * *", after=now)
        assert next_fire == datetime.datetime(2026, 1, 2, 9, 0, 0, tzinfo=datetime.UTC)

    def test_next_fire_monthly_rollover(self):
        """First-of-month expressions roll into the following month."""
        now = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        next_fire = compute_next_fire("0 0 1 * *", after=now)
        assert next_fire == datetime.datetime(2026, 2, 1, 0, 0, 0, tzinfo=datetime.UTC)


class TestComputeNextSend:
    def test_next_send_returns_future_datetime(self):
        now = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        next_send = compute_next_send("* * * * *", after=now)
        assert next_send > now
        assert next_send.minute == 1
        assert next_send.hour == 12
        assert next_send.day == 1

    def test_next_send_daily_at_9am(self):
        now = datetime.datetime(2026, 1, 1, 8, 0, 0, tzinfo=datetime.UTC)
        next_send = compute_next_send("0 9 * * *", after=now)
        assert next_send.hour == 9
        assert next_send.minute == 0
        assert next_send.day == 1

    def test_next_send_daily_past_today_rolls_to_tomorrow(self):
        now = datetime.datetime(2026, 1, 1, 10, 0, 0, tzinfo=datetime.UTC)
        next_send = compute_next_send("0 9 * * *", after=now)
        assert next_send.day == 2
        assert next_send.hour == 9

    def test_next_send_without_after_defaults_to_now(self):
        result = compute_next_send("* * * * *")
        assert result > datetime.datetime.now(datetime.UTC)

    def test_next_send_weekly_on_monday(self):
        now = datetime.datetime(2026, 1, 1, 8, 0, 0, tzinfo=datetime.UTC)
        next_send = compute_next_send("0 9 * * 1", after=now)
        # 2026-01-01 is a Thursday; next Monday is 2026-01-05.
        assert next_send == datetime.datetime(2026, 1, 5, 9, 0, 0, tzinfo=datetime.UTC)

    def test_next_send_strictly_after_exact_match(self):
        now = datetime.datetime(2026, 1, 1, 9, 0, 0, tzinfo=datetime.UTC)
        next_send = compute_next_send("0 9 * * *", after=now)
        assert next_send == datetime.datetime(2026, 1, 2, 9, 0, 0, tzinfo=datetime.UTC)

    def test_next_send_invalid_expression_raises(self):
        with pytest.raises(ValueError, match="columns has to be specified"):
            compute_next_send("not-a-cron")
