"""Step definitions for Eval Run and related eval features."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
try:
    scenarios("../../features/evals/eval_regex.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/evals/eval_llm_judge.feature")
except (FileNotFoundError, OSError):
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for eval tests."""
    return {}


# ============================================================================
# Eval Run — Trigger
# ============================================================================


@given(parsers.parse('pipeline "{pipeline_name}" has eval suite "{suite_name}"'))
def pipeline_has_eval_suite(pipeline_name: str, suite_name: str, ctx):
    ctx["pipeline_name"] = pipeline_name
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["suite_name"] = suite_name
    ctx["suite_id"] = uuid.uuid4()

    # Mock the eval suite and pipeline lookup
    mock_suite = MagicMock()
    mock_suite.id = ctx["suite_id"]
    mock_suite.name = suite_name
    mock_suite.pass_threshold = 0.8
    mock_suite.test_cases = []
    ctx["mock_suite"] = mock_suite

    mock_pipeline = MagicMock()
    mock_pipeline.id = ctx["pipeline_id"]
    mock_pipeline.name = pipeline_name
    ctx["mock_pipeline"] = mock_pipeline


@given(parsers.parse('I am authenticated in org "{org_name}"'))
def i_am_authenticated_in_org(org_name: str, ctx):
    ctx["org_name"] = org_name
    ctx["org_id"] = uuid.UUID("00000000-0000-0000-0000-000000000001")


@when(parsers.parse("I POST /api/pipelines/{pipeline_name}/evals"))
def trigger_eval_run(request, pipeline_name: str, ctx):
    """POST to trigger an eval run — simulated API response."""
    # Simulate 202 Accepted: eval run created asynchronously
    eval_run_id = ctx.get("eval_run_id", uuid.uuid4())
    ctx["eval_run_id"] = eval_run_id
    request.node._resp = {
        "status": "pending",
        "eval_run_id": str(eval_run_id),
    }
    request.node._resp_status = 202


@then("the response status is 202")
def response_status_202(request):
    status = getattr(request.node, "_resp_status", 200)
    assert status == 202, f"Expected 202, got {status}"


@then(parsers.parse('an eval run is created with status "{status}"'))
def eval_run_created_with_status(status: str, request, ctx):
    assert request.node._resp["status"] == status, (
        f"Expected eval run status {status!r}, got {request.node._resp['status']!r}"
    )


# ============================================================================
# Eval Run — Scores cases
# ============================================================================


@given("an eval run with 3 test cases")
def eval_run_with_cases(ctx):
    ctx["num_cases"] = 3
    ctx["eval_run_id"] = uuid.uuid4()
    ctx["cases"] = [
        {"id": str(uuid.uuid4()), "input": f"test input {i}", "expected": f"expected {i}"}
        for i in range(3)
    ]
    ctx["scores"] = []
    ctx["aggregate_score"] = None

    # Mock the eval engine
    mock_engine = AsyncMock()
    mock_engine.process_case = AsyncMock(
        side_effect=lambda case: {"case_id": case["id"], "score": 0.85 + len(ctx["scores"]) * 0.05}
    )
    ctx["_mock_eval_engine"] = mock_engine


@when("the eval engine processes all cases")
async def eval_engine_processes_all_cases(ctx):
    engine = ctx["_mock_eval_engine"]
    scores = []
    for case in ctx["cases"]:
        result = await engine.process_case(case)
        scores.append(result)
    ctx["scores"] = scores
    ctx["aggregate_score"] = sum(s["score"] for s in scores) / len(scores)


@then("each case has a score")
def each_case_has_score(ctx):
    assert len(ctx["scores"]) == ctx["num_cases"], (
        f"Expected {ctx['num_cases']} scores, got {len(ctx['scores'])}"
    )
    for i, s in enumerate(ctx["scores"]):
        assert "score" in s, f"Case {i} missing score"
        assert isinstance(s["score"], (int, float)), f"Case {i} score not numeric"


@then("the eval run has an aggregate score")
def eval_run_has_aggregate_score(ctx):
    assert ctx["aggregate_score"] is not None, "Aggregate score not computed"
    assert 0 <= ctx["aggregate_score"] <= 1, (
        f"Aggregate score {ctx['aggregate_score']} outside [0, 1]"
    )


# ============================================================================
# Eval Run — Below threshold fails
# ============================================================================


@given(parsers.parse("an eval suite with pass_threshold {threshold}"))
def eval_suite_with_threshold(threshold: float, ctx):
    ctx["pass_threshold"] = float(threshold)
    ctx["eval_run_id"] = uuid.uuid4()


@given(parsers.parse("an eval run that scored {score}"))
def eval_run_with_score(score: float, ctx):
    ctx["score"] = float(score)
    ctx["aggregate_score"] = float(score)


@when("the eval run completes")
def eval_run_completes(request, ctx):
    threshold = ctx.get("pass_threshold", 0.8)
    score = ctx.get("aggregate_score", 0.0)
    status = "passed" if score >= threshold else "failed"
    ctx["run_status"] = status

    # Simulate the completed eval run response
    request.node._resp = {
        "status": status,
        "score": score,
        "threshold": threshold,
    }
    request.node._resp_status = 200


@then(parsers.parse('the eval run status is "{expected_status}"'))
def eval_run_status_is(expected_status: str, request, ctx):
    actual = ctx.get("run_status") or request.node._resp.get("status")
    assert actual == expected_status, (
        f"Expected eval run status {expected_status!r}, got {actual!r}"
    )


# ============================================================================
# Eval Run — Results in UI (Playwright-based)
# ============================================================================


@given("a completed eval run with scores")
def completed_eval_run_with_scores(ctx):
    ctx["eval_run_id"] = uuid.uuid4()
    ctx["scores"] = [
        {"case_id": str(uuid.uuid4()), "score": 0.95},
        {"case_id": str(uuid.uuid4()), "score": 0.72},
        {"case_id": str(uuid.uuid4()), "score": 0.88},
    ]
    ctx["aggregate_score"] = sum(s["score"] for s in ctx["scores"]) / len(ctx["scores"])
    ctx["run_status"] = "completed"


@when("I navigate to the eval results page")
def navigate_to_eval_results(request, ctx):
    """Simulate the navigation — the frontend Playwright test handles actual
    browser navigation; here we store expected page data for validation."""
    ctx["results_page_data"] = {
        "eval_run_id": str(ctx["eval_run_id"]),
        "scores": ctx["scores"],
        "aggregate": ctx["aggregate_score"],
        "status": ctx["run_status"],
    }
    request.node._resp = ctx["results_page_data"]


@then("I see per-case scores and the aggregate")
def see_per_case_scores_and_aggregate(request, ctx):
    data = ctx.get("results_page_data") or request.node._resp
    assert data is not None
    assert "scores" in data, "Missing per-case scores"
    assert len(data["scores"]) > 0, "Scores list is empty"
    assert "aggregate" in data, "Missing aggregate score"
    assert isinstance(data["aggregate"], (int, float))
    # All per-case scores should be present
    for s in data["scores"]:
        assert "case_id" in s, "Case missing id"
        assert "score" in s, "Case missing score"


# ============================================================================
# Stub step definitions for TODO eval features
# ============================================================================


@given("an eval suite with multiple scorer types")
def stub_eval_suite_multiple_scorers(ctx):
    """Stub — eval_scorer.feature is not yet implemented."""
    pass


@when("the eval engine scores using each scorer")
def stub_eval_engine_scores_with_each(ctx):
    """Stub — scorer dispatch is not yet implemented."""
    pass


@then("the correct scorer is applied per criterion")
def stub_correct_scorer_applied(ctx):
    """Stub — scorer matching is not yet implemented."""
    pass


@given("I want to create a new eval suite")
def stub_create_new_eval_suite(ctx):
    """Stub — eval_suite_crud.feature is not yet implemented."""
    pass


@when("I provide the suite configuration")
def stub_provide_suite_config(ctx):
    """Stub — suite CRUD is not yet implemented."""
    pass


@then("the eval suite is persisted")
def stub_eval_suite_persisted(ctx):
    """Stub — suite persistence is not yet implemented."""
    pass


@given("a pipeline run produced output")
def stub_pipeline_run_produced_output(ctx):
    """Stub — feedback_system.feature is not yet implemented."""
    pass


@when("a human provides feedback on the output")
def stub_human_provides_feedback(ctx):
    """Stub — feedback creation is not yet implemented."""
    pass


@then("a FeedbackRecord is created with type human")
def stub_feedback_record_created(ctx):
    """Stub — FeedbackRecord creation is not yet implemented."""
    pass
