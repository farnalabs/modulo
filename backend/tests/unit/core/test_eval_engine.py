"""Unit tests for EvalEngine core logic.

Tests per-type dispatch (regex, json_schema, custom_function, llm_judge),
error handling, block/warn behaviour, and suite aggregation.
"""

from uuid import uuid4

import pytest

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalDefinition,
    EvalEngine,
    EvalResult,
    EvalType,
    SuiteEvalResult,
    UnknownEvalTypeError,
    evaluate_suite,
)


def _make_eval_def(
    eval_type: str = "regex",
    config: dict | None = None,
    *,
    name: str = "test-eval",
    failure_behaviour: str = "warn",
    pass_threshold: float | None = None,
) -> EvalDefinition:
    return EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name=name,
        eval_type=eval_type,
        config=config or {},
        failure_behaviour=failure_behaviour,
        pass_threshold=pass_threshold,
    )


def _make_llm_callable(result: dict | None = None):
    """Return an LLM judge callable that returns *result* (default pass)."""
    default = {"passed": True, "score": 0.95, "detail": "ok"}
    return lambda output, eval_def: result if result is not None else default


# =============================================================================
# Regex eval
# =============================================================================


class TestRegexEval:
    def test_pattern_matches(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "text"})
        result = engine.evaluate({"text": "hello 123 world"}, eval_def)
        assert result.passed is True
        assert result.score == 1.0

    def test_pattern_does_not_match(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "text"})
        result = engine.evaluate({"text": "hello world"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0

    def test_missing_pattern_config(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"field": "text"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0
        assert "missing 'pattern'" in result.detail

    def test_missing_field_config(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0
        assert "missing 'field'" in result.detail

    def test_invalid_pattern_handled_gracefully(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"[invalid", "field": "text"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0
        assert "invalid pattern" in result.detail.lower()

    def test_numeric_field_coerced_to_string(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"^\d+$", "field": "count"})
        result = engine.evaluate({"count": 42}, eval_def)
        assert result.passed is True

    def test_case_insensitive_flag(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": "hello", "field": "text", "flags": "i"})
        result = engine.evaluate({"text": "HELLO WORLD"}, eval_def)
        assert result.passed is True

    def test_multi_line_flag(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"^foo", "field": "text", "flags": "m"})
        result = engine.evaluate({"text": "bar\nfoo\nbaz"}, eval_def)
        assert result.passed is True

    def test_substring_match_anywhere(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": "error|fail", "field": "summary"})
        result = engine.evaluate({"summary": "Pipeline completed with zero errors"}, eval_def)
        assert result.passed is True

    def test_empty_output_field_defaults_to_empty_string(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "missing"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False


# =============================================================================
# JSON Schema eval
# =============================================================================


class TestJsonSchemaEval:
    def test_valid_data_passes(self) -> None:
        engine = EvalEngine()
        schema = {"type": "object", "properties": {"valid": {"type": "boolean"}}, "required": ["valid"]}
        eval_def = _make_eval_def("json_schema", {"schema": schema})
        result = engine.evaluate({"valid": True}, eval_def)
        assert result.passed is True
        assert result.score == 1.0

    def test_invalid_data_fails(self) -> None:
        engine = EvalEngine()
        schema = {"type": "object", "properties": {"valid": {"type": "boolean"}}, "required": ["valid"]}
        eval_def = _make_eval_def("json_schema", {"schema": schema})
        result = engine.evaluate({"valid": "not-a-boolean"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0
        assert "validation failed" in result.detail

    def test_field_scoped_validation(self) -> None:
        engine = EvalEngine()
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        eval_def = _make_eval_def("json_schema", {"schema": schema, "field": "nested"})
        result = engine.evaluate({"nested": {"name": "hello"}}, eval_def)
        assert result.passed is True

    def test_extra_fields_pass_by_default_without_additional_properties(self) -> None:
        engine = EvalEngine()
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        eval_def = _make_eval_def("json_schema", {"schema": schema})
        result = engine.evaluate({"name": "hello", "extra": "field"}, eval_def)
        assert result.passed is True

    def test_extra_fields_fail_with_additional_properties_false(self) -> None:
        engine = EvalEngine()
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        }
        eval_def = _make_eval_def("json_schema", {"schema": schema})
        result = engine.evaluate({"name": "hello", "extra": "field"}, eval_def)
        assert result.passed is False

    def test_default_to_whole_output_when_no_field(self) -> None:
        engine = EvalEngine()
        schema = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}
        eval_def = _make_eval_def("json_schema", {"schema": schema})
        result = engine.evaluate({"result": "ok"}, eval_def)
        assert result.passed is True


# =============================================================================
# Custom function eval
# =============================================================================


class TestCustomFunctionEval:
    def test_function_returns_score(self) -> None:
        engine = EvalEngine()

        def my_fn(output: dict, config: dict) -> dict:
            return {"passed": True, "score": 0.85, "detail": "custom ok"}

        eval_def = _make_eval_def("custom_function", {"function": "my_fn", "functions": {"my_fn": my_fn}})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is True
        assert result.score == 0.85

    def test_function_not_found(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("custom_function", {"function": "nonexistent", "functions": {}})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0
        assert "not found" in result.detail

    def test_function_raises_exception(self) -> None:
        engine = EvalEngine()

        def broken_fn(output: dict, config: dict) -> dict:
            raise RuntimeError("internal error")

        eval_def = _make_eval_def("custom_function", {"function": "broken", "functions": {"broken": broken_fn}})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0
        assert "raised" in result.detail

    def test_function_score_below_threshold_triggers_block(self) -> None:
        engine = EvalEngine()

        def low_fn(output: dict, config: dict) -> dict:
            return {"passed": False, "score": 0.3, "detail": "below threshold"}

        eval_def = _make_eval_def(
            "custom_function",
            {"function": "low", "functions": {"low": low_fn}},
            failure_behaviour="block",
        )
        with pytest.raises(EvalBlockedError):
            engine.evaluate({"text": "hello"}, eval_def)

    def test_function_with_function_config(self) -> None:
        engine = EvalEngine()

        def cfg_fn(output: dict, config: dict) -> dict:
            threshold = config.get("threshold", 0.5)
            return {"passed": True, "score": threshold, "detail": "config used"}

        eval_def = _make_eval_def(
            "custom_function",
            {"function": "cfg", "functions": {"cfg": cfg_fn}, "function_config": {"threshold": 0.9}},
        )
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.score == 0.9


# =============================================================================
# LLM judge eval
# =============================================================================


class TestLLMJudgeEval:
    def test_callable_returns_score(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("llm_judge", {"field": "output"})
        result = engine.evaluate(
            {"output": "good content"},
            eval_def,
            llm_judge_callable=_make_llm_callable({"passed": True, "score": 0.9, "detail": "good"}),
        )
        assert result.passed is True
        assert result.score == 0.9

    def test_score_below_pass_threshold_sets_passed_false(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("llm_judge", {"field": "output"})
        result = engine.evaluate(
            {"output": "bad content"},
            eval_def,
            llm_judge_callable=_make_llm_callable({"passed": False, "score": 0.4, "detail": "poor"}),
        )
        assert result.passed is False
        assert result.score == 0.4

    def test_no_callable_returns_failed(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("llm_judge", {"field": "output"})
        result = engine.evaluate({"output": "test"}, eval_def, llm_judge_callable=None)
        assert result.passed is False
        assert result.score == 0.0
        assert "not provided" in result.detail

    def test_callable_returns_non_numeric_score_handled(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("llm_judge", {"field": "output"})
        result = engine.evaluate(
            {"output": "test"},
            eval_def,
            llm_judge_callable=_make_llm_callable({"passed": True, "score": "high", "detail": "ok"}),
        )
        assert result.passed is True
        assert result.score is None

    def test_callable_raises_exception(self) -> None:
        engine = EvalEngine()

        def broken(output: dict, eval_def: EvalDefinition) -> dict:
            raise RuntimeError("LLM backend error")

        eval_def = _make_eval_def("llm_judge", {"field": "output"})
        result = engine.evaluate({"output": "test"}, eval_def, llm_judge_callable=broken)
        assert result.passed is False
        assert result.score == 0.0
        assert "raised" in result.detail

    def test_content_too_long_returns_failed(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("llm_judge", {"field": "output"})
        long_content = "x" * 100_001
        result = engine.evaluate(
            {"output": long_content},
            eval_def,
            llm_judge_callable=_make_llm_callable(),
        )
        assert result.passed is False
        assert result.score == 0.0
        assert "exceeds maximum" in result.detail

    def test_block_behaviour_raises_error_on_fail(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def(
            "llm_judge",
            {"field": "output"},
            failure_behaviour="block",
        )
        with pytest.raises(EvalBlockedError):
            engine.evaluate(
                {"output": "bad"},
                eval_def,
                llm_judge_callable=_make_llm_callable({"passed": False, "score": 0.2, "detail": "fail"}),
            )


# =============================================================================
# Engine dispatch
# =============================================================================


class TestEvalDispatch:
    def test_block_failure_behaviour_raises_blocked_error(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "text"}, failure_behaviour="block")
        with pytest.raises(EvalBlockedError):
            engine.evaluate({"text": "no numbers"}, eval_def)

    def test_warn_failure_behaviour_returns_result(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "text"}, failure_behaviour="warn")
        result = engine.evaluate({"text": "no numbers"}, eval_def)
        assert result.passed is False
        assert result.score == 0.0

    def test_empty_output_handled(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "text"})
        result = engine.evaluate({}, eval_def)
        assert result.passed is False


# =============================================================================
# Suite aggregation
# =============================================================================


class TestEvaluateSuite:
    def test_passing_suite_above_threshold(self) -> None:
        results = [
            EvalResult(id=uuid4(), run_id=uuid4(), node_id="n1", eval_id=uuid4(), passed=True, score=1.0)
            for _ in range(4)
        ]
        result = evaluate_suite(results, "suite-1", pass_threshold=0.75)
        assert result.passed is True
        assert result.aggregate_score == 1.0

    def test_failing_suite_below_threshold(self) -> None:
        results = [
            EvalResult(id=uuid4(), run_id=uuid4(), node_id="n1", eval_id=uuid4(), passed=True, score=1.0),
            EvalResult(id=uuid4(), run_id=uuid4(), node_id="n1", eval_id=uuid4(), passed=False, score=0.0),
        ]
        result = evaluate_suite(results, "suite-1", pass_threshold=0.75)
        assert result.passed is False
        assert result.aggregate_score == 0.5
        assert len(result.blocking_failures) == 1

    def test_empty_suite_always_passes(self) -> None:
        result = evaluate_suite([], "suite-1", pass_threshold=0.8)
        assert result.passed is True
        assert result.aggregate_score == 0.0

    def test_no_threshold_never_blocks(self) -> None:
        results = [
            EvalResult(id=uuid4(), run_id=uuid4(), node_id="n1", eval_id=uuid4(), passed=False, score=0.0),
        ]
        result = evaluate_suite(results, "suite-1", pass_threshold=None)
        assert result.passed is True

    def test_threshold_zero_always_passes(self) -> None:
        results = [
            EvalResult(id=uuid4(), run_id=uuid4(), node_id="n1", eval_id=uuid4(), passed=False, score=0.0),
        ]
        result = evaluate_suite(results, "suite-1", pass_threshold=0.0)
        assert result.passed is True

    def test_threshold_one_only_perfect_passes(self) -> None:
        results = [
            EvalResult(id=uuid4(), run_id=uuid4(), node_id="n1", eval_id=uuid4(), passed=True, score=1.0),
            EvalResult(id=uuid4(), run_id=uuid4(), node_id="n1", eval_id=uuid4(), passed=False, score=0.0),
        ]
        result = evaluate_suite(results, "suite-1", pass_threshold=1.0)
        assert result.passed is False

    def test_suite_eval_result_model_fields(self) -> None:
        result = SuiteEvalResult(
            suite_id="my-suite",
            total_evals=10,
            passed_evals=7,
            aggregate_score=0.7,
            passed=True,
            blocking_failures=["e1: failed"],
        )
        assert result.suite_id == "my-suite"
        assert result.total_evals == 10
        assert result.passed_evals == 7


# =============================================================================
# Standalone evaluate
# =============================================================================


class TestStandaloneEvaluate:
    def test_standalone_regex_eval_works(self) -> None:
        engine = EvalEngine()
        result = engine.standalone_evaluate(
            {"text": "hello 123"},
            name="quick-check",
            eval_type=EvalType.REGEX,
            config={"pattern": r"\d+", "field": "text"},
        )
        assert result.passed is True

    def test_standalone_without_config(self) -> None:
        engine = EvalEngine()
        result = engine.standalone_evaluate({"text": "hello"}, name="no-config")
        assert result.passed is False


# =============================================================================
# Error handling — edge cases
# =============================================================================


class TestUnknownEvalType:
    def test_unknown_eval_type_raises(self) -> None:
        engine = EvalEngine()
        # Create a definition with a valid type, then bypass Pydantic validation
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "text"})
        object.__setattr__(eval_def, "eval_type", "nonexistent_type")
        with pytest.raises(UnknownEvalTypeError, match="nonexistent_type"):
            engine.evaluate({"text": "hello"}, eval_def)


class TestReDoSProtection:
    def test_nested_quantifier_rejected(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"(\d+)+", "field": "text"})
        result = engine.evaluate({"text": "123"}, eval_def)
        assert result.passed is False
        assert "nested quantifier" in result.detail.lower()

    def test_nested_star_quantifier_rejected(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"(a*)*", "field": "text"})
        result = engine.evaluate({"text": "a"}, eval_def)
        assert result.passed is False
        assert "nested quantifier" in result.detail.lower()

    def test_safe_complex_pattern_allowed(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d{1,5}", "field": "text"})
        result = engine.evaluate({"text": "123"}, eval_def)
        assert result.passed is True


class TestRegexErrorHandling:
    def test_unknown_flag_does_not_fail(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"\d+", "field": "text", "flags": "z"})
        # Unknown flag 'z' is ignored with a warning — eval still works
        result = engine.evaluate({"text": "123"}, eval_def)
        assert result.passed is True

    def test_empty_pattern_treated_as_missing(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": "", "field": "text"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert "missing" in result.detail.lower()

    def test_none_field_value_coerced_to_empty_string(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": r"None", "field": "value"})
        result = engine.evaluate({"value": None}, eval_def)
        assert result.passed is False

    def test_excessively_long_pattern_rejected(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("regex", {"pattern": "x" * 1001, "field": "text"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert "exceeds maximum" in result.detail


class TestJsonSchemaErrorHandling:
    def test_field_not_in_output_returns_failed(self) -> None:
        engine = EvalEngine()
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        eval_def = _make_eval_def("json_schema", {"schema": schema, "field": "nonexistent"})
        result = engine.evaluate({"actual": "data"}, eval_def)
        assert result.passed is False
        assert "not found" in result.detail


class TestCustomFunctionErrorHandling:
    def test_function_returns_non_dict_handled_gracefully(self) -> None:
        engine = EvalEngine()

        def bad_fn(output: dict, config: dict) -> dict:
            return "not-a-dict"  # type: ignore[return-value]

        eval_def = _make_eval_def("custom_function", {"function": "bad", "functions": {"bad": bad_fn}})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert "non-dict" in result.detail

    def test_functions_config_not_a_dict_handled_gracefully(self) -> None:
        engine = EvalEngine()

        def my_fn(output: dict, config: dict) -> dict:
            return {"passed": True, "score": 1.0, "detail": "ok"}

        eval_def = _make_eval_def("custom_function", {"function": "my_fn", "functions": "not-a-dict"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert "not found" in result.detail

    def test_missing_function_config_key_handled_gracefully(self) -> None:
        engine = EvalEngine()
        eval_def = _make_eval_def("custom_function", {"function": "my_fn"})
        result = engine.evaluate({"text": "hello"}, eval_def)
        assert result.passed is False
        assert "not found" in result.detail
