"""Run-level judging budget for suite runs (ADR 066 D10, finding 8).

The SAQ suite job owns one 300 s transaction for the whole run. A dataset of
slow cases could therefore be killed at the ceiling with every completed verdict
rolled back. The runner now stops judging at a strictly lower budget, records
every unprocessed definition as ``error``, and terminalises honestly.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.execute_suite_run import (
    SUITE_JUDGING_BUDGET_SECONDS,
    SuiteJudgingBudgetExhaustedError,
    _budget_exhausted,
    _case_results_async,
    _evaluate_within_budget,
    _exhausted_case_results,
    _resolve_judging_budget,
    _run_suite_cases,
)


def _definition() -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="judge",
        eval_type=EvalType.MODEL_JUDGE,
        config={
            "answer_contract": "typed_questions",
            "field": "output",
            "questions": [{"id": "q", "type": "noul", "weight": 1.0}],
            "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
        },
        failure_behaviour="warn",
    )


class _HangingJudge:
    async def ajudge(self, output: dict[str, Any], eval_def: Any) -> Any:
        import asyncio

        await asyncio.sleep(30)
        return {"outcome": "pass", "score": 1.0}  # pragma: no cover - never reached


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


class TestBudgetConstant:
    def test_the_budget_is_strictly_below_the_saq_job_ceiling(self) -> None:
        assert 0 < SUITE_JUDGING_BUDGET_SECONDS < 300


class TestResolveBudget:
    def test_an_explicit_budget_wins(self) -> None:
        run = SimpleNamespace(extra={"judging_budget_seconds": 10})

        assert _resolve_judging_budget(run, 20) == 20

    def test_run_extra_is_used_when_no_explicit_budget_is_given(self) -> None:
        run = SimpleNamespace(extra={"judging_budget_seconds": "12.5"})

        assert _resolve_judging_budget(run, None) == 12.5

    @pytest.mark.parametrize("raw", [None, "nope", 0, -1, float("nan"), float("inf")])
    def test_invalid_overrides_fall_back_to_the_default(self, raw: Any) -> None:
        run = SimpleNamespace(extra={"judging_budget_seconds": raw})

        assert _resolve_judging_budget(run, None) == SUITE_JUDGING_BUDGET_SECONDS

    def test_an_override_cannot_exceed_the_ceiling(self) -> None:
        run = SimpleNamespace(extra={"judging_budget_seconds": 10_000})

        assert _resolve_judging_budget(run, None) == SUITE_JUDGING_BUDGET_SECONDS


class TestBudgetChecks:
    def test_no_deadline_never_exhausts(self) -> None:
        assert _budget_exhausted(None) is False

    def test_a_past_deadline_is_exhausted(self) -> None:
        assert _budget_exhausted(time.monotonic() - 1) is True

    def test_a_future_deadline_is_not_exhausted(self) -> None:
        assert _budget_exhausted(time.monotonic() + 60) is False


class TestExhaustedRows:
    def test_one_error_row_per_definition(self) -> None:
        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4())
        definitions = [_definition(), _definition()]

        results, errored = _exhausted_case_results(run, definitions, eval_definition_version=3)

        assert len(results) == 2
        assert errored == 2
        assert all(row.outcome == "error" for row in results)
        assert all(row.error_code == "judge.timeout" for row in results)
        assert all(row.eval_definition_version == 3 for row in results)


class TestCaseCutOff:
    async def test_a_case_started_before_the_deadline_is_cut_off(self) -> None:
        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4())
        case = SimpleNamespace(id=uuid.uuid4(), input_payload={"content": "x"})

        results, errored = await _case_results_async(
            run,  # type: ignore[arg-type]
            case,  # type: ignore[arg-type]
            [_definition()],
            EvalEngine(),
            _HangingJudge(),
            None,
            eval_definition_version=1,
            judging_deadline=time.monotonic() + 0.05,
        )

        assert errored == 1
        assert results[0].outcome == "error"
        assert results[0].error_code == "judge.timeout"

    async def test_a_spent_deadline_before_the_call_raises_budget_exhausted(self) -> None:
        run = SimpleNamespace(id=uuid.uuid4())
        definition = _definition()

        with pytest.raises(SuiteJudgingBudgetExhaustedError):
            await _evaluate_within_budget(
                EvalEngine(),
                {"output": "x"},
                definition,
                run,  # type: ignore[arg-type]
                _HangingJudge(),
                time.monotonic() - 1,
            )


class TestRunLoop:
    async def test_an_exhausted_run_records_every_case_and_claims_no_cost(self) -> None:
        session = _RecordingSession()
        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4(), claimed_cost=None)
        case = SimpleNamespace(id=uuid.uuid4(), input_payload={"content": "x"})
        definitions = [_definition(), _definition()]

        passed, failed, excluded = await _run_suite_cases(
            session,  # type: ignore[arg-type]
            run,  # type: ignore[arg-type]
            cases=[case, case],  # type: ignore[list-item]
            suite_defs=definitions,
            engine=EvalEngine(),
            model_judge_callable=_HangingJudge(),
            scenario_inputs=None,
            eval_definition_version=1,
            claims_llm=True,
            suite_ceiling=None,
            cost_per_llm_case=0,  # type: ignore[arg-type]
            judging_deadline=time.monotonic() - 1,
        )

        assert (passed, failed, excluded) == (0, 0, 2)
        assert len(session.added) == 4
        assert all(row.outcome == "error" for row in session.added)
        assert run.claimed_cost is None, "an unjudged case must not be charged"
