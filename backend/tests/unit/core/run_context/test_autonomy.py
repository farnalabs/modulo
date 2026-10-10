"""Unit tests for autonomy_level resolution and helpers."""

import logging
from typing import Any

import pytest

from modulo.core.run_context.autonomy import (
    AUTONOMY_GATING_FLAG,
    AUTONOMY_LEVEL_VALUES,
    PIPELINE_EARNED_AT_START_KEY,
    PIPELINE_MAX_AUTONOMY_KEY,
    AutonomyLevel,
    AutonomyResolution,
    autonomy_change_payload,
    autonomy_level_rank,
    effective_autonomy_level,
    resolve_autonomy,
    should_notify_on_complete,
    should_skip_hitl_review,
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


class TestShouldSkipHitlReview:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (AutonomyLevel.FULLY_AUTONOMOUS, True),
            (AutonomyLevel.MANUAL_APPROVAL, False),
            (AutonomyLevel.NOTIFY_ON_COMPLETE, False),
        ],
    )
    def test_skip_hitl_review(self, level: AutonomyLevel, expected: bool) -> None:
        assert should_skip_hitl_review(level) is expected


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

    def test_mixed_case_ceiling_is_rejected_not_stored(self) -> None:
        """FAR-1163: case-variants must never reach the case-sensitive DB CHECK.

        ``AutonomyLevel._missing_`` parses ``"FULLY_AUTONOMOUS"`` fine, so
        without this the value would be stored raw and the INSERT would die
        with an IntegrityError (409) instead of a clean rejection. This
        function cannot return a normalised value (raise-or-return-None
        contract), so it rejects; the REST field validators normalise before
        reaching here (see the endpoint tests).
        """
        with pytest.raises(ValueError, match="canonical"):
            validate_autonomy_ceiling("manual_approval", "FULLY_AUTONOMOUS")

    def test_canonical_ceiling_still_passes(self) -> None:
        # The canonical spelling is unaffected by the rejection above.
        assert validate_autonomy_ceiling("manual_approval", "fully_autonomous") is None


class TestMaxAutonomyReservedKey:
    def test_pipeline_max_autonomy_is_reserved(self) -> None:
        """FAR-1163: a context-setter may never overwrite the pinned ceiling
        (it would lift the cap on its own recommendation)."""
        from modulo.core.pipeline_engine.decorator import _RESERVED_RUN_CONTEXT_KEYS

        assert PIPELINE_MAX_AUTONOMY_KEY in _RESERVED_RUN_CONTEXT_KEYS
        assert PIPELINE_MAX_AUTONOMY_KEY == "_pipeline_max_autonomy"


class TestEarnedAutonomyResolution:
    """FAR-1175 (ADR 043 S1): earned-level resolution at a HITL gate."""

    def test_gating_off_ignores_earned_live(self) -> None:
        """With the flag off, resolution is byte-identical to S0 — the live
        earned level has no effect."""
        result = effective_autonomy_level(
            pipeline_default="manual_approval",
            run_context={PIPELINE_EARNED_AT_START_KEY: "manual_approval"},
            earned_live="fully_autonomous",
            gating_enabled=False,
        )
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_gating_on_earned_live_lowers_below_default(self) -> None:
        """Gating on: the live earned level becomes the base (a demotion from a
        fully_autonomous default)."""
        result = effective_autonomy_level(
            pipeline_default="fully_autonomous",
            run_context={},
            earned_live="manual_approval",
            gating_enabled=True,
        )
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_gating_on_earned_live_raises_above_default(self) -> None:
        """Gating on: a promoted earned level raises the base above the default."""
        result = effective_autonomy_level(
            pipeline_default="manual_approval",
            run_context={PIPELINE_MAX_AUTONOMY_KEY: "fully_autonomous"},
            earned_live="fully_autonomous",
            gating_enabled=True,
        )
        assert result == AutonomyLevel.FULLY_AUTONOMOUS

    def test_demotion_visible_at_next_gate_of_in_flight_run(self) -> None:
        """D2: a demotion bites an in-flight run at its next gate — the live
        level (lower) wins over the pinned-at-start level."""
        result = effective_autonomy_level(
            pipeline_default="fully_autonomous",
            run_context={PIPELINE_EARNED_AT_START_KEY: "fully_autonomous"},
            earned_live="manual_approval",
            gating_enabled=True,
        )
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_promotion_not_visible_to_in_flight_run(self) -> None:
        """D2: a promotion must not loosen an in-flight run — the pinned-at-start
        level (lower) still holds even though the live level is higher."""
        result = effective_autonomy_level(
            pipeline_default="manual_approval",
            run_context={PIPELINE_EARNED_AT_START_KEY: "manual_approval"},
            earned_live="fully_autonomous",
            gating_enabled=True,
        )
        assert result == AutonomyLevel.MANUAL_APPROVAL

    def test_ceiling_always_holds_against_earned(self) -> None:
        """The ceiling clamps the earned base even when both pin and live are
        above it."""
        resolution = resolve_autonomy(
            pipeline_default="manual_approval",
            run_context={
                PIPELINE_EARNED_AT_START_KEY: "fully_autonomous",
                PIPELINE_MAX_AUTONOMY_KEY: "notify_on_complete",
            },
            earned_live="fully_autonomous",
            gating_enabled=True,
        )
        assert resolution.effective == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert resolution.ceiling == AutonomyLevel.NOTIFY_ON_COMPLETE

    def test_earned_base_then_recommendation_clamped_to_ceiling(self) -> None:
        """A recommendation above the earned base may raise only to the ceiling."""
        resolution = resolve_autonomy(
            pipeline_default="manual_approval",
            run_context={
                PIPELINE_EARNED_AT_START_KEY: "manual_approval",
                PIPELINE_MAX_AUTONOMY_KEY: "notify_on_complete",
                "autonomy_recommendation": "fully_autonomous",
            },
            earned_live="manual_approval",
            gating_enabled=True,
        )
        assert resolution.effective == AutonomyLevel.NOTIFY_ON_COMPLETE
        assert resolution.clamped is True

    def test_only_pin_present_uses_pin(self) -> None:
        # An explicit ceiling must accompany the earned level — otherwise the
        # effective ceiling is the default and clamps the raise (S0 rule).
        result = effective_autonomy_level(
            pipeline_default="manual_approval",
            run_context={
                PIPELINE_EARNED_AT_START_KEY: "notify_on_complete",
                PIPELINE_MAX_AUTONOMY_KEY: "notify_on_complete",
            },
            earned_live=None,
            gating_enabled=True,
        )
        assert result == AutonomyLevel.NOTIFY_ON_COMPLETE

    def test_only_live_present_uses_live(self) -> None:
        result = effective_autonomy_level(
            pipeline_default="manual_approval",
            run_context={PIPELINE_MAX_AUTONOMY_KEY: "notify_on_complete"},
            earned_live="notify_on_complete",
            gating_enabled=True,
        )
        assert result == AutonomyLevel.NOTIFY_ON_COMPLETE

    def test_gating_on_no_earned_falls_back_to_default(self) -> None:
        """NULL earned (never set) means the pinned default is the base."""
        result = effective_autonomy_level(
            pipeline_default="notify_on_complete",
            run_context={},
            earned_live=None,
            gating_enabled=True,
        )
        assert result == AutonomyLevel.NOTIFY_ON_COMPLETE


class TestEarnedAutonomyReservedKey:
    def test_earned_at_start_is_reserved(self) -> None:
        """FAR-1175: a context-setter may never overwrite the pinned earned
        level (it would let the agent erase its own in-flight ceiling)."""
        from modulo.core.pipeline_engine.decorator import _RESERVED_RUN_CONTEXT_KEYS

        assert PIPELINE_EARNED_AT_START_KEY in _RESERVED_RUN_CONTEXT_KEYS
        assert PIPELINE_EARNED_AT_START_KEY == "_pipeline_earned_at_start"

    def test_gating_flag_name(self) -> None:
        assert AUTONOMY_GATING_FLAG == "autonomy_gating"
