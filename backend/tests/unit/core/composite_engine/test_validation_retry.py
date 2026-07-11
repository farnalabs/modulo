"""Tests for composite output validation with retry."""

import uuid

import pytest

from modulo.core.composite_engine.composite_binding import (
    CompositeValidationError,
    EvalDefinitionConfig,
    OutputValidation,
    ValidationResult,
)
from modulo.core.composite_engine.expander import (
    execute_composite_with_retry,
    run_output_validation,
)


def _template(nodes: list[dict] | None = None) -> dict:
    return {
        "nodes": nodes or [{"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": "Hello"}],
        "edges": [],
    }


def _node_def() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
    }


class TestRunOutputValidation:
    def test_no_evals_passes(self) -> None:
        ov = OutputValidation()
        result = run_output_validation({"key": "value"}, ov)
        assert result.passed
        assert result.failures == []

    def test_regex_match_passes(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="check_positive",
                    type="regex",
                    config={"field": "score", "pattern": r"\d+"},
                ),
            ],
        )
        result = run_output_validation({"score": "42"}, ov)
        assert result.passed
        assert result.failures == []

    def test_regex_no_match_fails(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="check_positive",
                    type="regex",
                    config={"field": "score", "pattern": r"\d+"},
                ),
            ],
        )
        result = run_output_validation({"score": "abc"}, ov)
        assert not result.passed
        assert len(result.failures) == 1
        assert "check_positive" in result.failures[0]

    def test_regex_missing_field_fails(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="field_check",
                    type="regex",
                    config={"field": "missing", "pattern": r".+"},
                ),
            ],
        )
        result = run_output_validation({"other": "data"}, ov)
        assert not result.passed

    def test_json_schema_valid_passes(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="schema_check",
                    type="json_schema",
                    config={
                        "schema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    },
                ),
            ],
        )
        result = run_output_validation({"name": "Alice"}, ov)
        assert result.passed

    def test_json_schema_invalid_fails(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="schema_check",
                    type="json_schema",
                    config={
                        "schema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    },
                ),
            ],
        )
        result = run_output_validation({"name": 42}, ov)
        assert not result.passed
        assert len(result.failures) == 1

    def test_json_schema_field_scope(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="field_schema",
                    type="json_schema",
                    config={
                        "field": "payload",
                        "schema": {"type": "object", "properties": {"x": {"type": "number"}}},
                    },
                ),
            ],
        )
        result = run_output_validation({"payload": {"x": 1}}, ov)
        assert result.passed

    def test_llm_judge_passes(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="judge",
                    type="llm_judge",
                    config={"rubric": "check quality"},
                ),
            ],
        )
        result = run_output_validation(
            {"text": "good"},
            ov,
            llm_judge_callable=lambda output, config: {"passed": True, "detail": "ok"},
        )
        assert result.passed

    def test_llm_judge_fails(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="judge",
                    type="llm_judge",
                    config={"rubric": "check quality"},
                ),
            ],
        )
        result = run_output_validation(
            {"text": "bad"},
            ov,
            llm_judge_callable=lambda output, config: {"passed": False, "detail": "low quality"},
        )
        assert not result.passed
        assert "low quality" in result.failures[0]

    def test_llm_judge_no_callable_fails(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="judge",
                    type="llm_judge",
                    config={"rubric": "check"},
                ),
            ],
        )
        result = run_output_validation({"text": "test"}, ov)
        assert not result.passed

    def test_unknown_eval_type_raises(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig.model_construct(
                    id="e1",
                    name="bad",
                    type="unknown",
                ),
            ],
        )
        with pytest.raises(ValueError, match="unknown"):
            run_output_validation({"x": "y"}, ov)


class TestExecuteCompositeWithRetry:
    async def test_no_validation_passthrough(self) -> None:
        template = _template()
        node_def = _node_def()
        result = await execute_composite_with_retry(node_def, template, parameter_values={}, input_payload={"a": 1})
        assert result == {"a": 1}

    async def test_validation_passes_no_retry(self) -> None:
        template = _template()
        node_def = _node_def()
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="check",
                    type="regex",
                    config={"field": "status", "pattern": "ok"},
                ),
            ],
        )
        result = await execute_composite_with_retry(
            node_def,
            template,
            parameter_values={},
            input_payload={"status": "ok"},
            output_validation=ov,
        )
        assert result == {"status": "ok"}

    async def test_retry_on_failure_succeeds_after_retry(self) -> None:
        call_count = 0

        import modulo.core.composite_engine.expander as expander_mod

        def fake_expand(node_def, template, params):
            nonlocal call_count
            call_count += 1
            return [{"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": "Hello"}]

        original_expand = expander_mod.expand_composite_node
        expander_mod.expand_composite_node = fake_expand

        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="check",
                    type="regex",
                    config={"field": "status", "pattern": "ok"},
                    failure_behaviour="retry",
                ),
            ],
            max_validation_retries=2,
        )

        results = [
            ValidationResult(passed=False, failures=["Eval 'check': regex /ok/ did not match field 'status'"]),
            ValidationResult(passed=False, failures=["Eval 'check': regex /ok/ did not match field 'status'"]),
            ValidationResult(passed=True),
        ]

        original_validate = expander_mod.run_output_validation
        expander_mod.run_output_validation = lambda mo, ov, ljc=None: results.pop(0)

        try:
            result = await execute_composite_with_retry(
                _node_def(),
                _template(),
                parameter_values={},
                input_payload={"status": "fail"},
                output_validation=ov,
            )
            assert call_count == 3
            assert result == {"status": "fail"}
        finally:
            expander_mod.expand_composite_node = original_expand
            expander_mod.run_output_validation = original_validate

    async def test_retry_budget_exhausted_raises(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="check",
                    type="regex",
                    config={"field": "status", "pattern": "ok"},
                    failure_behaviour="retry",
                ),
            ],
            max_validation_retries=1,
        )

        import modulo.core.composite_engine.expander as expander_mod

        original_expand = expander_mod.expand_composite_node
        expander_mod.expand_composite_node = lambda nd, ct, pv: [
            {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": "Hello"},
        ]

        original_validate = expander_mod.run_output_validation
        expander_mod.run_output_validation = lambda mo, ov, ljc=None: ValidationResult(
            passed=False,
            failures=["Eval 'check': regex /ok/ did not match field 'status'"],
        )

        try:
            with pytest.raises(CompositeValidationError) as exc_info:
                await execute_composite_with_retry(
                    _node_def(),
                    _template(),
                    parameter_values={},
                    input_payload={"status": "fail"},
                    output_validation=ov,
                )
            assert exc_info.value.retry_count == 1
        finally:
            expander_mod.expand_composite_node = original_expand
            expander_mod.run_output_validation = original_validate

    async def test_block_behaviour_immediate_failure(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="block_check",
                    type="regex",
                    config={"field": "status", "pattern": "ok"},
                    failure_behaviour="block",
                ),
            ],
            max_validation_retries=3,
        )

        import modulo.core.composite_engine.expander as expander_mod

        original_validate = expander_mod.run_output_validation
        expander_mod.run_output_validation = lambda mo, ov, ljc=None: ValidationResult(
            passed=False,
            failures=["Eval 'block_check': regex /ok/ did not match field 'status'"],
        )

        try:
            with pytest.raises(CompositeValidationError) as exc_info:
                await execute_composite_with_retry(
                    _node_def(),
                    _template(),
                    parameter_values={},
                    input_payload={"status": "fail"},
                    output_validation=ov,
                )
            assert exc_info.value.retry_count == 0
        finally:
            expander_mod.run_output_validation = original_validate

    async def test_warn_behaviour_does_not_retry(self) -> None:
        ov = OutputValidation(
            eval_definitions=[
                EvalDefinitionConfig(
                    id="e1",
                    name="warn_check",
                    type="regex",
                    config={"field": "status", "pattern": "ok"},
                    failure_behaviour="warn",
                ),
            ],
            max_validation_retries=3,
        )

        import modulo.core.composite_engine.expander as expander_mod

        original_validate = expander_mod.run_output_validation
        expander_mod.run_output_validation = lambda mo, ov, ljc=None: ValidationResult(
            passed=False,
            failures=["Eval 'warn_check': regex /ok/ did not match field 'status'"],
        )

        try:
            result = await execute_composite_with_retry(
                _node_def(),
                _template(),
                parameter_values={},
                input_payload={"status": "fail"},
                output_validation=ov,
            )
            assert result == {"status": "fail"}
        finally:
            expander_mod.run_output_validation = original_validate
