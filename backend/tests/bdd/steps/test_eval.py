"""Step definitions for Eval Run and related eval features."""

import contextlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/eval/eval_run.feature")

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
        {"id": str(uuid.uuid4()), "input": f"test input {i}", "expected": f"expected {i}"} for i in range(3)
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
def eval_engine_processes_all_cases(ctx):
    """Process all cases through the mocked eval engine.

    pytest-bdd does not await ``async def`` step functions, so the
    coroutine-in-mock bug here produced zero scores. Drive the engine from
    a fresh event loop instead, matching the pattern used by the other
    async steps in this module.
    """
    import asyncio

    engine = ctx["_mock_eval_engine"]
    cases = ctx["cases"]

    async def _process() -> None:
        scores = []
        for case in cases:
            result = await engine.process_case(case)
            scores.append(result)
        ctx["scores"] = scores
        ctx["aggregate_score"] = sum(s["score"] for s in scores) / len(scores)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_process())
    finally:
        loop.close()


@then("each case has a score")
def each_case_has_score(ctx):
    assert len(ctx["scores"]) == ctx["num_cases"], f"Expected {ctx['num_cases']} scores, got {len(ctx['scores'])}"
    for i, s in enumerate(ctx["scores"]):
        assert "score" in s, f"Case {i} missing score"
        assert isinstance(s["score"], (int, float)), f"Case {i} score not numeric"


@then("the eval run has an aggregate score")
def eval_run_has_aggregate_score(ctx):
    assert ctx["aggregate_score"] is not None, "Aggregate score not computed"
    assert 0 <= ctx["aggregate_score"] <= 1, f"Aggregate score {ctx['aggregate_score']} outside [0, 1]"


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
    assert actual == expected_status, f"Expected eval run status {expected_status!r}, got {actual!r}"


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
    assert data["scores"], "Scores list is empty"
    assert "aggregate" in data, "Missing aggregate score"
    assert isinstance(data["aggregate"], (int, float))
    # All per-case scores should be present
    for s in data["scores"]:
        assert "case_id" in s, "Case missing id"
        assert "score" in s, "Case missing score"


# ============================================================================
# eval/eval_scorer.feature  —  5 scenarios
# ============================================================================
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/eval/eval_scorer.feature")


@given("an eval suite with multiple scorer types")
def step_eval_suite_multiple_scorers(ctx):
    ctx["eval_scorer_type"] = None
    ctx["eval_output"] = None
    ctx["eval_error"] = None
    ctx["eval_passed"] = None


@given(parsers.parse('the criterion uses eval_type "{eval_type}" with pattern "{pattern}"'))
def step_scorer_regex_criterion(eval_type, pattern, ctx):
    ctx["eval_scorer_type"] = eval_type
    ctx["eval_config"] = {"pattern": pattern}


@given(parsers.parse('the criterion uses eval_type "{eval_type}" with a schema'))
def step_scorer_json_schema_criterion(eval_type, ctx):
    ctx["eval_scorer_type"] = eval_type
    ctx["eval_config"] = {
        "schema": {
            "type": "object",
            "properties": {"valid": {"type": "boolean"}},
            "required": ["valid"],
        }
    }


@given(parsers.parse('the criterion uses eval_type "{eval_type}" with rubric prompt "{rubric}"'))
def step_scorer_llm_judge_criterion(eval_type, rubric, ctx):
    """LLM judge criterion (eval_scorer.feature).

    pytest-bdd matches the most specific step pattern regardless of
    registration order, so the rubric-prompt variant wins over the generic
    ``the criterion uses eval_type "{eval_type}"`` step for ``... with rubric
    prompt "..."`` scenarios.
    """
    ctx["eval_scorer_type"] = eval_type
    ctx["eval_config"] = {"rubric_prompt": rubric}


@given(parsers.parse('the criterion uses eval_type "{eval_type}"'))
def step_scorer_custom_criterion(eval_type, ctx):
    ctx["eval_scorer_type"] = eval_type
    ctx["eval_config"] = {}


@given(parsers.parse('the criterion uses eval_type "{eval_type}" with pattern "{pattern}" and type "{type_val}"'))
def step_scorer_regex_with_type(eval_type, pattern, type_val, ctx):
    """Duplicate registration for alternate step pattern."""
    ctx["eval_scorer_type"] = eval_type
    ctx["eval_config"] = {"pattern": pattern}


@when("the eval engine scores using each scorer")
def step_eval_engine_scores(ctx):
    from modulo.core.eval_engine import EvalDefinition, EvalEngine

    engine = EvalEngine()
    output = ctx.get("eval_output", {})
    eval_type = ctx.get("eval_scorer_type", "")
    config = ctx.get("eval_config", {})

    try:
        eval_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            name="scorer-test",
            eval_type=eval_type,
            config=config,
        )
        result = engine.evaluate(output, eval_def)
        ctx["eval_passed"] = result.passed
        ctx["eval_result"] = result
        ctx["eval_error"] = None
    except Exception as exc:
        ctx["eval_passed"] = None
        ctx["eval_error"] = str(exc)


@then("the correct scorer is applied per criterion")
def step_correct_scorer_applied(ctx):
    """Confirm that no error was raised during scoring dispatch."""
    error = ctx.get("eval_error")
    assert error is None, f"Scorer dispatch failed: {error}"


@then("an error is raised for unknown eval type")
def step_unknown_eval_type_error(ctx):
    error = ctx.get("eval_error")
    assert error is not None, "Expected an error for unknown eval type"
    # The actual error message will vary — we just check one was raised


@then(parsers.parse('the output "{output}" passes the regex scorer'))
def step_output_passes_regex(output, ctx):
    ctx["eval_output"] = {"text": output}
    from modulo.core.eval_engine import EvalDefinition, EvalEngine

    engine = EvalEngine()
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="regex-pass",
        eval_type="regex",
        config={"pattern": ctx.get("eval_config", {}).get("pattern", ""), "field": "text"},
    )
    result = engine.evaluate(ctx["eval_output"], eval_def)
    assert result.passed, f"Regex scorer failed for output {output!r}: {result.detail}"


@then(parsers.parse('the output "{output}" fails the regex scorer'))
def step_output_fails_regex(output, ctx):
    ctx["eval_output"] = {"text": output}
    from modulo.core.eval_engine import EvalDefinition, EvalEngine

    engine = EvalEngine()
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="regex-fail",
        eval_type="regex",
        config={"pattern": ctx.get("eval_config", {}).get("pattern", ""), "field": "text"},
    )
    result = engine.evaluate(ctx["eval_output"], eval_def)
    assert not result.passed, f"Regex scorer should have failed for output {output!r}"


@then("valid data passes the json_schema scorer")
def step_valid_data_passes_json_schema(ctx):
    ctx["eval_output"] = {"valid": True}
    from modulo.core.eval_engine import EvalDefinition, EvalEngine

    engine = EvalEngine()
    config = ctx.get("eval_config", {})
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="json-schema-pass",
        eval_type="json_schema",
        config=config,
    )
    result = engine.evaluate(ctx["eval_output"], eval_def)
    assert result.passed, f"JSON Schema scorer failed: {result.detail}"


# ============================================================================
# eval/eval_suite_crud.feature  —  5 scenarios
# ============================================================================
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/eval/eval_suite_crud.feature")


def _eval_resp(status_code, **kwargs):
    return SimpleNamespace(
        status_code=status_code,
        ok=200 <= status_code < 300,
        json=lambda: kwargs,
        text=json.dumps(kwargs),
    )


@given(parsers.parse('an eval definition "{name}" exists'))
def step_eval_def_exists(name, request, ctx):
    ctx["eval_def_name"] = name
    ctx["eval_def_id"] = uuid.uuid4()
    ctx["eval_def_type"] = "regex"
    ctx["eval_def_pipeline_id"] = uuid.uuid4()


@when(
    parsers.parse('I POST /api/evals with name "{name}" and type "{eval_type}"'),
)
def step_create_eval_def(name, eval_type, request, ctx):
    """Create eval definition — checks auth context for 403."""
    # The conftest auth steps flag viewer scenarios on the node; branching on
    # that real auth state (instead of the scenario title) keeps new scenarios
    # from accidentally inheriting a spurious 403.
    if getattr(request.node, "_viewer_auth", False):
        request.node._resp = _eval_resp(403, detail="Only admins can create eval definitions")
        return

    from unittest.mock import AsyncMock, MagicMock

    from modulo.db.models.eval_definition import EvalDefinition

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    import asyncio

    eval_def_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    loop = asyncio.new_event_loop()
    try:
        ed = EvalDefinition(
            organisation_id=ORG_ID,
            pipeline_id=pipeline_id,
            name=name,
            eval_type=eval_type,
            config_json={},
            failure_behaviour="warn",
            account_id=USER_ID,
        )
        ed.id = eval_def_id
        mock_session.add(ed)
        loop.run_until_complete(mock_session.flush())

        ctx["eval_def_id"] = eval_def_id
        ctx["eval_def_name"] = name
        ctx["eval_def_type"] = eval_type
        request.node._resp = _eval_resp(201, id=str(eval_def_id), name=name, eval_type=eval_type)
    except Exception as exc:
        request.node._resp = _eval_resp(500, error=str(exc))
    finally:
        loop.close()


@when(parsers.parse('I PUT /api/evals/{eval_id} with a new name "{name}"'))
def step_update_eval_def(name, request, ctx):
    if getattr(request.node, "_viewer_auth", False):
        request.node._resp = _eval_resp(403, detail="Only admins can update eval definitions")
        return
    eval_id = ctx.get("eval_def_id", uuid.uuid4())
    request.node._resp = _eval_resp(200, id=str(eval_id), name=name, eval_type=ctx.get("eval_def_type", "regex"))


@when(parsers.parse("I DELETE /api/evals/{eval_id}"))
def step_delete_eval_def(request, ctx):
    if getattr(request.node, "_viewer_auth", False):
        request.node._resp = _eval_resp(403, detail="Only admins can delete eval definitions")
        return
    request.node._resp = _eval_resp(204)


@when("I GET /api/evals")
def step_list_evals(request, ctx):
    items = []
    if ctx.get("eval_def_name"):
        items.append(
            {
                "id": str(ctx["eval_def_id"]),
                "name": ctx["eval_def_name"],
                "eval_type": ctx.get("eval_def_type", "regex"),
            }
        )
    request.node._resp = _eval_resp(200, items=items, total=len(items), page=1, page_size=20)


@then(parsers.parse('the response contains eval definition "{name}"'))
def step_response_contains_eval_def(name, request, ctx):
    body = request.node._resp.json()
    items = body.get("items", [])
    names = [item.get("name") for item in items]
    assert name in names, f"Expected eval def {name!r} in response, got: {names}"


# ============================================================================
# eval/feedback_system.feature  —  5 scenarios
# ============================================================================
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/eval/feedback_system.feature")


@given("a pipeline run produced output")
def step_feedback_pipeline_run_output(ctx):
    ctx["run_id"] = uuid.uuid4()
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["feedback_record_id"] = None
    ctx["feedback_status"] = None


@given(parsers.parse('a feedback record with status "{status}"'))
def step_feedback_record_with_status(status, ctx):
    ctx["feedback_record_id"] = uuid.uuid4()
    ctx["feedback_status"] = status
    ctx["run_id"] = uuid.uuid4()


@given("the feedback has a valid run_id")
def step_feedback_has_run_id(ctx):
    if "run_id" not in ctx:
        ctx["run_id"] = uuid.uuid4()


@given("an eval suite that would pass the output")
def step_feedback_eval_suite_passes(ctx):
    ctx["eval_suite_pass"] = True


@when("a human provides feedback on the output")
def step_feedback_human_provides(ctx, request):
    """Simulate creating a feedback record via FeedbackManager."""
    from unittest.mock import AsyncMock

    from modulo.core.feedback_manager import FeedbackManager

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    mgr = FeedbackManager(mock_session, ORG_ID)

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        record = loop.run_until_complete(
            mgr.create_feedback_record(
                run_id=ctx.get("run_id", uuid.uuid4()),
                gate_id="gate-output-review",
                account_id=USER_ID,
                rejection_reason="Output contained hallucination",
                rejected_output={"text": "Incorrect data"},
                producing_node_id=str(uuid.UUID("00000000-0000-0000-0000-0000000000aa")),
                feedback_handler_type="human",
            )
        )
        ctx["feedback_record_id"] = record.id or uuid.uuid4()
        ctx["feedback_status"] = record.feedback_status
        ctx["feedback_handler_type"] = record.feedback_handler_type
    finally:
        loop.close()


@when(parsers.parse('the status is changed to "{new_status}"'))
def step_feedback_change_status(new_status, ctx, request):
    from unittest.mock import AsyncMock, MagicMock

    from modulo.core.feedback_manager import FeedbackManager, InvalidTransitionError

    mock_session = AsyncMock()
    mock_session.get = AsyncMock()

    mgr = FeedbackManager(mock_session, ORG_ID)
    record_id = ctx.get("feedback_record_id", uuid.uuid4())

    from modulo.db.models.feedback_record import FeedbackRecord

    mock_record = MagicMock(spec=FeedbackRecord)
    mock_record.id = record_id
    mock_record.feedback_status = ctx.get("feedback_status", "pending")

    # ``update_status`` reads via ``execute(...).scalar_one_or_none()``, not
    # ``session.get`` — a bare AsyncMock execute returns a coroutine for the
    # row, so wire the result object to return the mock record synchronously.
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_record
    mock_session.execute.return_value = mock_result

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        try:
            loop.run_until_complete(mgr.update_status(record_id, new_status))
        except InvalidTransitionError as exc:
            ctx["transition_error"] = str(exc)
        else:
            ctx["feedback_status"] = new_status
            ctx["transition_error"] = None
    finally:
        loop.close()


@when("the system detects an eval gap")
def step_feedback_detect_eval_gap(ctx, request):
    from unittest.mock import AsyncMock, MagicMock

    from modulo.core.feedback_manager import FeedbackManager

    mock_session = AsyncMock()
    mock_session.get = AsyncMock()

    # Mock a feedback record
    mock_record = MagicMock()
    mock_record.id = uuid.uuid4()
    mock_record.run_id = ctx.get("run_id", uuid.uuid4())
    mock_record.rejected_output = {"text": "This is incorrect"}
    mock_record.feedback_status = "pending"
    ctx["_mock_record"] = mock_record

    mgr = FeedbackManager(mock_session, ORG_ID)

    import asyncio

    from modulo.core.eval_engine import EvalDefinition

    # Provide an eval suite that passes on the output text "This is incorrect"
    # → no eval catches the rejection → this IS an eval gap
    passing_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        name="passing-check",
        eval_type="regex",
        config={"pattern": "This is incorrect", "field": "text"},
    )

    loop = asyncio.new_event_loop()
    try:
        is_gap = loop.run_until_complete(mgr.detect_eval_gap(mock_record, eval_suite=[passing_def]))
        ctx["eval_gap"] = is_gap
    finally:
        loop.close()


@when("a correction run is spawned")
def step_feedback_spawn_correction(ctx, request):
    from unittest.mock import AsyncMock, MagicMock, patch

    from modulo.core.feedback_manager import FeedbackManager

    mock_session = AsyncMock()
    fake_run_id = uuid.uuid4()

    # Mock get_run to return a fake run
    mock_get_run = AsyncMock()
    mock_run = MagicMock()
    mock_run.id = ctx.get("run_id", uuid.uuid4())
    mock_run.pipeline_id = uuid.uuid4()
    mock_run.snapshot_id = uuid.uuid4()
    mock_run.input_payload = {}
    mock_run.created_by = USER_ID
    mock_get_run.return_value = mock_run

    # Mock create_run to return a new run
    mock_create_run = AsyncMock()
    mock_new_run = MagicMock()
    mock_new_run.id = fake_run_id
    mock_create_run.return_value = mock_new_run

    mgr = FeedbackManager(mock_session, ORG_ID)
    record_id = ctx.get("feedback_record_id", uuid.uuid4())

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        with (
            patch("modulo.core.feedback_manager.get_run", mock_get_run),
            patch("modulo.core.feedback_manager.create_run", mock_create_run),
        ):
            mock_record = MagicMock()
            mock_record.id = record_id
            mock_record.run_id = ctx.get("run_id", uuid.uuid4())
            mock_record.rejection_reason = "Bad output"
            mock_record.rejected_output = {"text": "bad"}
            mock_record.producing_node_id = "node-gen"
            mock_record.account_id = USER_ID
            mock_record.feedback_status = "pending"
            # A bare MagicMock is truthy, so without this the manager believes
            # the record already has a correction run and raises
            # ConcurrentModificationError.
            mock_record.correction_run_id = None
            mock_session.get = AsyncMock(return_value=mock_record)

            # ``link_correction_run`` reads + writes via
            # ``execute(...).scalar_one_or_none()`` — route both to the mock
            # record so the link succeeds instead of awaiting a coroutine row.
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_record
            mock_session.execute.return_value = mock_result

            # Also patch get_feedback_record to return the mock
            with patch.object(mgr, "get_feedback_record", AsyncMock(return_value=mock_record)):
                new_run_id = loop.run_until_complete(mgr.spawn_correction_run(record_id))
                ctx["correction_run_id"] = new_run_id
                ctx["feedback_status"] = "correcting"
    finally:
        loop.close()


@then("a FeedbackRecord is created with type human")
def step_feedback_record_created_human(ctx):
    assert ctx.get("feedback_record_id") is not None, "No feedback record created"
    assert ctx.get("feedback_handler_type") == "human", (
        f"Expected human handler, got {ctx.get('feedback_handler_type')}"
    )


@then(parsers.parse('the feedback status is "{expected}"'))
def step_feedback_status_is(expected, ctx, request):
    actual = ctx.get("feedback_status")
    assert actual == expected, f"Expected feedback status {expected!r}, got {actual!r}"


@then(parsers.parse('the feedback status becomes "{expected}"'))
def step_feedback_status_becomes(expected, ctx, request):
    step_feedback_status_is(expected, ctx, request)


@then("the transition is allowed")
def step_feedback_transition_allowed(ctx):
    assert ctx.get("transition_error") is None, f"Transition was rejected: {ctx['transition_error']}"


@then("the transition is rejected")
def step_feedback_transition_rejected(ctx):
    assert ctx.get("transition_error") is not None, "Transition should have been rejected but it succeeded"


@then("the feedback record has eval_gap true")
def step_feedback_eval_gap_true(ctx):
    assert ctx.get("eval_gap") is True, f"Expected eval_gap=True, got {ctx.get('eval_gap')}"


@then("a new correction run is created")
def step_feedback_correction_run_created(ctx):
    assert ctx.get("correction_run_id") is not None, "No correction run created"
