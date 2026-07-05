"""Unit tests for autonomy_level resolution and helpers."""

import logging
from typing import Any

import pytest

from modulo.core.run_context.autonomy import (
    AUTONOMY_LEVEL_VALUES,
    AutonomyLevel,
    autonomy_change_payload,
    effective_autonomy_level,
    should_notify_on_complete,
    should_skip_hitl_gate,
)


class TestAutonomyLevel:
    def test_default_is_manual_approval(self) -> None:
        assert AutonomyLevel.default() == AutonomyLevel.MANUAL_APPROVAL

    def test_values_match_enum_members(self) -> None:
        assert AUTONOMY_LEVEL_VALUES == [
            "manual_approval",
            "notify_on_complete",
            "fully_autonomous",
        ]

    def test_from_valid_string(self) -> None:
        assert AutonomyLevel("manual_approval") == AutonomyLevel.MANUAL_APPROVAL
        assert AutonomyLevel("notify_on_complete") == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert AutonomyLevel("fully_autonomous") == AutonomyLevel.FULLY_AUTONOMOUS

    def test_missing_case_insensitive_match(self) -> None:
        assert AutonomyLevel("MANUAL_APPROVAL") == AutonomyLevel.MANUAL_APPROVAL
        assert AutonomyLevel("FULLY_AUTONOMOUS") == AutonomyLevel.FULLY_AUTONOMOUS
        assert AutonomyLevel("NOTIFY_ON_COMPLETE") == AutonomyLevel.NOTIFY_ON_COMPLETE

    def test_missing_unmatched_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            AutonomyLevel("bogus")
        with pytest.raises(ValueError):
            AutonomyLevel("notify-complete")

    def test_missing_non_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            AutonomyLevel(42)


class TestEffectiveAutonomyLevel:
    def test_run_context_recommendation_takes_priority(self) -> None:
        result = effective_autonomy_level(
            pipeline_default="manual_approval",
            run_context={"autonomy_recommendation": "fully_autonomous"},
        )
        assert result == AutonomyLevel.FULLY_AUTONOMOUS

    def test_pipeline_default_fallback(self) -> None:
        result = effective_autonomy_level(
            pipeline_default="notify_on_complete",
            run_context=None,
        )
        assert result == AutonomyLevel.NOTIFY_ON_COMPLETE

    def test_pipeline_default_when_context_has_no_recommendation(self) -> None:
        result = effective_autonomy_level(
            pipeline_default="fully_autonomous",
            run_context={"some_key": "value"},
        )
        assert result == AutonomyLevel.FULLY_AUTONOMOUS

    def test_safe_fallback_when_nothing_configured(self) -> None:
        result = effective_autonomy_level(pipeline_default=None, run_context=None)
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_safe_fallback_when_both_empty(self) -> None:
        result = effective_autonomy_level(pipeline_default=None, run_context={})
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_run_context_override_is_none_still_uses_pipeline_default(self) -> None:
        result = effective_autonomy_level(
            pipeline_default="notify_on_complete",
            run_context={"autonomy_recommendation": None},
        )
        assert result == AutonomyLevel.NOTIFY_ON_COMPLETE

    def test_pipeline_default_invalid_uses_safe_fallback(self) -> None:
        result = effective_autonomy_level(
            pipeline_default="invalid_value",
            run_context=None,
        )
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_run_context_recommendation_invalid_falls_back_to_pipeline_default(self) -> None:
        result = effective_autonomy_level(
            pipeline_default="fully_autonomous",
            run_context={"autonomy_recommendation": "bogus_value"},
        )
        assert result == AutonomyLevel.FULLY_AUTONOMOUS

    @pytest.mark.parametrize(
        ("pipeline_default", "run_context", "expected"),
        [
            (None, {"autonomy_recommendation": "fully_autonomous"}, AutonomyLevel.FULLY_AUTONOMOUS),
            ("fully_autonomous", {}, AutonomyLevel.FULLY_AUTONOMOUS),
            (
                None,
                {"autonomy_recommendation": "notify_on_complete"},
                AutonomyLevel.NOTIFY_ON_COMPLETE,
            ),
            (
                "manual_approval",
                {"autonomy_recommendation": "notify_on_complete"},
                AutonomyLevel.NOTIFY_ON_COMPLETE,
            ),
        ],
    )
    def test_priority_chain(
        self,
        pipeline_default: str | None,
        run_context: dict[str, Any] | None,
        expected: AutonomyLevel,
    ) -> None:
        result = effective_autonomy_level(pipeline_default, run_context)
        assert result == expected

    def test_invalid_recommendation_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            result = effective_autonomy_level(
                pipeline_default="notify_on_complete",
                run_context={"autonomy_recommendation": "bogus"},
            )
        assert result == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert len(caplog.records) == 1
        assert "bogus" in caplog.records[0].message

    def test_invalid_pipeline_default_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            result = effective_autonomy_level(
                pipeline_default="invalid_level",
                run_context=None,
            )
        assert result == AutonomyLevel.MANUAL_APPROVAL
        assert len(caplog.records) == 1
        assert "invalid_level" in caplog.records[0].message

    def test_both_invalid_only_logs_recommendation(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            result = effective_autonomy_level(
                pipeline_default="bad_default",
                run_context={"autonomy_recommendation": "bad_rec"},
            )
        assert result == AutonomyLevel.MANUAL_APPROVAL
        assert len(caplog.records) == 2


class TestShouldSkipHitlGate:
    def test_fully_autonomous_skips(self) -> None:
        assert should_skip_hitl_gate(AutonomyLevel.FULLY_AUTONOMOUS) is True

    def test_manual_approval_does_not_skip(self) -> None:
        assert should_skip_hitl_gate(AutonomyLevel.MANUAL_APPROVAL) is False

    def test_notify_on_complete_does_not_skip(self) -> None:
        assert should_skip_hitl_gate(AutonomyLevel.NOTIFY_ON_COMPLETE) is False


class TestShouldNotifyOnComplete:
    def test_notify_on_complete_returns_true(self) -> None:
        assert should_notify_on_complete(AutonomyLevel.NOTIFY_ON_COMPLETE) is True

    def test_fully_autonomous_returns_false(self) -> None:
        assert should_notify_on_complete(AutonomyLevel.FULLY_AUTONOMOUS) is False

    def test_manual_approval_returns_false(self) -> None:
        assert should_notify_on_complete(AutonomyLevel.MANUAL_APPROVAL) is False


class TestAutonomyChangePayload:
    def test_both_set(self) -> None:
        payload = autonomy_change_payload("manual_approval", "fully_autonomous")
        assert payload == {
            "previous_level": "manual_approval",
            "new_level": "fully_autonomous",
        }

    def test_previous_none(self) -> None:
        payload = autonomy_change_payload(None, "notify_on_complete")
        assert payload["previous_level"] is None
        assert payload["new_level"] == "notify_on_complete"

    def test_current_none(self) -> None:
        payload = autonomy_change_payload("fully_autonomous", None)
        assert payload["previous_level"] == "fully_autonomous"
        assert payload["new_level"] is None
