"""Unit tests for the human_set eval type and human-authored eval sets.

Covers engine dispatch, the demo_classification v1 set, missing-set handling,
and that a broken assertion fails loudly (never silently passes).
"""

import json
from uuid import uuid4

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.human_eval_sets import (
    DEMO_CLASSIFICATION_V1,
    get_human_eval_set,
    register_human_eval_set,
    run_human_eval_set,
)


def _make_human_eval_def(config: dict | None = None) -> EvalDefinition:
    return EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="human-set-eval",
        eval_type=EvalType.HUMAN_SET,
        config=config or {"set_name": "demo_classification", "field": "output"},
    )


class TestHumanSetEngineDispatch:
    def test_missing_set_name_fails(self) -> None:
        engine = EvalEngine()
        eval_def = _make_human_eval_def({"field": "output"})
        result = engine.evaluate({"output": "anything"}, eval_def)
        assert result.passed is False
        assert "set_name" in result.detail

    def test_unknown_set_name_fails(self) -> None:
        engine = EvalEngine()
        eval_def = _make_human_eval_def({"set_name": "does_not_exist", "field": "output"})
        result = engine.evaluate({"output": "anything"}, eval_def)
        assert result.passed is False
        assert "not registered" in result.detail

    def test_valid_classification_passes(self) -> None:
        engine = EvalEngine()
        eval_def = _make_human_eval_def()
        output = {"output": json.dumps({"category": "technical", "priority": "high", "confidence": 0.9})}
        result = engine.evaluate(output, eval_def)
        assert result.passed is True
        assert result.score == 1.0
        assert "demo_classification@v1" in result.detail

    def test_consistency_rule_fails_billing_low(self) -> None:
        engine = EvalEngine()
        eval_def = _make_human_eval_def()
        output = {"output": json.dumps({"category": "billing", "priority": "low"})}
        result = engine.evaluate(output, eval_def)
        assert result.passed is False
        assert "consistency" in result.detail

    def test_invalid_json_fails(self) -> None:
        engine = EvalEngine()
        eval_def = _make_human_eval_def()
        result = engine.evaluate({"output": "not json"}, eval_def)
        assert result.passed is False
        assert "valid_json" in result.detail

    def test_non_object_json_fails(self) -> None:
        engine = EvalEngine()
        eval_def = _make_human_eval_def()
        result = engine.evaluate({"output": json.dumps([1, 2, 3])}, eval_def)
        assert result.passed is False

    def test_missing_required_key_fails(self) -> None:
        engine = EvalEngine()
        eval_def = _make_human_eval_def()
        result = engine.evaluate({"output": json.dumps({"category": "general"})}, eval_def)
        assert result.passed is False
        assert "required_keys" in result.detail

    def test_block_behaviour_raises(self) -> None:
        from modulo.core.eval_engine import EvalBlockedError

        engine = EvalEngine()
        eval_def = _make_human_eval_def()
        eval_def.failure_behaviour = "block"
        with pytest.raises(EvalBlockedError):
            engine.evaluate({"output": json.dumps({"category": "billing", "priority": "low"})}, eval_def)


class TestHumanEvalSetRegistry:
    def test_demo_set_registered(self) -> None:
        assert get_human_eval_set("demo_classification") is DEMO_CLASSIFICATION_V1
        assert DEMO_CLASSIFICATION_V1.version == "v1"
        assert len(DEMO_CLASSIFICATION_V1.assertions) >= 6

    def test_duplicate_registration_raises(self) -> None:
        from dataclasses import replace

        with pytest.raises(ValueError):
            register_human_eval_set(replace(DEMO_CLASSIFICATION_V1, version="v2"))

    def test_run_human_eval_set_missing_set(self) -> None:
        eval_def = _make_human_eval_def({"set_name": "nope"})
        result = run_human_eval_set("nope", {"output": "x"}, eval_def=eval_def)
        assert result.passed is False

    def test_broken_assertion_fails_loudly(self) -> None:
        """A broken assertion must fail, never silently pass."""

        def _boom(output, config):
            raise RuntimeError("assertion exploded")

        from modulo.core.eval_engine.human_eval_sets import HumanAssertion, HumanEvalSet

        broken = HumanEvalSet(
            name="broken_set_tmp",
            version="v1",
            description="test",
            assertions=[HumanAssertion("boom", _boom)],
        )
        register_human_eval_set(broken)
        try:
            eval_def = _make_human_eval_def({"set_name": "broken_set_tmp"})
            result = run_human_eval_set("broken_set_tmp", {"output": "x"}, eval_def=eval_def)
            assert result.passed is False
            assert "boom" in result.detail
        finally:
            from modulo.core.eval_engine.human_eval_sets import HUMAN_EVAL_SETS

            HUMAN_EVAL_SETS.pop("broken_set_tmp", None)
