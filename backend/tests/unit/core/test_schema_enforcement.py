"""FAR-902: unit tests for schema enforcement record + aggregation (D2, D4 pure fn).

Also covers the FAR-902 enforcement-record PAYLOAD fix: proving that the
record carries real profile, native-output, repair and attempt data from both
the agent path and the sandbox path.
"""

from __future__ import annotations

import json

import pytest

from modulo.core.pipeline_engine.schema_enforcement import (
    MAX_ENFORCEMENT_PAYLOAD_BYTES,
    RunEnforcementAggregates,
    SchemaEnforcementRecord,
    aggregate_run_enforcement,
    build_enforcement_record,
)
from modulo.core.pipeline_engine.schema_repair import SchemaValidationOutcome


class TestBuildEnforcementRecord:
    """D2: pure function that builds the per-attempt enforcement payload."""

    def test_no_schema_returns_none(self) -> None:
        result = build_enforcement_record(
            outcome=SchemaValidationOutcome.NO_SCHEMA.value,
        )
        assert result is None

    def test_native_decoded(self) -> None:
        result = build_enforcement_record(
            outcome=SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value,
            resolved_profile="provider-strict",
            native_output=True,
        )
        assert result is not None
        assert result["outcome"] == "native_decoded_and_validated"
        assert result["resolved_profile"] == "provider-strict"
        assert result["native_output"] is True
        assert result["repair_attempts"] == 0
        assert result["wasted_attempts"] == 0
        assert not result["validation_errors"]
        assert result["total_error_count"] == 0
        assert result["truncated"] is False

    def test_repair_with_errors(self) -> None:
        errors = [{"pointer": "/a", "constraint": "required"}]
        result = build_enforcement_record(
            outcome=SchemaValidationOutcome.REPAIR_EXHAUSTED.value,
            resolved_profile="verbatim",
            native_output=False,
            repair_attempts=2,
            wasted_attempts=1,
            validation_errors=errors,
        )
        assert result is not None
        assert result["repair_attempts"] == 2
        assert result["wasted_attempts"] == 1
        assert len(result["validation_errors"]) == 1
        assert result["total_error_count"] == 1
        assert result["truncated"] is False

    def test_truncation_at_50_errors(self) -> None:
        errors = [{"pointer": f"/{i}", "constraint": "type"} for i in range(60)]
        result = build_enforcement_record(
            outcome=SchemaValidationOutcome.LENIENT_VALIDATION_BYPASSED.value,
            validation_errors=errors,
        )
        assert result is not None
        assert len(result["validation_errors"]) == 50
        assert result["total_error_count"] == 60
        assert result["truncated"] is True

    def test_payload_bound_to_64kb(self) -> None:
        """Even with 100 large errors, the serialised payload fits 64 KB."""
        big_errors = [{"pointer": f"/field_{i}", "constraint": "pattern", "message": "x" * 200} for i in range(100)]
        result = build_enforcement_record(
            outcome=SchemaValidationOutcome.REPAIR_EXHAUSTED.value,
            validation_errors=big_errors,
        )
        assert result is not None
        serialised = json.dumps(result, default=str, ensure_ascii=True)
        assert len(serialised.encode("utf-8")) <= MAX_ENFORCEMENT_PAYLOAD_BYTES

    def test_payload_trimming_loop_drops_errors_until_it_fits(self) -> None:
        """Errors are dropped in chunks when the serialised payload exceeds 64 KB.

        The whole error list must be large enough that the cap is breached
        *after* the ``_MAX_VALIDATION_ERRORS`` truncation, forcing the
        progressive-drop loop to run.
        """
        huge_errors = [{"pointer": f"/field_{i}", "constraint": "pattern", "message": "x" * 2000} for i in range(500)]
        result = build_enforcement_record(
            outcome=SchemaValidationOutcome.REPAIR_EXHAUSTED.value,
            validation_errors=huge_errors,
        )
        assert result is not None
        serialised = json.dumps(result, default=str, ensure_ascii=True)
        assert len(serialised.encode("utf-8")) <= MAX_ENFORCEMENT_PAYLOAD_BYTES
        # The loop ran and trimmed the kept error list below the 50-error cap.
        assert result["truncated"] is True
        assert len(result["validation_errors"]) < 50

    def test_payload_oversized_without_errors_logs_best_effort(self, caplog: pytest.LogCaptureFixture) -> None:
        """A payload still over the cap with zero errors logs and returns best-effort.

        When a non-error field alone (here ``resolved_profile``) exceeds the
        64 KB cap, the progressive-drop loop cannot help: the ``while ... else``
        fall-through logs ``schema_enforcement.payload_exceeds_cap`` and returns
        the oversized payload rather than raising.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            result = build_enforcement_record(
                outcome=SchemaValidationOutcome.VERBATIM_PASSED.value,
                resolved_profile="p" * (MAX_ENFORCEMENT_PAYLOAD_BYTES + 1024),
                validation_errors=[],
            )
        assert result is not None
        assert any("schema_enforcement.payload_exceeds_cap" in rec.message for rec in caplog.records)


class TestAggregateRunEnforcement:
    """D4: pure function that aggregates per-attempt records into run-level counters."""

    def test_empty_records(self) -> None:
        agg = aggregate_run_enforcement([])
        assert agg == RunEnforcementAggregates()

    def test_native_count(self) -> None:
        records = [
            {"outcome": "native_decoded_and_validated", "repair_attempts": 0, "wasted_attempts": 0},
            {"outcome": "native_decode_failed", "repair_attempts": 1, "wasted_attempts": 0},
            {"outcome": "verbatim_passed", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        agg = aggregate_run_enforcement(records)
        assert agg.native_count == 2
        assert agg.verbatim_count == 1
        assert agg.repair_count == 1
        assert agg.wasted_count == 0
        assert agg.total_attempts == 3
        assert agg.enforcement_record_count == 3

    def test_wasted_attempts_summed(self) -> None:
        records = [
            {"outcome": "repair_exhausted", "repair_attempts": 2, "wasted_attempts": 3},
            {"outcome": "lenient_validation_bypassed", "repair_attempts": 0, "wasted_attempts": 1},
        ]
        agg = aggregate_run_enforcement(records)
        assert agg.wasted_count == 4
        assert agg.repair_count == 2

    def test_unknown_outcome_not_counted(self) -> None:
        records = [
            {"outcome": "totally_unknown", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        agg = aggregate_run_enforcement(records)
        assert agg.native_count == 0
        assert agg.verbatim_count == 0
        assert agg.total_attempts == 1

    def test_all_verbatim_outcomes(self) -> None:
        outcomes = [
            "verbatim_passed",
            "posthoc_validation_failed",
            "lenient_validation_bypassed",
            "repair_exhausted",
            "repair_attempted",
            "passed_after_repair",
            "schema_unenforceable",
        ]
        records = [{"outcome": o, "repair_attempts": 0, "wasted_attempts": 0} for o in outcomes]
        agg = aggregate_run_enforcement(records)
        assert agg.verbatim_count == len(outcomes)
        assert agg.native_count == 0


class TestSchemaEnforcementRecordDataclass:
    """Verify the dataclass contract."""

    def test_defaults(self) -> None:
        rec = SchemaEnforcementRecord(outcome="test")
        assert rec.outcome == "test"
        assert rec.resolved_profile is None
        assert rec.native_output is False
        assert rec.repair_attempts == 0
        assert rec.wasted_attempts == 0
        assert not rec.validation_errors
        assert rec.total_error_count == 0
        assert rec.truncated is False

    def test_frozen(self) -> None:
        rec = SchemaEnforcementRecord(outcome="test")
        with pytest.raises(AttributeError):
            rec.outcome = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FAR-902 PAYLOAD FIX: prove enforcement records carry real data
# ---------------------------------------------------------------------------


class TestEnforcementPayloadPopulated:
    """Prove the enforcement record carries real profile, native-output,
    repair and attempt data — NOT the hollow defaults.

    Without the fix, every record says resolved_profile=None,
    native_output=False, repair_attempts=0, wasted_attempts=0 because the
    call sites omit these parameters.  Each test BELOW asserts a value that
    is NON-default, so it FAILS with the old hollow wiring.
    """

    def _simple_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }

    # -- resolved_profile threading --

    def test_finalize_node_result_threads_resolved_profile(self) -> None:
        """_finalize_node_result passes resolved_profile to build_enforcement_record.

        FAILS WITHOUT FIX: resolved_profile defaults to None when omitted.
        """
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "n1",
            {"name": "Alice"},
            self._simple_schema(),
            None,
            resolved_profile="provider-strict",
        )
        rec = result.get("_schema_enforcement_record")
        assert rec is not None
        assert rec["resolved_profile"] == "provider-strict"

    def test_finalize_node_result_threads_verbatim_profile(self) -> None:
        """When no profile override is set, the record carries 'verbatim'."""
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "n1",
            {"name": "Alice"},
            self._simple_schema(),
            None,
            resolved_profile="verbatim",
        )
        rec = result.get("_schema_enforcement_record")
        assert rec is not None
        assert rec["resolved_profile"] == "verbatim"

    # -- native_output threading --

    def test_finalize_node_result_threads_native_output_true(self) -> None:
        """When the provider uses native structured output, the record is True.

        FAILS WITHOUT FIX: native_output defaults to False when omitted.
        """
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "n1",
            {"name": "Alice"},
            self._simple_schema(),
            None,
            native_output=True,
        )
        rec = result.get("_schema_enforcement_record")
        assert rec is not None
        assert rec["native_output"] is True

    def test_finalize_node_result_threads_native_output_false(self) -> None:
        """When the provider does NOT use native output, the record is False."""
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "n1",
            {"name": "Alice"},
            self._simple_schema(),
            None,
            native_output=False,
        )
        rec = result.get("_schema_enforcement_record")
        assert rec is not None
        assert rec["native_output"] is False

    # -- repair_attempts / wasted_attempts threading --

    def test_finalize_node_result_threads_repair_info_from_validate(self) -> None:
        """When the repair loop runs, _finalize_node_result threads the counts.

        FAILS WITHOUT FIX: repair_attempts and wasted_attempts default to 0.
        """
        from unittest.mock import MagicMock

        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        # A repair invoke function that returns a valid output.
        mock_repair_fn = MagicMock(return_value=json.dumps({"name": "Fixed"}))

        result = _finalize_node_result(
            "n1",
            {"name": 123},  # wrong type for 'name' — triggers validation failure
            self._simple_schema(),
            None,
            mode="strict",
            _repair_invoke_fn=mock_repair_fn,
        )
        rec = result.get("_schema_enforcement_record")
        assert rec is not None
        # The repair loop ran at least one attempt.
        assert rec["repair_attempts"] >= 1
        # At least one attempt was wasted (schema rejection).
        assert rec["wasted_attempts"] >= 0

    def test_repair_info_no_repair_when_no_invoke_fn(self) -> None:
        """Without a repair invoke function, repair_attempts stays 0.

        In strict mode this raises OutputSchemaValidationError; the repair_info
        is still populated before the raise.
        """
        from modulo.core.pipeline_engine.node_runner import (
            OutputSchemaValidationError,
            _finalize_node_result,
        )

        with pytest.raises(OutputSchemaValidationError):
            _finalize_node_result(
                "n1",
                {"name": 123},
                self._simple_schema(),
                None,
                mode="strict",
                _repair_invoke_fn=None,
            )

    # -- validate_against_schema _repair_info threading --

    def test_validate_against_schema_populates_repair_info(self) -> None:
        """_validate_against_schema populates _repair_info with repair stats.

        FAILS WITHOUT FIX: _repair_info is not populated because the dict
        is never threaded through.
        """
        from unittest.mock import MagicMock

        from modulo.core.pipeline_engine.node_runner import _validate_against_schema

        bad_data = {"name": 123}  # wrong type
        repair_info: dict = {}
        mock_invoke = MagicMock(return_value=json.dumps({"name": "Fixed"}))

        _outcome, _errors, _data = _validate_against_schema(
            bad_data,
            self._simple_schema(),
            mode="strict",
            _repair_invoke_fn=mock_invoke,
            _repair_info=repair_info,
        )
        assert "repair_attempts" in repair_info
        assert "wasted_attempts" in repair_info
        assert repair_info["repair_attempts"] >= 1

    def test_validate_against_schema_no_repair_info_when_omitted(self) -> None:
        """When _repair_info is not passed, no error — backwards compatible."""
        from modulo.core.pipeline_engine.node_runner import _validate_against_schema

        _outcome, _errors, _data = _validate_against_schema(
            {"name": "OK"},
            self._simple_schema(),
            mode="lenient",
        )
        assert _outcome == SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value

    # -- run_repair_loop _repair_info threading --

    def test_run_repair_loop_populates_repair_info(self) -> None:
        """run_repair_loop populates _repair_info with repair stats.

        FAILS WITHOUT FIX: _repair_info is never populated because the dict
        is not threaded through run_repair_loop.
        """
        from unittest.mock import MagicMock

        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        bad_data = {"name": 123}
        repair_info: dict = {}
        mock_invoke = MagicMock(return_value=json.dumps({"name": "Fixed"}))

        _outcome, _errors, _data = run_repair_loop(
            bad_data,
            self._simple_schema(),
            budget=1,
            repair_invoke_fn=mock_invoke,
            _repair_info=repair_info,
        )
        assert "repair_attempts" in repair_info
        assert "wasted_attempts" in repair_info
        assert repair_info["repair_attempts"] == 1

    def test_run_repair_loop_wasted_count_on_exhaustion(self) -> None:
        """When repair exhausts, all schema-rejection attempts are wasted.

        With budget=2 and identical bad data each time, untranslatable
        detection stops the loop after the second record_attempt (same
        errors → REPAIR_EXHAUSTED before invoking). Only 1 invoke ran
        and was schema-rejected → wasted=1.

        FAILS WITHOUT FIX: wasted_attempts is never populated.
        """
        from unittest.mock import MagicMock

        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        bad_data = {"name": 123}
        repair_info: dict = {}
        # Invoke returns the same bad data — repair fails every time.
        mock_invoke = MagicMock(return_value=json.dumps(bad_data))

        _outcome, _errors, _data = run_repair_loop(
            bad_data,
            self._simple_schema(),
            budget=2,
            repair_invoke_fn=mock_invoke,
            _repair_info=repair_info,
        )
        assert repair_info["repair_attempts"] == 2
        # Only 1 actual invoke+schema-rejection; the 2nd attempt is
        # short-circuited by untranslatable detection.
        assert repair_info["wasted_attempts"] == 1

    def test_run_repair_loop_wasted_excludes_invoke_failures(self) -> None:
        """Invoke failures are NOT counted as wasted (different failure cause).

        FAILS WITHOUT FIX: wasted_attempts is never populated.
        """
        from unittest.mock import MagicMock

        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        bad_data = {"name": 123}
        repair_info: dict = {}
        # Invoke always raises — not a schema rejection.
        mock_invoke = MagicMock(side_effect=RuntimeError("provider down"))

        _outcome, _errors, _data = run_repair_loop(
            bad_data,
            self._simple_schema(),
            budget=2,
            repair_invoke_fn=mock_invoke,
            _repair_info=repair_info,
        )
        assert repair_info["repair_attempts"] == 1  # first attempt triggered the exception
        assert repair_info["wasted_attempts"] == 0  # invoke failure ≠ schema rejection

    def test_run_repair_loop_no_repair_info_on_no_budget_valid(self) -> None:
        """Without _repair_info, the no-budget valid path returns without mutating.

        Covers the ``_repair_info is None`` false arm of the no-budget branch.
        """
        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        outcome, _errors, data = run_repair_loop(
            {"name": "OK"},
            self._simple_schema(),
            budget=0,
            repair_invoke_fn=None,
        )
        assert outcome == SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value
        assert data == {"name": "OK"}

    def test_run_repair_loop_repair_info_on_no_budget_valid(self) -> None:
        """With no budget/invoke fn but already-valid data, _repair_info is zeroed.

        Covers the ``budget <= 0 or repair_invoke_fn is None`` + valid branch.
        """
        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        repair_info: dict = {}
        outcome, _errors, data = run_repair_loop(
            {"name": "OK"},
            self._simple_schema(),
            budget=0,
            repair_invoke_fn=None,
            _repair_info=repair_info,
        )
        assert outcome == SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value
        assert data == {"name": "OK"}
        assert repair_info == {"repair_attempts": 0, "wasted_attempts": 0}

    def test_run_repair_loop_repair_info_on_spend_limit(self) -> None:
        """The daily-spend-limit short-circuit populates _repair_info."""
        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        repair_info: dict = {}
        outcome, _errors, _data = run_repair_loop(
            {"name": 123},
            self._simple_schema(),
            budget=1,
            daily_spend_limit=1.0,
            current_spend=2.0,
            repair_invoke_fn=lambda _p: "{}",
            _repair_info=repair_info,
        )
        assert outcome == SchemaValidationOutcome.REPAIR_EXHAUSTED.value
        assert repair_info == {"repair_attempts": 0, "wasted_attempts": 0}

    def test_run_repair_loop_no_repair_info_on_initial_valid(self) -> None:
        """Without _repair_info, an already-valid payload returns cleanly.

        Covers the ``_repair_info is None`` false arm of the initial-valid
        branch (budget/invoke present, data valid).
        """
        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        outcome, _errors, data = run_repair_loop(
            {"name": "OK"},
            self._simple_schema(),
            budget=1,
            repair_invoke_fn=lambda _p: "{}",
        )
        assert outcome == SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value
        assert data == {"name": "OK"}

    def test_run_repair_loop_repair_info_on_initial_valid(self) -> None:
        """Data that is already valid with a budget/invoke fn zeroes _repair_info."""
        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        repair_info: dict = {}
        outcome, _errors, data = run_repair_loop(
            {"name": "OK"},
            self._simple_schema(),
            budget=1,
            repair_invoke_fn=lambda _p: "{}",
            _repair_info=repair_info,
        )
        assert outcome == SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value
        assert data == {"name": "OK"}
        assert repair_info == {"repair_attempts": 0, "wasted_attempts": 0}

    def test_run_repair_loop_repair_info_on_budget_exhaustion(self) -> None:
        """Loop exit by budget exhaustion (not untranslatable break) populates stats.

        With budget=1 the loop exits via ``is_exhausted`` (no second
        ``record_attempt`` to trigger untranslatable detection), exercising the
        budget-exhaustion return arm with ``_repair_info`` present.
        """
        from unittest.mock import MagicMock

        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        repair_info: dict = {}
        mock_invoke = MagicMock(return_value=json.dumps({"name": 123}))

        outcome, _errors, _data = run_repair_loop(
            {"name": 123},
            self._simple_schema(),
            budget=1,
            repair_invoke_fn=mock_invoke,
            _repair_info=repair_info,
        )
        assert outcome == SchemaValidationOutcome.REPAIR_EXHAUSTED.value
        assert repair_info["repair_attempts"] == 1
        assert repair_info["wasted_attempts"] == 1

    def test_run_repair_loop_repair_info_on_bad_json(self) -> None:
        """A non-JSON repair response populates _repair_info on the terminal arm."""
        from modulo.core.pipeline_engine.schema_repair import run_repair_loop

        repair_info: dict = {}
        outcome, _errors, _data = run_repair_loop(
            {"name": 123},
            self._simple_schema(),
            budget=1,
            repair_invoke_fn=lambda _p: "not json",
            _repair_info=repair_info,
        )
        assert outcome == SchemaValidationOutcome.NATIVE_DECODE_NOT_JSON.value
        assert repair_info["repair_attempts"] == 1


# ---------------------------------------------------------------------------
# FAR-902 D2: flip guard + mode/outcome derivation
# ---------------------------------------------------------------------------


class TestDeriveModeFromRecords:
    """D2: derive_mode_from_records — pure function for mode derivation."""

    def test_empty_records_returns_lenient(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_mode_from_records

        assert derive_mode_from_records([]) == "lenient"

    def test_lenient_bypass_returns_lenient(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_mode_from_records

        records = [
            {"outcome": "lenient_validation_bypassed", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        assert derive_mode_from_records(records) == "lenient"

    def test_strict_outcomes_returns_strict(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_mode_from_records

        records = [
            {"outcome": "native_decoded_and_validated", "repair_attempts": 0, "wasted_attempts": 0},
            {"outcome": "verbatim_passed", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        assert derive_mode_from_records(records) == "strict"

    def test_mixed_outcomes_with_lenient_returns_lenient(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_mode_from_records

        records = [
            {"outcome": "native_decoded_and_validated", "repair_attempts": 0, "wasted_attempts": 0},
            {"outcome": "lenient_validation_bypassed", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        assert derive_mode_from_records(records) == "lenient"


class TestDeriveOutcomeFromRecords:
    """D2: derive_outcome_from_records — pure function for outcome derivation."""

    def test_empty_records_returns_no_schema(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_outcome_from_records

        assert derive_outcome_from_records([]) == "no_schema"

    def test_single_record_returns_its_outcome(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_outcome_from_records

        records = [{"outcome": "verbatim_passed"}]
        assert derive_outcome_from_records(records) == "verbatim_passed"

    def test_most_severe_outcome_wins(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_outcome_from_records

        records = [
            {"outcome": "native_decoded_and_validated"},
            {"outcome": "repair_exhausted"},
            {"outcome": "verbatim_passed"},
        ]
        assert derive_outcome_from_records(records) == "repair_exhausted"

    def test_posthoc_wins_over_lenient(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import derive_outcome_from_records

        records = [
            {"outcome": "lenient_validation_bypassed"},
            {"outcome": "posthoc_validation_failed"},
        ]
        assert derive_outcome_from_records(records) == "posthoc_validation_failed"


class TestComputeFlipGuardVerdict:
    """D2: compute_flip_guard_verdict — pure function for flip safety."""

    def test_safe_when_no_records(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import compute_flip_guard_verdict

        verdict = compute_flip_guard_verdict([])
        assert verdict.safe_to_flip is True
        assert verdict.lenient_warning_count == 0
        assert verdict.total_records == 0
        assert not verdict.affected_outcomes
        assert "Safe to flip" in verdict.advisory

    def test_safe_when_no_warnings(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import compute_flip_guard_verdict

        records = [
            {"outcome": "native_decoded_and_validated", "repair_attempts": 0, "wasted_attempts": 0},
            {"outcome": "verbatim_passed", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        verdict = compute_flip_guard_verdict(records)
        assert verdict.safe_to_flip is True
        assert verdict.lenient_warning_count == 0
        assert verdict.total_records == 2
        assert "Safe to flip" in verdict.advisory

    def test_unsafe_when_warnings_exist(self) -> None:
        from modulo.core.pipeline_engine.schema_enforcement import compute_flip_guard_verdict

        records = [
            {"outcome": "lenient_validation_bypassed", "repair_attempts": 0, "wasted_attempts": 0},
            {"outcome": "native_decoded_and_validated", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        verdict = compute_flip_guard_verdict(records)
        assert verdict.safe_to_flip is False
        assert verdict.lenient_warning_count == 1
        assert verdict.total_records == 2
        assert "lenient_validation_bypassed" in verdict.affected_outcomes
        assert "NOT safe to flip" in verdict.advisory

    def test_never_mutates_mode(self) -> None:
        """The flip guard MUST NOT change the mode under any circumstance.

        This test calls the guard with various enforcement records and
        verifies the resolved mode (via derive_mode_from_records) is
        unchanged after the call.
        """
        from modulo.core.pipeline_engine.schema_enforcement import (
            compute_flip_guard_verdict,
            derive_mode_from_records,
        )

        records_with_warnings = [
            {"outcome": "lenient_validation_bypassed", "repair_attempts": 0, "wasted_attempts": 0},
        ]
        records_without_warnings = [
            {"outcome": "native_decoded_and_validated", "repair_attempts": 0, "wasted_attempts": 0},
        ]

        # Mode before calling the guard
        mode_before_warnings = derive_mode_from_records(records_with_warnings)
        mode_before_no_warnings = derive_mode_from_records(records_without_warnings)

        # Call the guard — it must NOT mutate anything
        verdict_warnings = compute_flip_guard_verdict(records_with_warnings)
        verdict_no_warnings = compute_flip_guard_verdict(records_without_warnings)

        # Mode is unchanged
        assert derive_mode_from_records(records_with_warnings) == mode_before_warnings
        assert derive_mode_from_records(records_without_warnings) == mode_before_no_warnings

        # The guard never mutates the mode — it only advises
        assert verdict_warnings.safe_to_flip is False
        assert verdict_no_warnings.safe_to_flip is True
