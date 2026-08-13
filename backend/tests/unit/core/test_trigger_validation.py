"""QA lens tests for ``modulo.core.trigger_validation`` (FAR-158).

The ``ongoing`` trigger type keeps a pipeline topped up to a target number of
in-flight runs. Its configuration is validated identically at every write
surface (REST, MCP, PATCH) via ``validate_ongoing_config``, so the pure
validator is the single contract to lock:

* non-``ongoing`` types pass through untouched;
* ``daily_spend_limit`` is required and must be > 0 (runaway-cost guard);
* target ``max_concurrent_runs`` must be within 1..20;
* the target must not exceed the owning pipeline's ``max_concurrent_runs``
  (the effective pool is ``min(trigger, pipeline)`` at top-up time);
* ``config_json.scan_interval_seconds`` must be >= 60 (the scheduler tick is
  60s) and falls back to 60 when absent.
"""

from decimal import Decimal

import pytest
from fastapi import HTTPException

from modulo.core.trigger_validation import validate_ongoing_config


def _raise_detail(exc: HTTPException) -> str:
    assert exc.status_code == 422
    return str(exc.detail)


class TestNonOngoingPassthrough:
    def test_non_ongoing_type_skips_all_checks(self) -> None:
        """A non-ongoing trigger passes through with no checks — even when the
        ongoing rules would be violated."""
        assert (
            validate_ongoing_config(
                "cron",
                max_concurrent_runs=99,
                daily_spend_limit=None,
                config_json={"scan_interval_seconds": 1},
                pipeline_max_concurrent_runs=1,
            )
            is None
        )

    def test_empty_and_webhook_types_also_pass(self) -> None:
        for trigger_type in ("", "webhook", "polling", "agent_signal"):
            assert (
                validate_ongoing_config(
                    trigger_type,
                    max_concurrent_runs=0,
                    daily_spend_limit=None,
                    config_json=None,
                    pipeline_max_concurrent_runs=0,
                )
                is None
            )


class TestDailySpendLimit:
    @pytest.mark.parametrize("spend", [None, 0, -1, Decimal("0.00"), Decimal("-12.50"), -0.001])
    def test_missing_or_non_positive_limit_rejected(self, spend: object) -> None:
        with pytest.raises(HTTPException) as excinfo:
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=spend,  # type: ignore[arg-type]
                config_json=None,
                pipeline_max_concurrent_runs=10,
            )
        assert _raise_detail(excinfo.value) == "ongoing triggers require daily_spend_limit (must be greater than 0)"

    @pytest.mark.parametrize("spend", [Decimal("0.01"), 1, 25.50, Decimal("123.45")])
    def test_positive_limit_accepted(self, spend: object) -> None:
        assert (
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=spend,  # type: ignore[arg-type]
                config_json=None,
                pipeline_max_concurrent_runs=10,
            )
            is None
        )


class TestMaxConcurrentRuns:
    @pytest.mark.parametrize("target", [-5, 0, 21, 100])
    def test_target_outside_1_20_rejected(self, target: int) -> None:
        with pytest.raises(HTTPException) as excinfo:
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=target,
                daily_spend_limit=Decimal("25.00"),
                config_json=None,
                pipeline_max_concurrent_runs=target + 50,
            )
        assert _raise_detail(excinfo.value) == "ongoing trigger target max_concurrent_runs must be between 1 and 20"

    @pytest.mark.parametrize("target", [1, 10, 20])
    def test_target_boundaries_accepted(self, target: int) -> None:
        assert (
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=target,
                daily_spend_limit=Decimal("25.00"),
                config_json=None,
                pipeline_max_concurrent_runs=target,
            )
            is None
        )

    def test_target_above_pipeline_cap_rejected(self) -> None:
        """The effective pool is min(trigger, pipeline), so a target above the
        pipeline cap would be silently useless and is rejected up front."""
        with pytest.raises(HTTPException) as excinfo:
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=Decimal("25.00"),
                config_json=None,
                pipeline_max_concurrent_runs=4,
            )
        assert (
            _raise_detail(excinfo.value) == "ongoing trigger target max_concurrent_runs cannot exceed the pipeline's "
            "max_concurrent_runs (4)"
        )

    @pytest.mark.parametrize("pipeline_cap", [5, 10, 20])
    def test_target_at_or_below_pipeline_cap_accepted(self, pipeline_cap: int) -> None:
        assert (
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=Decimal("25.00"),
                config_json=None,
                pipeline_max_concurrent_runs=pipeline_cap,
            )
            is None
        )


class TestScanInterval:
    @pytest.mark.parametrize("scan", [1, 59])
    def test_scan_below_tick_rejected(self, scan: int) -> None:
        """The scheduler tick is 60s; a lower scan would be ignored."""
        with pytest.raises(HTTPException) as excinfo:
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=Decimal("25.00"),
                config_json={"scan_interval_seconds": scan},
                pipeline_max_concurrent_runs=10,
            )
        assert _raise_detail(excinfo.value) == "ongoing trigger scan_interval_seconds must be at least 60"

    @pytest.mark.parametrize("scan", [60, 90, 120, 3600])
    def test_scan_at_or_above_tick_accepted(self, scan: int) -> None:
        assert (
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=Decimal("25.00"),
                config_json={"scan_interval_seconds": scan},
                pipeline_max_concurrent_runs=10,
            )
            is None
        )

    def test_scan_defaults_to_60_when_absent(self) -> None:
        """config_json without scan_interval_seconds (or None) defaults to 60."""
        for config in (None, {}, {"unrelated": "key"}):
            assert (
                validate_ongoing_config(
                    "ongoing",
                    max_concurrent_runs=5,
                    daily_spend_limit=Decimal("25.00"),
                    config_json=config,
                    pipeline_max_concurrent_runs=10,
                )
                is None
            )

    def test_scan_defaults_to_60_when_zero_or_falsy(self) -> None:
        """A falsy scan value (0, "") is treated as absent and defaults to 60."""
        for scan in (0, ""):
            assert (
                validate_ongoing_config(
                    "ongoing",
                    max_concurrent_runs=5,
                    daily_spend_limit=Decimal("25.00"),
                    config_json={"scan_interval_seconds": scan},
                    pipeline_max_concurrent_runs=10,
                )
                is None
            )

    def test_scan_numeric_string_is_coerced(self) -> None:
        assert (
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=Decimal("25.00"),
                config_json={"scan_interval_seconds": "90"},
                pipeline_max_concurrent_runs=10,
            )
            is None
        )

    def test_scan_non_integer_string_raises_value_error(self) -> None:
        """A non-numeric scan string currently surfaces as ValueError (the int()
        coercion), not a 422 — locks current behaviour for the pure validator."""
        with pytest.raises(ValueError):
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=Decimal("25.00"),
                config_json={"scan_interval_seconds": "every-minute"},
                pipeline_max_concurrent_runs=10,
            )


class TestCombinations:
    def test_fully_valid_config_accepted(self) -> None:
        assert (
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=5,
                daily_spend_limit=Decimal("25.00"),
                config_json={"scan_interval_seconds": 120},
                pipeline_max_concurrent_runs=5,
            )
            is None
        )

    def test_first_violation_wins(self) -> None:
        """Rules are evaluated in order: spend limit is checked before target
        range, so a config violating several rules reports the first one."""
        with pytest.raises(HTTPException) as excinfo:
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=99,
                daily_spend_limit=None,
                config_json={"scan_interval_seconds": 1},
                pipeline_max_concurrent_runs=1,
            )
        assert "daily_spend_limit" in _raise_detail(excinfo.value)

    def test_target_range_reported_before_pipeline_cap(self) -> None:
        """A target out of 1..20 is reported before the pipeline-cap check."""
        with pytest.raises(HTTPException) as excinfo:
            validate_ongoing_config(
                "ongoing",
                max_concurrent_runs=0,
                daily_spend_limit=Decimal("25.00"),
                config_json=None,
                pipeline_max_concurrent_runs=0,
            )
        assert "must be between 1 and 20" in _raise_detail(excinfo.value)
