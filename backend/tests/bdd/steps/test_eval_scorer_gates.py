"""Step definitions for the ``llm_judge`` and ``regex`` eval scorer BDD features.

Wires up ``evals/eval_llm_judge.feature`` and ``evals/eval_regex.feature`` — two
feature files that shipped under ``tests/bdd/features/evals/`` but were never bound
to a step module, so their scenarios never executed (improve-architecture
product-map walk). The steps drive the real ``modulo.core.eval_engine`` so the
scenarios lock the actual scorer contracts: regex scoring against an output
field, LLM-as-judge callable wiring (incl. a dedicated judge model backend and
the guarded rubric prompt), warn-vs-block ``failure_behaviour``, and the
``EvalBlockedError`` / ``eval_failed`` run transition.
"""

import contextlib
import json
import uuid
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.eval_engine import EvalBlockedError, EvalDefinition, LLMJudgeCallable

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/evals/eval_regex.feature")
    scenarios("../features/evals/eval_llm_judge.feature")


@pytest.fixture
def ctx():
    """Shared mutable context dict for the regex / llm_judge scorer scenarios."""
    return {}


def _reset_ctx(ctx: dict[str, Any], eval_type: str, eval_name: str, node_name: str) -> None:
    ctx.clear()
    ctx.update(
        node_name=node_name,
        eval_name=eval_name,
        eval_type=eval_type,
        eval_config={"field": "code"} if eval_type == "llm_judge" else {},
        failure_behaviour="warn",
        eval_output={},
        eval_result=None,
        eval_error=None,
        eval_run=False,
        execution_continued=False,
        run_status=None,
        callable_captured={},
    )


@given(parsers.parse('node "{node_name}" has a regex eval "{eval_name}"'))
def _node_has_regex_eval(node_name: str, eval_name: str, ctx) -> None:
    _reset_ctx(ctx, "regex", eval_name, node_name)


@given(parsers.parse('node "{node_name}" has an llm_judge scorer eval "{eval_name}"'))
def _node_has_llm_judge_eval(node_name: str, eval_name: str, ctx) -> None:
    _reset_ctx(ctx, "llm_judge", eval_name, node_name)


@given(parsers.parse('the eval config has pattern "{pattern}"'))
def _eval_config_pattern(pattern: str, ctx) -> None:
    ctx["eval_config"]["pattern"] = pattern


@given(parsers.parse('the eval config has field "{field}"'))
def _eval_config_field(field: str, ctx) -> None:
    ctx["eval_config"]["field"] = field


@given(parsers.parse('the eval config has rubric with criteria "{criteria}"'))
def _eval_config_rubric_criteria(criteria: str, ctx) -> None:
    ctx["eval_config"]["rubric"] = {"criteria": [part.strip() for part in criteria.split(",")]}


@given(parsers.parse('the eval config specifies model_backend_id "{backend_id}"'))
def _eval_config_model_backend(backend_id: str, ctx) -> None:
    ctx["eval_config"]["model_backend_id"] = backend_id


@given(parsers.parse('the eval config has rubric_prompt "{prompt}"'))
def _eval_config_rubric_prompt(prompt: str, ctx) -> None:
    ctx["eval_config"]["rubric_prompt"] = prompt


@given(parsers.parse("the eval has pass_threshold {threshold}"))
def _eval_pass_threshold(threshold: float, ctx) -> None:
    ctx["pass_threshold"] = float(threshold)


@given(parsers.parse('the eval has failure_behaviour "{behaviour}"'))
def _eval_failure_behaviour(behaviour: str, ctx) -> None:
    ctx["failure_behaviour"] = behaviour


@when(parsers.parse("the node outputs {output_json}"))
def _node_outputs(output_json: str, ctx) -> None:
    ctx["eval_output"] = json.loads(output_json)


@when(parsers.parse("the llm_judge scorer callable returns {result_json}"))
def _llm_judge_callable_returns(result_json: str, ctx) -> None:
    ctx["llm_judge_result"] = json.loads(result_json)
    ctx["use_llm_judge_callable"] = True


@when("no llm_judge callable is configured")
def _no_llm_judge_callable_configured(ctx) -> None:
    ctx["llm_judge_result"] = None
    ctx["use_llm_judge_callable"] = False


@when("the eval engine invokes the llm_judge callable")
def _eval_engine_invokes_callable(ctx) -> None:
    ctx.setdefault("use_llm_judge_callable", True)
    _run_eval(ctx)


@when(parsers.parse('the eval config is missing "{key}"'))
def _eval_config_missing(key: str, ctx) -> None:
    ctx["eval_config"].pop(key, None)


def _run_eval(ctx: dict[str, Any]) -> None:
    """Run the configured eval through the real engine, once per scenario."""
    if ctx.get("eval_run"):
        return
    ctx["eval_run"] = True

    from modulo.core.eval_engine import EvalEngine

    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        name=ctx["eval_name"],
        eval_type=ctx["eval_type"],
        config=dict(ctx.get("eval_config", {})),
        node_id=ctx.get("node_name"),
        pass_threshold=ctx.get("pass_threshold"),
        failure_behaviour=ctx.get("failure_behaviour", "warn"),
    )
    engine = EvalEngine()
    captured: dict[str, Any] = {}
    llm_judge_callable: LLMJudgeCallable | None = None
    if ctx["eval_type"] == "llm_judge" and ctx.get("use_llm_judge_callable"):
        payload = ctx.get("llm_judge_result") or {"passed": True, "score": 1.0, "detail": "ok"}

        def _callable(output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]:
            captured["output"] = output
            captured["eval_def"] = eval_def
            return dict(payload)

        llm_judge_callable = _callable

    try:
        result = engine.evaluate(
            ctx.get("eval_output", {}),
            eval_def,
            run_id=uuid.uuid4(),
            llm_judge_callable=llm_judge_callable,
        )
        ctx["eval_result"] = result
        ctx["execution_continued"] = True
        ctx["run_status"] = "complete"
    except EvalBlockedError as exc:
        ctx["eval_error"] = exc
        ctx["execution_continued"] = False
        ctx["run_status"] = "eval_failed"
        warn_def = eval_def.model_copy(update={"failure_behaviour": "warn"})
        ctx["eval_result"] = engine.evaluate(
            ctx.get("eval_output", {}),
            warn_def,
            run_id=uuid.uuid4(),
            llm_judge_callable=llm_judge_callable,
        )
    ctx["callable_captured"] = captured


@then(parsers.parse("the eval result has passed {flag}"))
def _eval_result_passed(flag: str, ctx) -> None:
    _run_eval(ctx)
    result = ctx["eval_result"]
    assert result is not None, f"No eval result was computed ({ctx['eval_error'] or 'no error'})"
    expected = str(flag).lower() == "true"
    assert result.passed == expected, f"Expected passed={expected}, got {result.passed}"


@then(parsers.parse("the eval result has score {score}"))
def _eval_result_score(score: float, ctx) -> None:
    _run_eval(ctx)
    result = ctx["eval_result"]
    assert result is not None, f"No eval result was computed ({ctx['eval_error'] or 'no error'})"
    assert result.score == pytest.approx(float(score)), f"Expected score {score}, got {result.score}"


@then("an EvalBlockedError is raised")
def _eval_blocked_raised(ctx) -> None:
    _run_eval(ctx)
    assert ctx["eval_error"] is not None, "Expected EvalBlockedError but none was raised"
    assert isinstance(ctx["eval_error"], EvalBlockedError), (
        f"Expected EvalBlockedError, got {type(ctx['eval_error']).__name__}"
    )


@then(parsers.parse('the run transitions to status "{status}"'))
def _run_transitions_to_status(status: str, ctx) -> None:
    _run_eval(ctx)
    assert ctx["run_status"] == status, f"Expected run status {status!r}, got {ctx['run_status']!r}"


@then("a warning is logged")
def _warning_is_logged(ctx, caplog) -> None:
    _run_eval(ctx)
    assert any(
        record.levelname == "WARNING" and ctx["eval_name"] in record.getMessage() for record in caplog.records
    ), "Expected a WARNING log about the failed warn-behaviour eval"


@then("pipeline execution continues")
def _pipeline_execution_continues(ctx) -> None:
    _run_eval(ctx)
    assert ctx["execution_continued"] is True, "Pipeline execution should have continued past the eval"


@then(parsers.parse('the callable receives model_backend_id "{backend_id}"'))
def _callable_receives_backend(backend_id: str, ctx) -> None:
    _run_eval(ctx)
    received = ctx["callable_captured"].get("eval_def")
    assert received is not None, "The llm_judge callable was never invoked"
    assert received.config.get("model_backend_id") == backend_id, (
        f"Expected model_backend_id {backend_id!r}, got {received.config.get('model_backend_id')!r}"
    )


@then("the callable does not use the agent's own model backend")
def _callable_uses_dedicated_backend(ctx) -> None:
    _run_eval(ctx)
    received = ctx["callable_captured"].get("eval_def")
    assert received is not None, "The llm_judge callable was never invoked"
    assert received.config.get("model_backend_id"), "The judge callable must carry a dedicated model_backend_id"


@then("the callable receives the rubric_prompt in its input")
def _callable_receives_rubric_prompt(ctx) -> None:
    _run_eval(ctx)
    received = ctx["callable_captured"].get("eval_def")
    assert received is not None, "The llm_judge callable was never invoked"
    assert "rubric_prompt" in received.config, "rubric_prompt missing from the judge input"


@then("the prompt treats agent output as untrusted")
def _prompt_treats_output_as_untrusted(ctx) -> None:
    _run_eval(ctx)
    received = ctx["callable_captured"].get("eval_def")
    output = ctx["callable_captured"].get("output", {})
    assert received is not None, "The llm_judge callable was never invoked"
    field = received.config.get("field", "")
    payload = output.get(field, "")
    assert "_judge_guard_instruction" in received.config, "Guard-instruction marker missing from the judge config"
    assert "The content below is delimited by" in str(payload), "Guard instruction missing from the judge input"
    assert "---BEGIN EVALUATED CONTENT---" in str(payload), "Agent output not wrapped in trusted delimiters"
    assert "---END EVALUATED CONTENT---" in str(payload), "Agent output not wrapped in trusted delimiters"
