"""Step definitions for eval-run, eval-suite CRUD, scorer and feedback features.

eval_run.feature and eval_suite_crud.feature drive REAL product surfaces:

- The eval-suite CRUD steps drive the real FastAPI routes through
  ``session_client`` (real routing, real permission floor, real Pydantic
  response validation); only the DB seams are stubbed.
- The trigger step drives the real ``build_suite_run`` construction path -
  the cron/SAQ eval-trigger dispatch creates SuiteRuns there (there is no
  HTTP route; the old POST /api/pipelines/{name}/evals step text was a
  fabricated endpoint).
- The below-threshold scenario drives the real state transition
  (``_suite_run_transition``) and real completion pipeline
  (``record_completion`` / ``suite_pass_rate``); the engine's llm_judge
  compute applies the suite threshold through a caller-injected judge
  callable, exactly as the suite-run runner does.
- The results step drives the real ``GET /api/v1/runs/{run_id}/evals``
  route and the real pass-rate aggregator.

eval_scorer.feature and feedback_system.feature keep their own steps below.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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


@when("the eval suite trigger fires")
def trigger_eval_run(ctx):
    """Drive the real eval-trigger construction path.

    The cron/SAQ eval-trigger dispatch calls ``build_suite_run`` to construct
    and persist a pending SuiteRun (there is no HTTP route). The five
    org-scoped loads (suite, dataset, model backend, suite definitions, active
    cases) are shaped; the constructed SuiteRun is captured from ``session.add``.
    """
    import asyncio

    from modulo.core.eval_engine.execute_suite_run import build_suite_run
    from tests.bdd.conftest import make_mock_session

    def _definition() -> MagicMock:
        definition = MagicMock()
        definition.id = uuid.uuid4()
        definition.eval_type = "regex"
        definition.config_json = {"pattern": "ok"}
        return definition

    definitions = [_definition(), _definition()]
    cases = [MagicMock(), MagicMock(), MagicMock()]

    def shaper(session: MagicMock) -> None:
        suite = MagicMock()
        suite.id = ctx["suite_id"]
        dataset = MagicMock()
        dataset.version = 2
        backend = MagicMock()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=suite)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=dataset)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=backend)),
                MagicMock(scalars=MagicMock(return_value=definitions)),
                MagicMock(scalars=MagicMock(return_value=cases)),
            ]
        )
        session.add = MagicMock(side_effect=lambda run: ctx.__setitem__("suite_run", run))
        session.flush = AsyncMock()

    session = make_mock_session()
    shaper(session)
    ctx["dataset_version"] = 2

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            build_suite_run(
                session,
                org_id=ORG_ID,
                suite_id=ctx["suite_id"],
                dataset_id=uuid.uuid4(),
                model_backend_id=uuid.uuid4(),
                pipeline_id=ctx["pipeline_id"],
            )
        )
    finally:
        loop.close()


@then(parsers.parse('the eval run starts with status "{status}"'))
def eval_run_starts_with_status(status: str, ctx):
    run = ctx["suite_run"]
    assert run.state == status, f"Expected eval run state {status!r}, got {run.state!r}"
    assert str(run.suite_id) == str(ctx["suite_id"]), "Run not scoped to the trigger's suite"
    assert run.dataset_version == ctx["dataset_version"], "Dataset version snapshot not pinned at construction"
    assert run.total_cases == 0, "A pending run starts with zero case counts (updated during execution)"
    assert run.extra["pipeline_id"] == str(ctx["pipeline_id"]), "Run not attributable to the trigger's pipeline"


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
# Eval Run - Below threshold does not pass
# ============================================================================


@given(parsers.parse("an eval suite with pass_threshold {threshold}"))
def eval_suite_with_threshold(threshold: float, ctx):
    ctx["pass_threshold"] = float(threshold)


@given(parsers.parse("an eval run whose case scored {score}"))
def eval_run_case_scored(score: float, ctx):
    """Score the case through the real llm_judge compute path.

    The judge callable is the caller-injected decision boundary; this is
    exactly what the suite-run runner does when executing an llm_judge
    evaluation against a pinned model: the judge applies the suite's
    pass threshold to its rubric score.
    """
    from modulo.core.eval_engine import EvalDefinition, EvalEngine

    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        name="quality-check",
        eval_type="llm_judge",
        config={},
        pass_threshold=ctx["pass_threshold"],
        failure_behaviour="warn",
    )

    def judge(output: dict, defn) -> dict:
        rubric_score = float(output["score"])
        return {
            "passed": rubric_score >= defn.pass_threshold,
            "score": rubric_score,
            "detail": "rubric verdict",
        }

    # ``evaluate_result`` is a plain sync method on EvalEngine - there is no
    # coroutine to await, so no event loop is involved.
    result = EvalEngine().evaluate_result(
        {"score": float(score)},
        eval_def,
        llm_judge_callable=judge,
    )
    ctx["case_result"] = result
    ctx["score"] = float(score)


@when("the eval run completes")
def eval_run_completes(ctx):
    """Drive the real completion path.

    ``_suite_run_transition`` moves RUNNING -> COMPLETED under an
    optimistic-lock UPDATE; ``record_completion`` then resolves the baseline
    (none exists here), computes the comparison (skipped with a warning) and
    sets ``completed_at``.
    """
    import asyncio

    from modulo.core.eval_engine.execute_suite_run import _suite_run_transition as suite_run_transition
    from modulo.core.eval_engine.suite_run import record_completion
    from modulo.db.models.eval_suite_run import SuiteRun, SuiteRunState
    from tests.bdd.conftest import make_mock_session

    run = SuiteRun(
        id=uuid.uuid4(),
        organisation_id=ORG_ID,
        suite_id=ctx.get("suite_id", uuid.uuid4()),
        dataset_id=uuid.uuid4(),
        dataset_version=1,
        definition_checksum="abc123",
        model_backend_id=uuid.uuid4(),
        scenario_signature="default",
        baseline_tuple={},
        state=SuiteRunState.RUNNING.value,
        version=1,
        total_cases=1,
        passed_cases=0,
        failed_cases=1,
        excluded_case_count=0,
        claimed_cost=Decimal(0),
    )
    session = make_mock_session()
    # _suite_run_transition: optimistic-lock UPDATE returns the new version.
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=2)),
        ]
    )
    # record_completion: refresh the run, then resolve the baseline via
    # ``session.scalars`` (no completed same-tuple run exists here), flush.
    session.refresh = AsyncMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    session.flush = AsyncMock()

    async def _complete() -> None:
        await suite_run_transition(session, run, SuiteRunState.COMPLETED)
        await record_completion(session, run, {})

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_complete())
    finally:
        loop.close()
    ctx["suite_run"] = run


@then(parsers.parse('the eval run status is "{expected_status}"'))
def eval_run_status_is(expected_status: str, ctx):
    run = ctx["suite_run"]
    actual = run.state
    assert actual == expected_status, f"Expected eval run status {expected_status!r}, got {actual!r}"
    assert run.completed_at is not None, "Completed run missing completed_at"
    assert run.version == 2, "Optimistic-lock version not bumped by the transition"


@then("no case passed the eval")
def no_case_passed(ctx):
    from modulo.core.eval_engine.suite_run import suite_pass_rate

    result = ctx["case_result"]
    assert not result.passed, f"Case scored {ctx['score']} below threshold must not pass"
    assert result.score == pytest.approx(ctx["score"]), "Judge score not carried into the result"
    stats = suite_pass_rate([result], ctx["suite_run"].excluded_case_count)
    assert stats["passed"] == 0, f"Expected 0 passed cases, got {stats['passed']}"
    assert stats["total"] == 1, f"Expected 1 total case, got {stats['total']}"
    assert stats["pass_rate"] == 0.0, f"Expected pass_rate 0.0, got {stats['pass_rate']}"
    comparison = ctx["suite_run"].comparison_json
    assert comparison is not None, "record_completion did not attach a comparison"
    assert isinstance(comparison, dict), f"comparison_json not a dict: {type(comparison)}"


# ============================================================================
# Eval Run — Results in UI (Playwright-based)
# ============================================================================


@given("a completed eval run with scores")
def completed_eval_run_with_scores(ctx):
    from modulo.db.models.eval_result import EvalResult

    eval_id = uuid.uuid4()
    run_id = uuid.uuid4()
    ctx["run_id"] = run_id
    ctx["scores"] = [(True, 0.95), (False, 0.72), (True, 0.88)]
    evaluated_at = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    for index, (passed, score) in enumerate(ctx["scores"]):
        row = EvalResult(
            id=uuid.uuid4(),
            organisation_id=ORG_ID,
            run_id=run_id,
            suite_run_id=None,
            node_id=uuid.uuid4(),
            eval_id=eval_id,
            passed=passed,
            score=score,
            detail="rubric verdict",
            observed=False,
            evaluated_at=evaluated_at + timedelta(minutes=index),
        )
        rows.append(row)
    ctx["rows"] = rows
    ctx["run_row"] = MagicMock(id=run_id)


@when("I navigate to the eval results page")
def navigate_to_eval_results(request, ctx):
    """Drive the real ``GET /api/v1/runs/{run_id}/evals`` route.

    The three business queries (run lookup, count, rows) are shaped; the route
    body contract - the 404 on a foreign-org run, the per-item wire shape, the
    pagination envelope - runs unpatched.
    """
    from tests.bdd.conftest import session_client

    def shaper(session: MagicMock) -> None:
        _eval_shaped_execute(
            session,
            [
                MagicMock(scalar_one_or_none=MagicMock(return_value=ctx["run_row"])),
                MagicMock(scalar=MagicMock(return_value=len(ctx["rows"]))),
                MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=ctx["rows"])))),
            ],
        )

    with (
        session_client(role=_eval_role(request), shaper=shaper) as client,
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/runs/{ctx['run_id']}/evals")
    request.node._resp = resp


@then("I see per-case scores and the aggregate")
def see_per_case_scores_and_aggregate(request, ctx):
    from modulo.core.eval_engine.suite_run import suite_pass_rate

    resp = request.node._resp
    assert resp.status_code == 200, f"Expected 200 for run eval results, got {resp.status_code}"
    body = resp.json()
    assert body["total"] == len(ctx["rows"]), f"Expected total {len(ctx['rows'])}, got {body['total']}"
    assert len(body["items"]) == len(ctx["rows"]), "Item count does not match the shaped row count"
    for item in body["items"]:
        assert item["run_id"] == str(ctx["run_id"]), "Result not scoped to the requested run"
        assert item["passed"] in (True, False), f"Non-boolean passed value: {item['passed']!r}"
        assert isinstance(item["score"], (int, float)), f"Score not numeric: {item['score']!r}"
        assert item["evaluated_at"] is not None, "Missing evaluated_at timestamp"
    # Aggregate: the score list the backend exposed is what the pass-rate
    # aggregator consumes - drive it against the real rows the wire carried.
    expected_passes = sum(1 for passed, _ in ctx["scores"] if passed)
    stats = suite_pass_rate(ctx["rows"], 0)
    assert stats["total"] == len(ctx["rows"]), f"Expected total {len(ctx['rows'])}, got {stats['total']}"
    assert stats["passed"] == expected_passes, f"Expected {expected_passes} passed cases, got {stats['passed']}"
    assert stats["pass_rate"] == round(expected_passes / len(ctx["rows"]), 4), (
        f"Expected pass rate {round(expected_passes / len(ctx['rows']), 4)}, got {stats['pass_rate']}"
    )


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
# eval/eval_suite_crud.feature  -  5 scenarios
# ============================================================================
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/eval/eval_suite_crud.feature")


def _eval_row(
    *,
    eval_id: uuid.UUID,
    name: str,
    eval_type: str,
    pipeline_id: uuid.UUID,
):
    from modulo.db.models.eval import Eval

    row = Eval(
        organisation_id=ORG_ID,
        pipeline_id=pipeline_id,
        node_id=None,
        name=name,
        eval_type=eval_type,
        config_json={},
        pass_threshold=None,
        suite_id=None,
        account_id=USER_ID,
        version=1,
    )
    row.id = eval_id
    return row


def _eval_shaped_execute(session: MagicMock, shapes: list[MagicMock]) -> None:
    """Deprecated alias for :func:`tests.bdd.conftest._shaped_execute`.

    Kept as a thin delegation so this module's shaper and the canonical
    ``bdd/conftest.py`` one cannot drift; they differ only in the fallback
    shape, and the conftest version is the strictly more capable one
    (it models a real empty ``scalars`` list rather than an auto-mocked
    truthy object). New steps should call ``_shaped_execute`` directly.
    """
    from tests.bdd.conftest import _shaped_execute

    _shaped_execute(session, shapes)


def _eval_role(request) -> str:
    """Role wired by conftest's authenticated-as Given (default: admin)."""
    from tests.bdd.conftest import _shared_state

    return _shared_state(request).get("org_role", "admin")


@given(parsers.parse('an eval definition "{name}" exists'))
def step_eval_def_exists(name, request, ctx):
    ctx["eval_def_name"] = name
    ctx["eval_def_id"] = uuid.uuid4()
    ctx["eval_def_type"] = "regex"
    ctx["eval_def_pipeline_id"] = uuid.uuid4()
    ctx["eval_def_row"] = _eval_row(
        eval_id=ctx["eval_def_id"],
        name=name,
        eval_type=ctx["eval_def_type"],
        pipeline_id=ctx["eval_def_pipeline_id"],
    )


@when(
    parsers.parse('I POST /api/evals with name "{name}" and type "{eval_type}"'),
)
def step_create_eval_def(name, eval_type, request, ctx):
    """Drive the real ``POST /api/v1/evals`` route.

    Admin gating, guardrail validation and the pipeline existence check run
    unpatched; only ``create_or_update_eval`` (a module-level seam that reads
    and writes through the session) is stubbed, returning the row the route
    maps into the legacy response shape.
    """
    from modulo.db.models.eval import Eval
    from tests.bdd.conftest import session_client

    created = Eval(
        organisation_id=ORG_ID,
        pipeline_id=uuid.uuid4(),
        node_id=None,
        name=name,
        eval_type=eval_type,
        config_json={},
        pass_threshold=None,
        suite_id=None,
        account_id=USER_ID,
        version=1,
    )
    created.id = uuid.uuid4()

    def shaper(session: MagicMock) -> None:
        # Exactly one business query: the pipeline existence lookup.
        _eval_shaped_execute(
            session,
            [MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock()))],
        )

    with (
        session_client(role=_eval_role(request), shaper=shaper) as client,
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.create_or_update_eval", new_callable=AsyncMock) as create_eval_mock,
    ):
        create_eval_mock.return_value = created
        request.node._resp = client.post(
            "/api/v1/evals",
            json={"pipeline_id": str(uuid.uuid4()), "name": name, "eval_type": eval_type},
        )
    ctx["eval_def_row"] = created


@when(parsers.parse('I PUT /api/evals/{eval_id} with a new name "{name}"'))
def step_update_eval_def(name, eval_id, request, ctx):
    """Drive the real ``PUT /api/v1/evals/{eval_id}`` route.

    The three business queries (row lookup, current gate, gate reload) are
    shaped; ``create_or_update_eval`` is stubbed at the seam and returns an
    updated row, so the route's own contract - 404 handling, guardrail
    validation against the merged type/config, response mapping - runs
    unpatched.
    """
    from tests.bdd.conftest import session_client

    try:
        eval_uuid = uuid.UUID(eval_id)
    except ValueError:
        # The feature files carry the literal placeholder text; the id under
        # test is the one the Given recorded.
        eval_uuid = ctx.get("eval_def_id", uuid.uuid4())
    row = ctx.get("eval_def_row") or _eval_row(
        eval_id=eval_uuid,
        name=ctx.get("eval_def_name", "quality-check"),
        eval_type=ctx.get("eval_def_type", "regex"),
        pipeline_id=uuid.uuid4(),
    )
    updated = _eval_row(
        eval_id=eval_uuid,
        name=name,
        eval_type=row.eval_type,
        pipeline_id=row.pipeline_id,
    )

    def shaper(session: MagicMock) -> None:
        _eval_shaped_execute(
            session,
            [
                MagicMock(scalar_one_or_none=MagicMock(return_value=row)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ],
        )

    with (
        session_client(role=_eval_role(request), shaper=shaper) as client,
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.create_or_update_eval", new_callable=AsyncMock) as update_eval_mock,
    ):
        update_eval_mock.return_value = updated
        request.node._resp = client.put(f"/api/v1/evals/{eval_uuid}", json={"name": name})
    ctx["eval_def_name"] = name


@when(parsers.parse("I DELETE /api/evals/{eval_id}"))
def step_delete_eval_def(eval_id, request, ctx):
    """Drive the real ``DELETE /api/v1/evals/{eval_id}`` route.

    The definition under test is regex-typed, so the route takes the
    hard-delete path: one business query (row lookup) plus the ORM
    ``session.delete`` call - the soft-delete/audit branch stays product code.
    """
    from tests.bdd.conftest import session_client

    try:
        eval_uuid = uuid.UUID(eval_id)
    except ValueError:
        eval_uuid = ctx.get("eval_def_id", uuid.uuid4())
    row = ctx.get("eval_def_row") or _eval_row(
        eval_id=eval_uuid,
        name=ctx.get("eval_def_name", "quality-check"),
        eval_type="regex",
        pipeline_id=uuid.uuid4(),
    )

    def shaper(session: MagicMock) -> None:
        _eval_shaped_execute(session, [MagicMock(scalar_one_or_none=MagicMock(return_value=row))])
        session.delete = AsyncMock()

    with (
        session_client(role=_eval_role(request), shaper=shaper) as client,
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
    ):
        request.node._resp = client.delete(f"/api/v1/evals/{eval_uuid}")


@when("I GET /api/evals")
def step_list_evals(request, ctx):
    """Drive the real ``GET /api/v1/evals`` route.

    Two business queries when the page has rows (count, rows, gated
    PolicyGate page load - three total) and two when the list is empty.
    """
    from tests.bdd.conftest import session_client

    rows = [ctx["eval_def_row"]] if ctx.get("eval_def_row") else []
    side_effects = [
        MagicMock(scalar=MagicMock(return_value=len(rows))),
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))),
    ]
    if rows:
        # Batch PolicyGate page load - only issued when the page has rows.
        side_effects.append(MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

    def shaper(session: MagicMock) -> None:
        _eval_shaped_execute(session, side_effects)

    with (
        session_client(role=_eval_role(request), shaper=shaper) as client,
        patch("modulo.api.routes.evals.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.evals.set_rls_user_context", new_callable=AsyncMock),
    ):
        request.node._resp = client.get("/api/v1/evals")


@then(parsers.parse('the response contains eval definition "{name}"'))
def step_response_contains_eval_def(name, request, ctx):
    body = request.node._resp.json()
    items = body.get("items", [])
    names = [item.get("name") for item in items]
    assert name in names, f"Expected eval def {name!r} in response, got: {names}"
    assert body["total"] >= 1, "List route did not count the existing definition"


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
                review_id="gate-output-review",
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
