"""FAR-902: unit tests for schema enforcement record + aggregation (D2, D4 pure fn)."""

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
