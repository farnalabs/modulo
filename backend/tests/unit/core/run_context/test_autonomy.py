"""Unit tests for autonomy_level resolution and helpers."""

import logging
from typing import Any

import pytest

from modulo.core.run_context.autonomy import (
    AUTONOMY_LEVEL_VALUES,
    PIPELINE_MAX_AUTONOMY_KEY,
    AutonomyLevel,
    AutonomyResolution,
    autonomy_change_payload,
    autonomy_level_rank,
    effective_autonomy_level,
    resolve_autonomy,
    should_notify_on_complete,
    should_skip_hitl_gate,
    validate_autonomy_ceiling,
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
        with pytest.raises(ValueError, match="Invalid autonomy level"):
            AutonomyLevel("bogus")
        with pytest.raises(ValueError, match="Invalid autonomy level"):
            AutonomyLevel("notify-complete")

    def test_missing_non_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid autonomy level"):
            AutonomyLevel(42)


class TestEffectiveAutonomyLevel:
    def test_run_context_recommendation_cannot_raise_without_ceiling(self) -> None:
        """FAR-1163 S0 — prove-the-fix: with no ceiling pinned, the effective
        ceiling is the pipeline default, so a context-setter writing
        ``fully_autonomous`` on a ``manual_approval`` pipeline resolves
        ``manual_approval`` and is reported as clamped (the old behaviour —
        unconditional escalation — is the hole this closes)."""
        result = effective_autonomy_level(
            pipeline_default="manual_approval",
            run_context={"autonomy_recommendation": "fully_autonomous"},
        )
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_run_context_recommendation_lowers_freely(self) -> None:
        """Lowering is always allowed and never reported as a clamp."""
        resolution = resolve_autonomy(
            pipeline_default="fully_autonomous",
            run_context={"autonomy_recommendation": "manual_approval"},
        )
        assert resolution.effective == AutonomyLevel.MANUAL_APPROVAL
        assert resolution.requested == AutonomyLevel.MANUAL_APPROVAL
        assert resolution.clamped is False

    def test_ceiling_allows_raise_up_to_ceiling(self) -> None:
        """With ceiling ``fully_autonomous`` and default ``manual_approval``,
        a ``notify_on_complete`` recommendation raises (not clamped); a
        recommendation above the ceiling clamps to the ceiling."""
        raised = resolve_autonomy(
            pipeline_default="manual_approval",
            run_context={
                "autonomy_recommendation": "notify_on_complete",
                PIPELINE_MAX_AUTONOMY_KEY: "fully_autonomous",
            },
        )
        assert raised.effective == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert raised.clamped is False
        assert raised.ceiling == AutonomyLevel.FULLY_AUTONOMOUS

        clamped_to_ceiling = resolve_autonomy(
            pipeline_default="manual_approval",
            run_context={
                "autonomy_recommendation": "fully_autonomous",
                PIPELINE_MAX_AUTONOMY_KEY: "notify_on_complete",
            },
        )
        assert clamped_to_ceiling.effective == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert clamped_to_ceiling.requested == AutonomyLevel.FULLY_AUTONOMOUS
        assert clamped_to_ceiling.ceiling == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert clamped_to_ceiling.clamped is True

    def test_clamp_resolution_reports_requested_and_ceiling(self) -> None:
        resolution = resolve_autonomy(
            pipeline_default="manual_approval",
            run_context={"autonomy_recommendation": "fully_autonomous"},
        )
        assert resolution == AutonomyResolution(
            effective=AutonomyLevel.MANUAL_APPROVAL,
            requested=AutonomyLevel.FULLY_AUTONOMOUS,
            ceiling=AutonomyLevel.MANUAL_APPROVAL,
            clamped=True,
        )

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
            # No ceiling pinned → effective ceiling is the default: raises
            # from a context-setter are clamped (FAR-1163 S0).
            (None, {"autonomy_recommendation": "fully_autonomous"}, AutonomyLevel.MANUAL_APPROVAL),
            ("fully_autonomous", {}, AutonomyLevel.FULLY_AUTONOMOUS),
            (
                None,
                {"autonomy_recommendation": "notify_on_complete"},
                AutonomyLevel.MANUAL_APPROVAL,
            ),
            (
                "manual_approval",
                {"autonomy_recommendation": "notify_on_complete"},
                AutonomyLevel.MANUAL_APPROVAL,
            ),
            # Explicit ceiling re-opens raising up to the ceiling.
            (
                "manual_approval",
                {
                    "autonomy_recommendation": "notify_on_complete",
                    PIPELINE_MAX_AUTONOMY_KEY: "fully_autonomous",
                },
                AutonomyLevel.NOTIFY_ON_COMPLETE,
            ),
            # A recommendation above the ceiling clamps to the ceiling.
            (
                "manual_approval",
                {
                    "autonomy_recommendation": "fully_autonomous",
                    PIPELINE_MAX_AUTONOMY_KEY: "notify_on_complete",
                },
                AutonomyLevel.NOTIFY_ON_COMPLETE,
            ),
            # Lowering below the default is always allowed.
            (
                "fully_autonomous",
                {"autonomy_recommendation": "manual_approval"},
                AutonomyLevel.MANUAL_APPROVAL,
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

    def test_non_dict_run_context_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            result = effective_autonomy_level(
                pipeline_default="notify_on_complete",
                run_context="unexpected_string",
            )
        assert result == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert len(caplog.records) == 1
        assert "not a dict" in caplog.records[0].message


class TestShouldSkipHitlGate:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (AutonomyLevel.FULLY_AUTONOMOUS, True),
            (AutonomyLevel.MANUAL_APPROVAL, False),
            (AutonomyLevel.NOTIFY_ON_COMPLETE, False),
        ],
    )
    def test_skip_hitl_gate(self, level: AutonomyLevel, expected: bool) -> None:
        assert should_skip_hitl_gate(level) is expected


class TestShouldNotifyOnComplete:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (AutonomyLevel.NOTIFY_ON_COMPLETE, True),
            (AutonomyLevel.FULLY_AUTONOMOUS, False),
            (AutonomyLevel.MANUAL_APPROVAL, False),
        ],
    )
    def test_notify(self, level: AutonomyLevel, expected: bool) -> None:
        assert should_notify_on_complete(level) is expected


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


class TestAutonomyRank:
    def test_levels_ordered_manual_to_fully_autonomous(self) -> None:
        assert autonomy_level_rank(AutonomyLevel.MANUAL_APPROVAL) < autonomy_level_rank(
            AutonomyLevel.NOTIFY_ON_COMPLETE
        )
        assert autonomy_level_rank(AutonomyLevel.NOTIFY_ON_COMPLETE) < autonomy_level_rank(
            AutonomyLevel.FULLY_AUTONOMOUS
        )


class TestValidateAutonomyCeiling:
    def test_none_ceiling_never_violates(self) -> None:
        assert validate_autonomy_ceiling("fully_autonomous", None) is None

    def test_ceiling_equal_to_default_is_valid(self) -> None:
        assert validate_autonomy_ceiling("manual_approval", "manual_approval") is None

    def test_ceiling_above_default_is_valid(self) -> None:
        assert validate_autonomy_ceiling("manual_approval", "fully_autonomous") is None

    def test_ceiling_below_default_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >="):
            validate_autonomy_ceiling("fully_autonomous", "notify_on_complete")

    def test_invalid_ceiling_raises_when_strict(self) -> None:
        with pytest.raises(ValueError, match="Invalid max_autonomy_level"):
            validate_autonomy_ceiling("manual_approval", "banana")

    def test_lenient_invalid_ceiling_does_not_raise(self) -> None:
        # A stored/unparseable ceiling does not constrain resolution either
        # (resolution falls back to the default), so lenient mode skips it.
        assert validate_autonomy_ceiling("fully_autonomous", "banana", lenient=True) is None
        assert validate_autonomy_ceiling("fully_autonomous", object(), lenient=True) is None

    def test_unparseable_default_ranks_as_manual_approval(self) -> None:
        # Matches resolution's fallback: an unknown default cannot fail the
        # check by itself.
        assert validate_autonomy_ceiling("autonomous", "manual_approval") is None


class TestMaxAutonomyReservedKey:
    def test_pipeline_max_autonomy_is_reserved(self) -> None:
        """FAR-1163: a context-setter may never overwrite the pinned ceiling
        (it would lift the cap on its own recommendation)."""
        from modulo.core.pipeline_engine.decorator import _RESERVED_RUN_CONTEXT_KEYS

        assert PIPELINE_MAX_AUTONOMY_KEY in _RESERVED_RUN_CONTEXT_KEYS
        assert PIPELINE_MAX_AUTONOMY_KEY == "_pipeline_max_autonomy"
