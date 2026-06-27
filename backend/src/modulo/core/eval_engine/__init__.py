"""EvalEngine — evaluates node outputs against eval definitions.

Supports four eval types:
  - llm_judge      : LLM-as-judge via ModelBackendHub
  - regex          : pattern match against output field
  - json_schema    : validate output against JSON Schema
  - custom_function: call a user-defined function

Each eval has a configurable failure_behaviour (warn | block).
Blocked evals raise EvalBlockedError.
"""

import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import jsonschema  # type: ignore[import-untyped]
from pydantic import BaseModel

_log = logging.getLogger(__name__)


class EvalType(StrEnum):
    LLM_JUDGE = "llm_judge"
    REGEX = "regex"
    JSON_SCHEMA = "json_schema"
    CUSTOM_FUNCTION = "custom_function"


FailureBehaviour = Literal["warn", "block"]


class EvalDefinition(BaseModel):
    """Pydantic DTO — mirrors the DB model for in-memory evaluation."""

    id: UUID
    org_id: UUID
    pipeline_id: UUID | None = None
    node_id: str | None = None
    name: str
    eval_type: EvalType
    config: dict[str, Any] = {}
    failure_behaviour: FailureBehaviour = "warn"
    pass_threshold: float | None = None  # 0.0-1.0, minimum pass rate for the suite
    suite_id: str | None = None  # groups evals into suites
    created_at: datetime = datetime.now(UTC)


class EvalResult(BaseModel):
    """Pydantic DTO — the outcome of a single eval against an output."""

    id: UUID = uuid4()
    run_id: UUID
    node_id: str
    eval_id: UUID
    passed: bool
    score: float | None = None
    detail: str = ""
    evaluated_at: datetime = datetime.now(UTC)


class EvalBlockedError(RuntimeError):
    """Raised when an eval with failure_behaviour='block' fails."""

    def __init__(self, eval_name: str, detail: str) -> None:
        super().__init__(f"Eval {eval_name!r} blocked pipeline: {detail}")
        self.eval_name = eval_name
        self.detail = detail


class EvalSuiteBlockedError(RuntimeError):
    """Raised when an eval suite's aggregate score is below pass_threshold."""

    def __init__(self, suite_id: str, score: float, threshold: float) -> None:
        super().__init__(f"Eval suite {suite_id!r} blocked pipeline: score {score:.2f} < threshold {threshold:.2f}")
        self.suite_id = suite_id
        self.score = score
        self.threshold = threshold


class SuiteEvalResult(BaseModel):
    """Aggregate result for an eval suite."""

    suite_id: str
    total_evals: int
    passed_evals: int
    aggregate_score: float  # 0.0-1.0
    passed: bool
    blocking_failures: list[str]


class LLMJudgeCallable(Protocol):
    """Protocol for LLM judge callables."""

    def __call__(self, output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]: ...


def _result_from_dict(
    raw: dict[str, Any],
    run_id: UUID,
    node_id: str,
    eval_id: UUID,
) -> EvalResult:
    return EvalResult(
        run_id=run_id,
        node_id=node_id,
        eval_id=eval_id,
        passed=bool(raw["passed"]),
        score=float(raw["score"]) if raw.get("score") is not None else None,
        detail=str(raw.get("detail", "")),
    )


class EvalEngine:
    """Stateless engine — evaluates one output against one eval definition per call."""

    def evaluate(
        self,
        output: dict[str, Any],
        eval_def: EvalDefinition,
        *,
        llm_judge_callable: LLMJudgeCallable | None = None,
    ) -> EvalResult:
        """Run a single eval against *output*.

        Args:
            output: The node's output dict (may contain ``field`` per eval_def.config).
            eval_def: The eval definition (EvalDefinition DTO).
            llm_judge_callable: Required for ``llm_judge`` type. Should accept
                ``(output_dict, eval_def)`` and return a dict with keys
                ``passed`` (bool), ``score`` (float|None), ``detail`` (str).

        Returns:
            EvalResult with pass/fail outcome.

        Raises:
            EvalBlockedError: When eval fails and failure_behaviour == "block".
        """
        run_id = uuid4()
        match eval_def.eval_type:
            case EvalType.REGEX:
                result = self._evaluate_regex(output, eval_def, run_id)
            case EvalType.JSON_SCHEMA:
                result = self._evaluate_json_schema(output, eval_def, run_id)
            case EvalType.CUSTOM_FUNCTION:
                result = self._evaluate_custom(output, eval_def, run_id)
            case EvalType.LLM_JUDGE:
                result = self._evaluate_llm(output, eval_def, run_id, llm_judge_callable)
            case _:
                raise ValueError(f"Unknown eval type: {eval_def.eval_type}")

        if not result.passed and eval_def.failure_behaviour == "block":
            raise EvalBlockedError(eval_def.name, result.detail)

        if not result.passed and eval_def.failure_behaviour == "warn":
            _log.warning("eval.failed_warn", extra={"eval_name": eval_def.name, "detail": result.detail})

        return result

    def _evaluate_regex(
        self,
        output: dict[str, Any],
        eval_def: EvalDefinition,
        run_id: UUID,
    ) -> EvalResult:
        pattern_str = eval_def.config.get("pattern", "")
        if not pattern_str:
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=False,
                score=0.0,
                detail="Regex eval missing 'pattern' in config",
            )
        field = eval_def.config.get("field", "")
        if not field:
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=False,
                score=0.0,
                detail="Regex eval missing 'field' in config",
            )
        value = str(output.get(field, ""))
        passed = bool(re.search(pattern_str, value))
        return EvalResult(
            run_id=run_id,
            node_id=eval_def.node_id or "",
            eval_id=eval_def.id,
            passed=passed,
            score=1.0 if passed else 0.0,
            detail=f"regex {'matched' if passed else 'no match'}: /{pattern_str}/ on {field}",
        )

    def _evaluate_json_schema(
        self,
        output: dict[str, Any],
        eval_def: EvalDefinition,
        run_id: UUID,
    ) -> EvalResult:
        schema = eval_def.config.get("schema", {})
        field = eval_def.config.get("field", "")
        data = output.get(field, output) if field else output
        try:
            jsonschema.validate(data, schema)
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=True,
                score=1.0,
                detail="JSON Schema validation passed",
            )
        except jsonschema.ValidationError as e:
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=False,
                score=0.0,
                detail=f"JSON Schema validation failed: {e.message}",
            )

    def _evaluate_custom(
        self,
        output: dict[str, Any],
        eval_def: EvalDefinition,
        run_id: UUID,
    ) -> EvalResult:
        """Evaluate using a user-defined function.

        The function is looked up from the ``functions`` registry passed in
        ``eval_def.config["functions"]`` — a dict of name -> callable.
        The callable receives ``(output: dict, config: dict)`` and must return
        a dict with keys ``passed`` (bool), ``score`` (float|None), ``detail`` (str).
        """
        fn_name = eval_def.config.get("function", "")
        fn_registry: dict[str, Any] = eval_def.config.get("functions", {})
        fn = fn_registry.get(fn_name)
        if fn is None:
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=False,
                score=0.0,
                detail=f"Custom function {fn_name!r} not found in registry",
            )
        try:
            raw = fn(output, eval_def.config.get("function_config", {}))
            return _result_from_dict(raw, run_id, eval_def.node_id or "", eval_def.id)
        except Exception as exc:
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=False,
                score=0.0,
                detail=f"Custom function {fn_name!r} raised: {exc}",
            )

    def _evaluate_llm(
        self,
        output: dict[str, Any],
        eval_def: EvalDefinition,
        run_id: UUID,
        llm_judge_callable: LLMJudgeCallable | None,
    ) -> EvalResult:
        if llm_judge_callable is None:
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=False,
                score=0.0,
                detail="LLM judge callable not provided",
            )
        try:
            raw = llm_judge_callable(output, eval_def)
            return _result_from_dict(raw, run_id, eval_def.node_id or "", eval_def.id)
        except Exception as exc:
            return EvalResult(
                run_id=run_id,
                node_id=eval_def.node_id or "",
                eval_id=eval_def.id,
                passed=False,
                score=0.0,
                detail=f"LLM judge raised: {exc}",
            )

    # ------------------------------------------------------------------
    # Standalone evaluate() path for Feedback System (§8.20)
    # ------------------------------------------------------------------

    def standalone_evaluate(
        self,
        output: dict[str, Any],
        *,
        name: str = "standalone",
        eval_type: EvalType = EvalType.REGEX,
        config: dict[str, Any] | None = None,
        failure_behaviour: FailureBehaviour = "warn",
        org_id: UUID | None = None,
    ) -> EvalResult:
        """Evaluate an output without a persisted EvalDefinition.

        This is the entry point for the Feedback System (§8.20) which needs
        to run ad-hoc evals on human feedback responses without creating a
        persisted eval definition first.
        """
        eval_def = EvalDefinition(
            id=uuid4(),
            org_id=org_id or uuid4(),
            name=name,
            eval_type=eval_type,
            config=config or {},
            failure_behaviour=failure_behaviour,
        )
        return self.evaluate(output, eval_def)


def evaluate_suite(
    eval_results: Sequence[EvalResult],
    suite_id: str,
    pass_threshold: float | None,
) -> SuiteEvalResult:
    """Aggregate eval results for a suite and check against pass_threshold.

    Args:
        eval_results: Individual eval results belonging to this suite.
        suite_id: The suite identifier.
        pass_threshold: Minimum pass rate (0.0-1.0). If None, the suite
            never blocks but still returns an aggregate result.

    Returns:
        SuiteEvalResult with aggregate score and pass/fail decision.
    """
    total = len(eval_results)
    passed_evals = sum(1 for r in eval_results if r.passed)
    aggregate_score = passed_evals / total if total > 0 else 1.0
    blocking_failures = [f"{r.eval_id}: {r.detail}" for r in eval_results if not r.passed]

    suite_passed = True
    if pass_threshold is not None and total > 0:
        suite_passed = aggregate_score >= pass_threshold

    return SuiteEvalResult(
        suite_id=suite_id,
        total_evals=total,
        passed_evals=passed_evals,
        aggregate_score=aggregate_score,
        passed=suite_passed,
        blocking_failures=blocking_failures,
    )
