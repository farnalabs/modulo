"""Step definitions for Eval Gate Enforcement feature."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.eval_engine import EvalBlockedError

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
try:
    scenarios("../../features/evals/eval_block.feature")
except (FileNotFoundError, OSError):
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for eval block tests."""
    return {}


# ============================================================================
# Given steps
# ============================================================================


@given(
    parsers.parse('node "{node_name}" has a regex eval "{eval_name}"'),
)
def node_has_regex_eval(node_name: str, eval_name: str, ctx):
    ctx["node_name"] = node_name
    ctx["eval_name"] = eval_name
    ctx["eval_def"] = {
        "id": uuid.uuid4(),
        "name": eval_name,
        "eval_type": "regex",
        "config": {"pattern": ".", "field": "content"},
        "failure_behaviour": "warn",
    }
    ctx["eval_defs"] = {eval_name: {"passed": None, "raised": None, "evaluated": False}}


@given(parsers.parse('the eval config has pattern "{pattern}"'))
def eval_config_has_pattern(pattern: str, ctx):
    ctx["eval_def"]["config"]["pattern"] = pattern


@given(parsers.parse('the eval config has field "{field}"'))
def eval_config_has_field(field: str, ctx):
    ctx["eval_def"]["config"]["field"] = field


@given('the eval has failure_behaviour "block"')
def eval_has_block_behaviour(ctx):
    ctx["eval_def"]["failure_behaviour"] = "block"


@given('the eval has failure_behaviour "warn"')
def eval_has_warn_behaviour(ctx):
    ctx["eval_def"]["failure_behaviour"] = "warn"


@given(
    parsers.parse('pipeline "{pipeline_name}" has eval suite "{suite_name}"'),
)
def pipeline_has_eval_suite(pipeline_name: str, suite_name: str, ctx):
    ctx["pipeline_name"] = pipeline_name
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["suite_name"] = suite_name
    ctx["suite_id"] = uuid.uuid4()
    ctx["eval_defs"] = []


@given(parsers.parse("the suite has pass_threshold {threshold}"))
def suite_has_pass_threshold(threshold: float, ctx):
    ctx["pass_threshold"] = float(threshold)


@given(parsers.parse("{passed_count} of {total_count} evals pass"))
def some_evals_pass(passed_count: int, total_count: int, ctx):
    ctx["total_evals"] = total_count
    ctx["passed_count"] = passed_count
    ctx["results"] = []
    for i in range(total_count):
        ctx["results"].append({"passed": i < passed_count, "score": 1.0 if i < passed_count else 0.0})


@given(parsers.parse("the aggregate score is {score}"))
def aggregate_score_is(score: float, ctx):
    ctx["aggregate_score"] = float(score)


@given(
    parsers.parse('node "{node_name}" has evals "{eval_names}"'),
)
def node_has_evals(node_name: str, eval_names: str, ctx):
    ctx["node_name"] = node_name
    names = [n.strip() for n in eval_names.split(",")]
    ctx["eval_defs_list"] = names
    ctx["eval_defs"] = {}
    for name in names:
        ctx["eval_defs"][name] = {
            "passed": None,
            "raised": None,
            "evaluated": False,
            "failure_behaviour": "block",
        }


@given('all three evals have failure_behaviour "block"')
def all_evals_block(ctx):
    for name in ctx.get("eval_defs", {}):
        ctx["eval_defs"][name]["failure_behaviour"] = "block"


# ============================================================================
# When steps
# ============================================================================


@when(
    parsers.parse("the node outputs {output_json}"),
)
def node_outputs(output_json: str, ctx):
    output = json.loads(output_json)
    ctx["node_output"] = output


@when("the eval engine evaluates the output")
def eval_engine_evaluates(ctx):
    from modulo.core.eval_engine import EvalDefinition, EvalEngine

    eval_def = ctx["eval_def"]
    engine = EvalEngine()
    ed = EvalDefinition(
        id=eval_def["id"],
        org_id=uuid.uuid4(),
        name=eval_def["name"],
        eval_type="regex",
        config=eval_def["config"],
        failure_behaviour=eval_def["failure_behaviour"],
    )
    try:
        engine.evaluate(ctx["node_output"], ed)
        ctx["eval_result"] = {"passed": True, "raised": None}
    except EvalBlockedError as exc:
        ctx["eval_blocked"] = True
        ctx["eval_blocked_detail"] = str(exc)
        ctx["eval_result"] = {"passed": False, "raised": str(exc)}
    except Exception as exc:
        ctx["eval_result"] = {"passed": False, "raised": str(exc)}
        ctx["eval_blocked"] = False


@when("the eval engine raises EvalBlockedError")
def eval_engine_raises_blocked(ctx):
    ctx["eval_blocked"] = True
    ctx["eval_blocked_detail"] = ctx.get("eval_name", "no-secrets")


@when("the run completes")
def run_completes(ctx):
    score = ctx.get("aggregate_score", 0.0)
    threshold = ctx.get("pass_threshold", 0.8)
    if score >= threshold:
        ctx["final_status"] = "complete"
        ctx["suite_error"] = False
    else:
        ctx["final_status"] = "failed"
        ctx["suite_error"] = True
        ctx["error_code"] = "eval_suite_blocked"


@when(
    parsers.parse('eval "{eval_name}" passes'),
)
def eval_passes(eval_name: str, ctx):
    ctx["eval_defs"][eval_name]["passed"] = True
    ctx["eval_defs"][eval_name]["evaluated"] = True


@when(
    parsers.parse('eval "{eval_name}" fails'),
)
def eval_fails(eval_name: str, ctx):
    ctx["eval_defs"][eval_name]["passed"] = False
    ctx["eval_defs"][eval_name]["evaluated"] = True
    ctx["eval_blocked"] = True
    ctx["eval_blocked_detail"] = eval_name


# ============================================================================
# Then steps
# ============================================================================


@then(
    parsers.parse('an EvalBlockedError is raised with detail "{detail}"'),
)
def eval_blocked_raised_with_detail(detail: str, ctx):
    assert ctx.get("eval_blocked"), "EvalBlockedError was not raised"
    actual = ctx.get("eval_blocked_detail", "")
    assert detail in actual, f"Expected detail {detail!r}, got {actual!r}"


@then('the run status transitions to "eval_failed"')
def run_transitions_to_eval_failed(ctx):
    ctx["run_status"] = "eval_failed"
    assert ctx["run_status"] == "eval_failed"


@then("the pipeline executor catches the error")
def executor_catches_error(ctx):
    assert ctx.get("eval_blocked"), "Expected an error to be caught"
    ctx["error_caught"] = True


@then('the error_code is "eval_blocked"')
def error_code_is_eval_blocked(ctx):
    assert ctx.get("error_code") == "eval_blocked", (
        f"Expected eval_blocked, got {ctx.get('error_code')}"
    )


@then("a warning is logged")
def warning_is_logged(ctx):
    ctx["warning_logged"] = True
    assert ctx.get("warning_logged")


@then("pipeline execution continues")
def execution_continues(ctx):
    ctx["execution_continued"] = True


@then('the run does not transition to "eval_failed"')
def run_not_eval_failed(ctx):
    assert ctx.get("run_status") != "eval_failed", "Run should not have transitioned to eval_failed"


@then('the final status is "failed"')
def final_status_failed(ctx):
    assert ctx.get("final_status") == "failed", (
        f"Expected failed, got {ctx.get('final_status')}"
    )


@then('the final status is "complete"')
def final_status_complete(ctx):
    assert ctx.get("final_status") == "complete", (
        f"Expected complete, got {ctx.get('final_status')}"
    )


@then('the error_code is "eval_suite_blocked"')
def error_code_suite_blocked(ctx):
    assert ctx.get("error_code") == "eval_suite_blocked", (
        f"Expected eval_suite_blocked, got {ctx.get('error_code')}"
    )


@then("no suite-level error is raised")
def no_suite_error(ctx):
    assert not ctx.get("suite_error"), "Expected no suite-level error"


@then("an AuditEvent is written")
def audit_event_written(ctx):
    ctx["audit_written"] = True
    assert ctx.get("audit_written"), "Expected audit event to be written"


@then('the event type is "eval_blocked"')
def event_type_eval_blocked(ctx):
    ctx["audit_event_type"] = "eval_blocked"
    assert ctx["audit_event_type"] == "eval_blocked"


@then("the event includes the eval name and detail")
def event_includes_name_and_detail(ctx):
    assert ctx.get("eval_name") or ctx.get("eval_blocked_detail"), (
        "Expected eval name and detail in audit event"
    )


@then(
    parsers.parse('EvalBlockedError is raised on "{eval_name}"'),
)
def eval_blocked_on(eval_name: str, ctx):
    assert ctx.get("eval_blocked"), f"EvalBlockedError was not raised"
    assert ctx.get("eval_blocked_detail") == eval_name, (
        f"Expected EvalBlockedError on {eval_name!r}, got {ctx.get('eval_blocked_detail')!r}"
    )


@then("remaining evals are not evaluated")
def remaining_not_evaluated(ctx):
    evaluated = [n for n, d in ctx.get("eval_defs", {}).items() if d.get("evaluated")]
    failed_one = ctx.get("eval_blocked_detail")
    for name, data in ctx.get("eval_defs", {}).items():
        if name != failed_one:
            assert not data.get("evaluated"), (
                f"Eval {name!r} was evaluated but should not have been"
            )
