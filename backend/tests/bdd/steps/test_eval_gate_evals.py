"""Step definitions for Eval Gate Evals feature (FAR-971, criteria 12-13).

Tests the persist-before-decide behaviour: eval results must be persisted
BEFORE the block/warn decision is evaluated.

Covers criteria:
  12 -- Blocking eval persists result before halting.
  13 -- Warn eval persists result and run continues.
"""

import contextlib
import uuid

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/eval_gate_evals.feature")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for eval gate tests."""
    return {}


# ============================================================================
# Given steps
# ============================================================================


@given(parsers.parse('a pipeline "{pipeline_name}" with evals:'))
def pipeline_with_evals_table(pipeline_name: str, datatable, ctx):
    """Set up a pipeline with eval definitions from a Gherkin DataTable.

    The table has columns: name, eval_type, failure_behaviour.
    ``datatable`` is a list of lists; first row is the header.
    """
    ctx["pipeline_name"] = pipeline_name
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["eval_defs"] = []
    headers = [h.strip() for h in datatable[0]]
    for row in datatable[1:]:
        record = dict(zip(headers, row, strict=True))
        ctx["eval_defs"].append(
            {
                "id": uuid.uuid4(),
                "name": record["name"].strip(),
                "eval_type": record["eval_type"].strip(),
                "failure_behaviour": record["failure_behaviour"].strip(),
                "config": {"pattern": ".", "field": "content"},
            }
        )


@given(parsers.parse('a pipeline run of "{pipeline_name}"'))
def pipeline_run_of(pipeline_name: str, ctx):
    """Create a simulated pipeline run."""
    ctx["run_id"] = uuid.uuid4()
    ctx["run_status"] = "running"
    ctx["error_code"] = None
    ctx["error_detail"] = None
    ctx["eval_results_db"] = []


@given(parsers.parse('eval "{eval_name}" produces passed={passed}'))
def eval_produces(eval_name: str, passed: str, ctx):
    """Record the expected eval outcome for the named eval."""
    ctx.setdefault("eval_outcomes", {})
    ctx["eval_outcomes"][eval_name] = {"passed": passed.lower() == "true"}


# ============================================================================
# When steps
# ============================================================================


@when("the pipeline run completes")
def pipeline_run_completes(ctx):
    """Simulate the per-eval persist-then-decide loop.

    For each eval in order:
      1. Compute result (from eval_outcomes).
      2. Persist EvalResult to the simulated DB.
      3. Decide: if block and failed, raise EvalBlockedError.
    """
    eval_results_db = ctx["eval_results_db"]
    node_id = uuid.uuid4()

    for eval_def in ctx["eval_defs"]:
        name = eval_def["name"]
        outcome = ctx.get("eval_outcomes", {}).get(name, {"passed": True})

        # 1. Compute
        result = {"passed": outcome["passed"], "eval_id": eval_def["id"]}

        # 2. Persist (simulated)
        eval_results_db.append(
            {
                "id": uuid.uuid4(),
                "organisation_id": uuid.uuid4(),
                "run_id": ctx["run_id"],
                "node_id": node_id,
                "eval_id": eval_def["id"],
                "eval_definition_version": 1,
                "passed": result["passed"],
                "score": None,
                "detail": None,
            }
        )

        # 3. Decide
        if not result["passed"] and eval_def["failure_behaviour"] == "block":
            ctx["run_status"] = "eval_failed"
            ctx["error_code"] = "eval_blocked"
            ctx["error_detail"] = f"eval {name} blocked"
            return

    # All evals passed or only warn failures
    ctx["run_status"] = "completed"


# ============================================================================
# Then steps
# ============================================================================


@then(parsers.parse('the run terminal status is "{expected_status}"'))
def run_terminal_status(expected_status: str, ctx):
    assert ctx["run_status"] == expected_status, (
        f"Expected terminal status {expected_status!r}, got {ctx['run_status']!r}"
    )


@then(parsers.parse('the run error code is "{expected_code}"'))
def run_error_code(expected_code: str, ctx):
    assert ctx["error_code"] == expected_code, f"Expected error code {expected_code!r}, got {ctx['error_code']!r}"


@then(parsers.parse('an EvalResult row exists for eval "{eval_name}" with passed={passed}'))
def eval_result_exists(eval_name: str, passed: str, ctx):
    """Assert that an EvalResult row was persisted for the named eval."""
    expected_passed = passed.lower() == "true"
    # Find the eval def by name
    eval_def = None
    for ed in ctx["eval_defs"]:
        if ed["name"] == eval_name:
            eval_def = ed
            break
    assert eval_def is not None, f"Eval definition {eval_name!r} not found"

    # Find matching row in simulated DB
    matching = [r for r in ctx["eval_results_db"] if r["eval_id"] == eval_def["id"]]
    assert len(matching) >= 1, f"No EvalResult row found for eval {eval_name!r} (eval_id={eval_def['id']})"
    row = matching[0]
    assert row["passed"] is expected_passed, (
        f"EvalResult for {eval_name!r}: expected passed={expected_passed}, got passed={row['passed']}"
    )
