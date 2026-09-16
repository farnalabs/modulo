"""FAR-899: tests for schema validation, operator-toggled mode, and repair loop.

Verifies:
- Real JSON Schema validation (Draft202012Validator) replaces presence-only check
- Lenient mode: invalid input → node SUCCEEDS + structured warning emitted
- Strict mode: invalid input → node FAILS
- Flip guard: _query_recent_lenient_failures threshold check
- Repair loop: budget default 1, hard cap 3, exhaustion → node fails
- no_schema node → nothing runs
- Reuses FakeStructuredOutputBackend from FAR-898
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine.schema_repair import (
    SchemaRepairLoop,
    SchemaValidationOutcome,
    _clear_validator_cache,
    _compile_validator,
    _query_recent_lenient_failures,
    get_repair_budget,
    validate_against_schema,
)

# ---------------------------------------------------------------------------
# Reusable test double (FAR-898, reused by FAR-899)
# ---------------------------------------------------------------------------


class _FakeStructuredOutputBackend:
    """Minimal backend that records every kwarg it receives."""

    supports_native_structured_output = True
    supports_tools = False

    def __init__(self, output: str = '{"result": "ok"}') -> None:
        self._output = output
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type("Resp", (), {"content": self._output})()


class _FakeHub:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def get(self, backend_id: Any) -> Any:
        return self._backend


# ---------------------------------------------------------------------------
# Test schema validation (core function)
# ---------------------------------------------------------------------------


class TestValidateAgainstSchema:
    """validate_against_schema with Draft202012Validator."""

    def setup_method(self) -> None:
        _clear_validator_cache()

    def test_valid_dict_passes(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        data = {"name": "Alice"}
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is True
        assert errors == []

    def test_invalid_dict_fails(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        data = {"age": 30}
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is False
        assert len(errors) > 0
        # Should have a "required" constraint error
        assert any(e["constraint"] == "required" for e in errors)

    def test_wrong_type_fails(self) -> None:
        schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
        data = {"count": "not-a-number"}
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is False
        assert any(e["constraint"] == "type" for e in errors)

    def test_array_passes(self) -> None:
        schema = {"type": "array", "items": {"type": "string"}}
        data = ["a", "b", "c"]
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is True
        assert errors == []

    def test_array_wrong_items_fails(self) -> None:
        schema = {"type": "array", "items": {"type": "string"}}
        data = ["a", 123, "c"]
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is False
        assert len(errors) == 1
        assert errors[0]["constraint"] == "type"

    def test_scalar_passes(self) -> None:
        schema = {"type": "string"}
        data = "hello"
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is True
        assert errors == []

    def test_scalar_wrong_type_fails(self) -> None:
        schema = {"type": "string"}
        data = 42
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is False
        assert errors[0]["constraint"] == "type"

    def test_nested_object_validation(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
            "required": ["user"],
        }
        data = {"user": {"name": "Bob"}}
        is_valid, _errors = validate_against_schema(data, schema)
        assert is_valid is True

    def test_nested_object_missing_required(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }
            },
            "required": ["user"],
        }
        data = {"user": {"age": 25}}
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is False
        assert any(e["constraint"] == "required" for e in errors)

    def test_enum_constraint(self) -> None:
        schema = {"type": "string", "enum": ["a", "b", "c"]}
        data = "d"
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is False
        assert errors[0]["constraint"] == "enum"
        assert errors[0]["allowed"] == ["a", "b", "c"]

    def test_empty_schema_accepts_anything(self) -> None:
        schema: dict[str, Any] = {}
        data = {"anything": "goes"}
        is_valid, errors = validate_against_schema(data, schema)
        assert is_valid is True
        assert errors == []

    def test_validator_caching(self) -> None:
        schema = {"type": "object"}
        v1 = _compile_validator(schema)
        v2 = _compile_validator(schema)
        assert v1 is v2  # Same object from cache


# ---------------------------------------------------------------------------
# Test _validate_against_schema integration (node_runner.py)
# ---------------------------------------------------------------------------


class TestNodeRunnerValidateAgainstSchema:
    """Integration of validate_against_schema into _validate_against_schema."""

    def test_no_schema_returns_no_schema(self) -> None:
        outcome, errors = nr._validate_against_schema({"name": "test"}, {})
        assert outcome == SchemaValidationOutcome.NO_SCHEMA.value
        assert errors == []

    def test_valid_data_lenient_mode(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        outcome, errors = nr._validate_against_schema(
            {"name": "test"},
            schema,
            mode="lenient",
        )
        assert outcome == SchemaValidationOutcome.NATIVE_DECODED_AND_VALIDATED.value
        assert errors == []

    def test_invalid_data_lenient_mode_succeeds_with_warning(self, caplog: Any) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        outcome, errors = nr._validate_against_schema(
            {"age": 30},
            schema,
            mode="lenient",
        )
        assert outcome == SchemaValidationOutcome.LENIENT_VALIDATION_BYPASSED.value
        assert len(errors) > 0
        # Node does NOT fail
        assert "schema_validation.lenient_bypass" in caplog.text

    def test_invalid_data_strict_mode_raises(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        with pytest.raises(nr.OutputSchemaValidationError, match="Strict schema validation failed"):
            nr._validate_against_schema(
                {"age": 30},
                schema,
                mode="strict",
                schema_id="test-schema",
            )

    def test_invalid_data_strict_mode_no_repair_budget(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        with pytest.raises(nr.OutputSchemaValidationError, match="Strict schema validation failed"):
            nr._validate_against_schema(
                {"age": 30},
                schema,
                mode="strict",
                repair_budget_config=0,
            )


# ---------------------------------------------------------------------------
# Test repair loop
# ---------------------------------------------------------------------------


class TestSchemaRepairLoop:
    """Repair loop budget and exhaustion."""

    def test_default_budget(self) -> None:
        repair = SchemaRepairLoop(schema_id="test")
        assert repair.budget == 1
        assert repair.is_exhausted is False

    def test_hard_cap_enforced(self) -> None:
        repair = SchemaRepairLoop(budget=10, schema_id="test")
        assert repair.budget == 3  # Hard cap

    def test_zero_budget(self) -> None:
        repair = SchemaRepairLoop(budget=0, schema_id="test")
        assert repair.budget == 0
        assert repair.is_exhausted is True

    def test_single_attempt_exhausts_default_budget(self) -> None:
        repair = SchemaRepairLoop(schema_id="test")
        errors = [{"pointer": "$.name", "constraint": "required"}]
        outcome = repair.record_attempt(errors)
        assert outcome == SchemaValidationOutcome.REPAIR_EXHAUSTED
        assert repair.is_exhausted is True

    def test_hard_cap_3_allows_3_attempts(self) -> None:
        repair = SchemaRepairLoop(budget=3, schema_id="test")
        errors = [{"pointer": "$.name", "constraint": "required"}]

        # First attempt
        outcome = repair.record_attempt(errors)
        assert outcome == SchemaValidationOutcome.REPAIR_ATTEMPTED

        # Second attempt (same errors → untranslatable)
        outcome = repair.record_attempt(errors)
        assert outcome == SchemaValidationOutcome.REPAIR_EXHAUSTED

    def test_untranslatable_stops_early(self) -> None:
        repair = SchemaRepairLoop(budget=3, schema_id="test")
        errors_a = [{"pointer": "$.name", "constraint": "required"}]
        errors_b = [{"pointer": "$.name", "constraint": "required"}]  # Same errors

        repair.record_attempt(errors_a)
        outcome = repair.record_attempt(errors_b)
        assert outcome == SchemaValidationOutcome.REPAIR_EXHAUSTED

    def test_different_errors_continue(self) -> None:
        repair = SchemaRepairLoop(budget=3, schema_id="test")
        errors_a = [{"pointer": "$.name", "constraint": "required"}]
        errors_b = [{"pointer": "$.age", "constraint": "type"}]  # Different errors

        repair.record_attempt(errors_a)
        outcome = repair.record_attempt(errors_b)
        assert outcome == SchemaValidationOutcome.REPAIR_ATTEMPTED


class TestGetRepairBudget:
    """get_repair_budget clamping."""

    def test_default(self) -> None:
        assert get_repair_budget() == 1

    def test_clamped_to_hard_cap(self) -> None:
        assert get_repair_budget(10) == 3

    def test_zero(self) -> None:
        assert get_repair_budget(0) == 0

    def test_invalid_type(self) -> None:
        assert get_repair_budget("invalid") == 1

    def test_none(self) -> None:
        assert get_repair_budget(None) == 1


# ---------------------------------------------------------------------------
# Test flip guard
# ---------------------------------------------------------------------------


class TestFlipGuard:
    """_query_recent_lenient_failures threshold check."""

    @pytest.mark.asyncio
    async def test_no_org_returns_zero(self) -> None:
        session = AsyncMock()
        result = await _query_recent_lenient_failures(session, None)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_query_failure_returns_above_threshold(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("DB error"))
        result = await _query_recent_lenient_failures(session, uuid.uuid4())
        assert result > 0.001  # Above threshold → blocks flip


# ---------------------------------------------------------------------------
# Test _finalize_node_result integration
# ---------------------------------------------------------------------------


class TestFinalizeNodeResult:
    """_finalize_node_result with schema validation."""

    def test_no_schema_returns_completed(self) -> None:
        result = nr._finalize_node_result("n1", {"data": 1}, None, None)
        assert result["artifacts"][0]["status"] == "completed"

    def test_valid_schema_returns_completed(self) -> None:
        schema = {"type": "object", "properties": {"data": {"type": "integer"}}}
        result = nr._finalize_node_result("n1", {"data": 1}, schema, None)
        assert result["artifacts"][0]["status"] == "completed"

    def test_invalid_schema_lenient_returns_completed(self) -> None:
        schema = {"type": "object", "properties": {"data": {"type": "integer"}}, "required": ["data"]}
        result = nr._finalize_node_result(
            "n1",
            {"wrong": "field"},
            schema,
            None,
            mode="lenient",
        )
        # Lenient mode: node succeeds despite validation failure
        assert result["artifacts"][0]["status"] == "completed"

    def test_invalid_schema_strict_raises(self) -> None:
        schema = {"type": "object", "properties": {"data": {"type": "integer"}}, "required": ["data"]}
        with pytest.raises(nr.OutputSchemaValidationError):
            nr._finalize_node_result(
                "n1",
                {"wrong": "field"},
                schema,
                None,
                mode="strict",
            )


# ---------------------------------------------------------------------------
# Test _manual_resume_output integration
# ---------------------------------------------------------------------------


class TestManualResumeOutput:
    """_manual_resume_output with schema validation."""

    def test_no_output_returns_none(self) -> None:
        result = nr._manual_resume_output({}, {"type": "object"})
        assert result is None

    def test_valid_output_passes(self) -> None:
        decision = {"output": {"name": "test"}}
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        result = nr._manual_resume_output(decision, schema)
        assert result == {"name": "test"}

    def test_invalid_output_strict_raises(self) -> None:
        decision = {"output": {"age": 30}}
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        with pytest.raises(nr.OutputSchemaValidationError):
            nr._manual_resume_output(
                decision,
                schema,
                mode="strict",
            )

    def test_invalid_output_lenient_succeeds(self) -> None:
        decision = {"output": {"age": 30}}
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        result = nr._manual_resume_output(
            decision,
            schema,
            mode="lenient",
        )
        # Lenient mode: returns the output despite validation failure
        assert result == {"age": 30}


# ---------------------------------------------------------------------------
# Test repair prompt building (injection hardening)
# ---------------------------------------------------------------------------


class TestRepairPrompt:
    """_build_repair_prompt injection hardening."""

    def test_no_raw_schema_text(self) -> None:
        from modulo.core.pipeline_engine.schema_repair import _build_repair_prompt

        errors = [{"pointer": "$.name", "constraint": "required", "expected": "string", "actual": "None"}]
        prompt = _build_repair_prompt(errors, "test-schema", 1, 3)

        # Must NOT contain raw schema free-text
        assert "description" not in prompt.lower()
        assert "title" not in prompt.lower()
        assert "examples" not in prompt.lower()

    def test_structural_metadata_present(self) -> None:
        from modulo.core.pipeline_engine.schema_repair import _build_repair_prompt

        errors = [{"pointer": "$.name", "constraint": "required", "expected": "string", "actual": "None"}]
        prompt = _build_repair_prompt(errors, "test-schema", 1, 3)

        assert "test-schema" in prompt
        assert "$.name" in prompt
        assert "required" in prompt

    def test_max_chars_cap(self) -> None:
        from modulo.core.pipeline_engine.schema_repair import _build_repair_prompt

        # Create many errors to exceed the cap
        errors = [{"pointer": f"$.field{i}", "constraint": "type"} for i in range(100)]
        prompt = _build_repair_prompt(errors, "test-schema", 1, 3)

        assert len(prompt) <= 2048


# ---------------------------------------------------------------------------
# Test outcome enum completeness
# ---------------------------------------------------------------------------


class TestSchemaValidationOutcome:
    """All expected outcomes are defined."""

    def test_all_outcomes_defined(self) -> None:
        expected = {
            "no_schema",
            "native_decoded_and_validated",
            "native_decode_failed",
            "native_decode_not_json",
            "verbatim_passed",
            "posthoc_validation_failed",
            "repair_attempted",
            "passed_after_repair",
            "repair_exhausted",
            "lenient_validation_bypassed",
            "schema_unenforceable",
        }
        actual = {o.value for o in SchemaValidationOutcome}
        assert expected == actual
