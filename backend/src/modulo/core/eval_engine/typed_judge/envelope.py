"""Typed judge verdict contract (ADR 066 D5, D6, D11).

The single verdict shape every live judge call site produces and every
persistence boundary consumes. Its central invariant is that **a verdict is
never inferred from an absence**:

* ``outcome`` has three values — ``pass``, ``fail``, ``error``.
* ``error`` means *no verdict was produced*. ``score`` is ``None`` (never
  ``0.0``), ``error_code`` is set, and the legacy ``passed`` projection is
  ``False`` so the policy gate keeps failing closed (D6: the gate vocabulary
  is deliberately unchanged).
* ``pass`` / ``fail`` carry no ``error_code``.

``passed`` on the engine/persistence DTO is a *compatibility projection* of
``outcome``: it is derived, never authoritative. This module owns that
derivation (:meth:`JudgeVerdict.passed`).

Error codes use the run error taxonomy's dotted vocabulary (ADR 042 / the
``error_codes`` registry); ``provider.*`` values are reused verbatim and the
``judge.*`` sub-namespace names judge-instrument failures. Do not invent a
second spelling of a failure that the run taxonomy already names.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Version of the persisted ``judge_result_json`` envelope. Bump when a field
#: gains a meaning incompatible with an older reader.
JUDGE_VERDICT_SCHEMA_VERSION = 1


class JudgeOutcome(StrEnum):
    """The verdict's terminal state. ``ERROR`` is *not* a failing verdict."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class JudgeConfidenceKind(StrEnum):
    """Provenance of a confidence value (ADR 066 §0 — never collapsed).

    Values of different kinds are never averaged, banded together or compared.
    """

    VENDOR_CLAIMED_CALIBRATED = "vendor_claimed_calibrated"
    MODEL_SELF_REPORTED = "model_self_reported"
    UNAVAILABLE = "unavailable"


class JudgeErrorCode(StrEnum):
    """Judge sub-namespace of the run error taxonomy.

    ``provider.*`` entries reuse the canonical run-taxonomy codes verbatim
    (a provider auth/billing refusal is the same failure whether it happens in
    a node or in a judge). The ``judge.*`` entries name instrument failures
    that only a judge can produce.
    """

    PROVIDER_AUTHENTICATION = "provider.authentication"
    PROVIDER_BILLING = "provider.billing"
    PROVIDER_UNAVAILABLE = "provider.unavailable"
    JUDGE_TIMEOUT = "judge.timeout"
    JUDGE_MALFORMED_RESPONSE = "judge.malformed_response"
    JUDGE_UNAVAILABLE = "judge.unavailable"
    JUDGE_CONFIGURATION = "judge.configuration"


class JudgeExecutionError(RuntimeError):
    """A judge call produced no verdict, with a coded reason.

    Raised by judge callables (the model-backend bridge, the suite dispatcher,
    the guardrail revalidator) so the engine can persist an ``error`` outcome
    instead of misreading the absence as a failing verdict.
    """

    def __init__(
        self,
        code: JudgeErrorCode,
        detail: str,
        *,
        judge: JudgeIdentity | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        # The resolved judge identity travels with the failure so an ``error``
        # verdict keeps its provenance (D15), rather than losing it at the
        # moment the verdict could not be produced.
        self.judge = judge

    def as_verdict(self, *, judge: JudgeIdentity | None = None) -> JudgeVerdict:
        return error_verdict(self.code, self.detail, judge=judge or self.judge)


class JudgeIdentity(BaseModel):
    """Who answered. Recorded on the verdict (ADR 066 D5/D15)."""

    model_config = ConfigDict(extra="forbid")

    mechanism: str = "model_backend"
    judge_backend_id: str | None = None
    vendor: str | None = None
    model: str | None = None


class QuestionVerdict(BaseModel):
    """One question's answer inside a verdict envelope.

    The per-question detail is a structured record, never the raw payload: it
    names what was asked and how it was answered.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    answer: str | int | float | bool | None = None
    passed: bool | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: str = ""


class JudgeVerdict(BaseModel):
    """The schema-versioned verdict envelope persisted on an eval result."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = JUDGE_VERDICT_SCHEMA_VERSION
    outcome: JudgeOutcome
    score: float | None = None
    detail: str = ""
    questions: list[QuestionVerdict] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_kind: JudgeConfidenceKind = JudgeConfidenceKind.UNAVAILABLE
    judge: JudgeIdentity = Field(default_factory=JudgeIdentity)
    escalated: bool = False
    error_code: JudgeErrorCode | None = None

    @model_validator(mode="after")
    def _validate_error_state(self) -> JudgeVerdict:
        """Enforce the error/pass/fail invariants at construction time."""
        if self.outcome is JudgeOutcome.ERROR:
            if self.error_code is None:
                raise ValueError("an error outcome must carry an error_code")
            if self.score is not None:
                raise ValueError("an error outcome must not carry a score (use None, never 0.0)")
        elif self.error_code is not None:
            raise ValueError("only an error outcome may carry an error_code")
        return self

    @property
    def passed(self) -> bool:
        """The compatibility projection. ``error`` projects to ``False`` (fails closed)."""
        return self.outcome is JudgeOutcome.PASS

    def to_json(self) -> dict[str, Any]:
        """Serialise for the ``judge_result_json`` column (JSON-safe values)."""
        return self.model_dump(mode="json")


def error_verdict(
    code: JudgeErrorCode,
    detail: str,
    *,
    judge: JudgeIdentity | None = None,
) -> JudgeVerdict:
    """Build the verdict for a judge that produced no verdict."""
    return JudgeVerdict(
        outcome=JudgeOutcome.ERROR,
        score=None,
        detail=detail,
        error_code=code,
        judge=judge or JudgeIdentity(),
        confidence_kind=JudgeConfidenceKind.UNAVAILABLE,
    )


def _finite_float(
    raw: Any,
    *,
    field: str,
    code: JudgeErrorCode = JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
) -> float | None:
    """Coerce a numeric field, rejecting non-finite values (NaN/Infinity).

    ``json.loads`` accepts ``NaN``/``Infinity`` by default, and a NaN compares
    False against every bound, so it can silently disable a threshold or a
    veto. Non-finite numbers are malformed, never a verdict.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise JudgeExecutionError(code, f"{field} is not numeric") from exc
    if not math.isfinite(value):
        raise JudgeExecutionError(code, f"{field} is not finite")
    return value


def _coerce_score(raw: Any) -> float | None:
    """Legacy score coercion: a non-number is dropped (pre-existing behaviour),
    but a NON-FINITE number is malformed (NaN would otherwise clear every
    threshold/veto comparison)."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            "judge returned a non-finite score",
        )
    return value


def _reject_json_constant(value: str) -> Any:
    """``json.loads(parse_constant=...)`` hook: NaN/Infinity are malformed."""
    raise JudgeExecutionError(
        JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
        f"judge returned the non-finite JSON constant {value}",
    )


def _coerce_questions(raw: Any) -> list[QuestionVerdict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            "judge returned a non-list 'questions' field",
        )
    try:
        return [QuestionVerdict.model_validate(item) for item in raw]
    except (TypeError, ValueError) as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned an invalid question verdict: {type(exc).__name__}",
        ) from exc


def _coerce_judge_identity(raw: dict[str, Any], judge: JudgeIdentity | None) -> JudgeIdentity:
    """Resolve the verdict's judge identity, preferring the caller-supplied one."""
    if judge is not None:
        return judge
    raw_identity = raw.get("judge")
    if not isinstance(raw_identity, dict):
        return JudgeIdentity()
    try:
        return JudgeIdentity.model_validate(raw_identity)
    except ValueError as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned an invalid identity: {type(exc).__name__}",
        ) from exc


def _coerce_outcome(raw: Any) -> JudgeOutcome:
    try:
        return JudgeOutcome(str(raw or "error"))
    except ValueError as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned an unknown outcome: {raw!r}",
        ) from exc


def _coerce_error_code(raw: Any) -> JudgeErrorCode | None:
    if not raw:
        return None
    try:
        return JudgeErrorCode(str(raw))
    except ValueError as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned an unknown error_code: {raw!r}",
        ) from exc


def _coerce_confidence_kind(raw: Any, default: JudgeConfidenceKind) -> JudgeConfidenceKind:
    try:
        return JudgeConfidenceKind(str(raw or default))
    except ValueError as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned an unknown confidence_kind: {raw!r}",
        ) from exc


def _coerce_text(raw: Any) -> str:
    """Render an optional text field as a string (``None`` becomes ``""``)."""
    return "" if raw is None else str(raw)


def _build_typed_verdict(
    raw: dict[str, Any],
    *,
    identity: JudgeIdentity,
    default_confidence_kind: JudgeConfidenceKind,
) -> JudgeVerdict:
    """Build the verdict for the typed dialect (``outcome``/``error_code``)."""
    outcome = _coerce_outcome(raw.get("outcome"))
    error_code = _coerce_error_code(raw.get("error_code"))
    confidence_kind = _coerce_confidence_kind(raw.get("confidence_kind"), default_confidence_kind)
    try:
        return JudgeVerdict(
            outcome=outcome,
            score=_coerce_score(raw.get("score")),
            detail=_coerce_text(raw.get("detail")),
            questions=_coerce_questions(raw.get("questions")),
            confidence=_coerce_score(raw.get("confidence")),
            confidence_kind=confidence_kind,
            judge=identity,
            escalated=bool(raw.get("escalated", False)),
            error_code=error_code,
        )
    except ValueError as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned an invalid verdict envelope: {exc}",
        ) from exc


def _build_legacy_verdict(
    raw: dict[str, Any],
    *,
    identity: JudgeIdentity,
    default_confidence_kind: JudgeConfidenceKind,
) -> JudgeVerdict:
    """Build the verdict for the legacy dialect (``passed``/``score``/``detail``).

    The shape is unchanged, but ``passed`` must be a REAL boolean.
    ``bool("false")`` is True, so a small model (or an injected output steering
    it) could otherwise flip a gate.
    """
    raw_passed = raw.get("passed", False)
    if not isinstance(raw_passed, bool):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned a non-boolean 'passed' ({type(raw_passed).__name__})",
        )
    return JudgeVerdict(
        outcome=JudgeOutcome.PASS if raw_passed else JudgeOutcome.FAIL,
        score=_coerce_score(raw.get("score")),
        detail=_coerce_text(raw.get("detail")),
        judge=identity,
        confidence_kind=default_confidence_kind,
    )


def from_judge_callable_dict(
    raw: Any,
    *,
    judge: JudgeIdentity | None = None,
    default_confidence_kind: JudgeConfidenceKind = JudgeConfidenceKind.UNAVAILABLE,
) -> JudgeVerdict:
    """Normalise a judge callable's return value into a typed verdict.

    A judge callable returns a dict. The typed shape carries ``outcome`` (or an
    ``error_code``); the **legacy dialect** carries only ``passed``/``score``/
    ``detail`` and is preserved exactly (ADR 066 D8: legacy definitions keep
    today's behaviour until migrated).

    A non-dict return is a malformed response, never a failing verdict.
    """
    if not isinstance(raw, dict):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned {type(raw).__name__}, expected a dict",
        )

    identity = _coerce_judge_identity(raw, judge)
    if raw.get("outcome") is not None or raw.get("error_code") is not None:
        return _build_typed_verdict(
            raw,
            identity=identity,
            default_confidence_kind=default_confidence_kind,
        )
    return _build_legacy_verdict(
        raw,
        identity=identity,
        default_confidence_kind=default_confidence_kind,
    )


# ---------------------------------------------------------------------------
# Typed question aggregation (ADR 066 D4 / appendix "Derived quantities")
# ---------------------------------------------------------------------------


def _question_label(question: dict[str, Any]) -> str:
    """Return the question's id for error detail (``"?"`` when it has none)."""
    raw_qid = question.get("id")
    return str(raw_qid) if raw_qid is not None else "?"


def _binary_score(passed: bool) -> float:
    return 1.0 if passed else 0.0


def _noul_answer(qid: str, answer: Any) -> tuple[bool, float]:
    """Score a ``noul`` answer.

    NEVER truthiness: a model returning the STRING "false" (or "no", "False",
    "0") would otherwise flip a gate to pass. A ``noul`` answer is a real
    boolean or the response is malformed.
    """
    if not isinstance(answer, bool):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"question {qid!r} expects a boolean answer, got {type(answer).__name__}",
        )
    return answer, _binary_score(answer)


def _choice_passing_keys(question: dict[str, Any], qid: str) -> list[Any]:
    passing_keys = question.get("passing_keys") or []
    if not isinstance(passing_keys, list):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            f"question {qid!r} has a non-list 'passing_keys'",
        )
    return passing_keys


def _choice_option_score(
    question: dict[str, Any],
    qid: str,
    answer: Any,
    passed: bool,
) -> float:
    option_scores = question.get("option_scores") or {}
    if isinstance(option_scores, dict) and answer in option_scores:
        option_score = _finite_float(
            option_scores[answer],
            field=f"question {qid!r} option score",
            code=JudgeErrorCode.JUDGE_CONFIGURATION,
        )
        if option_score is not None:
            return option_score
    return _binary_score(passed)


def _choice_answer(question: dict[str, Any], qid: str, answer: Any) -> tuple[bool, float]:
    passing_keys = _choice_passing_keys(question, qid)
    passed = answer in passing_keys
    return passed, _choice_option_score(question, qid, answer, passed)


def _numeric_level_scores(question: dict[str, Any], qid: str) -> list[float]:
    levels = question.get("levels") or []
    numeric: list[float] = []
    for level in levels:
        if not isinstance(level, dict):
            continue
        level_score = _finite_float(
            level.get("score"),
            field=f"question {qid!r} level score",
            code=JudgeErrorCode.JUDGE_CONFIGURATION,
        )
        if level_score is not None:
            numeric.append(level_score)
    return numeric


def _score_bounds(question: dict[str, Any], qid: str) -> tuple[float, float]:
    numeric = _numeric_level_scores(question, qid)
    if len(numeric) < 2:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            f"score question {qid!r} declares fewer than two ordered levels",
        )
    return min(numeric), max(numeric)


def _required_pass_from(question: dict[str, Any], qid: str) -> float:
    pass_from = _finite_float(
        question.get("pass_from"),
        field=f"question {qid!r} pass_from",
        code=JudgeErrorCode.JUDGE_CONFIGURATION,
    )
    if pass_from is None:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            f"score question {qid!r} must declare pass_from",
        )
    return pass_from


def _normalise_level(value: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _score_answer(question: dict[str, Any], qid: str, answer: Any) -> tuple[bool, float]:
    lo, hi = _score_bounds(question, qid)
    value = _finite_float(answer, field=f"question {qid!r} answer")
    if value is None:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"question {qid!r} expects a numeric level",
        )
    pass_from = _required_pass_from(question, qid)
    return value >= pass_from, _normalise_level(value, lo, hi)


def _question_answer_score(question: dict[str, Any], answer: Any) -> tuple[bool, float]:
    """Return ``(passed, score)`` for one typed question's answer."""
    qid = _question_label(question)
    qtype = question.get("type")
    if qtype == "noul":
        return _noul_answer(qid, answer)
    if qtype == "choice":
        return _choice_answer(question, qid, answer)
    if qtype == "score":
        return _score_answer(question, qid, answer)
    raise JudgeExecutionError(
        JudgeErrorCode.JUDGE_CONFIGURATION,
        f"question {qid!r} has an unsupported type {qtype!r}",
    )


@dataclass(frozen=True)
class _QuestionAggregate:
    """One question's verdict plus its contribution to the weighted mean."""

    verdict: QuestionVerdict
    vetoed: bool
    weighted_total: float
    weight_sum: float


def _pass_threshold(pass_rule: dict[str, Any] | None) -> float:
    raw_threshold = (pass_rule or {}).get("threshold")
    if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float)):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            "pass_rule.threshold is required and must be a number in [0, 1]; "
            "a missing threshold would make every aggregate pass",
        )
    threshold = float(raw_threshold)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            f"pass_rule.threshold must be a finite number in [0, 1] (got {threshold})",
        )
    return threshold


def _question_weight(question: dict[str, Any], qid: str) -> float:
    weight = _finite_float(
        question.get("weight", 1.0),
        field=f"question {qid!r} weight",
        code=JudgeErrorCode.JUDGE_CONFIGURATION,
    )
    if weight is None:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            f"question {qid!r} weight is required",
        )
    if weight < 0:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            f"question {qid!r} weight must be non-negative",
        )
    return weight


def _answer_verdict(
    question: dict[str, Any],
    qid: str,
    answer: Any,
    passed: bool,
    qscore: float,
) -> QuestionVerdict:
    return QuestionVerdict(
        question_id=qid,
        answer=answer if isinstance(answer, (str, int, float, bool)) else None,
        passed=passed,
        confidence=qscore,
        detail="" if passed else str(question.get("name") or question.get("instructions") or ""),
    )


def _aggregate_question(question: dict[str, Any], answers: dict[str, Any]) -> _QuestionAggregate:
    """Score one question and fold it into the aggregate inputs."""
    qid = str(question.get("id", ""))
    if not qid:
        raise JudgeExecutionError(JudgeErrorCode.JUDGE_CONFIGURATION, "a typed question has no id")
    if qid not in answers:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge did not answer question {qid!r}",
        )
    answer = answers[qid]
    passed, qscore = _question_answer_score(question, answer)
    verdict = _answer_verdict(question, qid, answer, passed, qscore)
    if bool(question.get("required", False)):
        # D4: required questions gate; they never contribute to the mean.
        return _QuestionAggregate(verdict=verdict, vetoed=not passed, weighted_total=0.0, weight_sum=0.0)
    weight = _question_weight(question, qid)
    return _QuestionAggregate(
        verdict=verdict,
        vetoed=False,
        weighted_total=qscore * weight,
        weight_sum=weight,
    )


def _aggregate_passed(vetoed: bool, aggregate: float | None, threshold: float) -> bool:
    if vetoed:
        return False
    if aggregate is None:
        # All-required set: every required question passed (none vetoed).
        return True
    return aggregate >= threshold


def aggregate_typed_answers(
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
    pass_rule: dict[str, Any] | None = None,
) -> tuple[list[QuestionVerdict], float | None, bool]:
    """Aggregate per-question answers into ``(verdicts, score, passed)``.

    ADR 066 D4: ``aggregate`` is the weighted mean over **non-required**
    questions with ``weight > 0``. A ``required`` question that FAILS vetoes the
    verdict, full stop — its normalised score is never compared to a
    ``min_pass_confidence`` (a score below ``pass_from`` can still normalise
    above such a threshold, and NaN would clear every bound). Required
    questions are excluded from the mean; when there are NO non-required
    questions the verdict is the required set alone (pass iff none vetoed). A
    missing answer is a malformed response, never an empty judgment.
    """
    threshold = _pass_threshold(pass_rule)
    verdicts: list[QuestionVerdict] = []
    weighted_total = 0.0
    weight_sum = 0.0
    vetoed = False
    for question in questions:
        item = _aggregate_question(question, answers)
        verdicts.append(item.verdict)
        vetoed = vetoed or item.vetoed
        weighted_total += item.weighted_total
        weight_sum += item.weight_sum
    aggregate = (weighted_total / weight_sum) if weight_sum > 0 else None
    return verdicts, aggregate, _aggregate_passed(vetoed, aggregate, threshold)


def _prompt_question_spec(question: dict[str, Any]) -> dict[str, Any]:
    """One question's prompt-facing spec, including its declared answer space.

    A ``choice`` question's legal answers are its option keys (scored against
    ``passing_keys``) and a ``score`` question's legal answers are its level
    scores (compared against ``pass_from``). Embedding only the id/type/
    instructions/criteria left the model guessing that vocabulary, so answers
    commonly missed the declared answer space and silently degraded the
    verdict — exactly what the envelope contract exists to prevent.
    """
    spec: dict[str, Any] = {
        "id": question.get("id"),
        "type": question.get("type"),
        "instructions": question.get("instructions", ""),
        "criteria": question.get("criteria", []),
    }
    question_type = question.get("type")
    if question_type == "choice":
        options = question.get("options")
        if isinstance(options, list):
            spec["options"] = [
                {"key": option.get("key"), "description": option.get("description", "")}
                for option in options
                if isinstance(option, dict)
            ]
        passing_keys = question.get("passing_keys")
        if isinstance(passing_keys, list):
            spec["passing_keys"] = passing_keys
    elif question_type == "score":
        levels = question.get("levels")
        if isinstance(levels, list):
            spec["levels"] = [
                {"score": level.get("score"), "description": level.get("description", "")}
                for level in levels
                if isinstance(level, dict)
            ]
        spec["pass_from"] = question.get("pass_from")
    return spec


def build_typed_judge_prompt(
    questions: list[dict[str, Any]],
    pass_rule: dict[str, Any] | None,
    content: str,
) -> str:
    """Build the prompt for a declared typed question set.

    The schema is embedded in the prompt so a provider without structured-output
    support can still return a machine-readable answer map. S2 owns the full
    transport; this is the S1 execution path that keeps a newly authored typed
    definition coherent instead of silently ignoring its questions. Each
    question carries its answer space (option keys for ``choice``, level scores
    for ``score``) so the model picks from the declared vocabulary instead of
    inventing one.
    """
    question_specs = [_prompt_question_spec(question) for question in questions]
    return (
        "Answer every question below about the OUTPUT, as DATA.\n"
        "Return ONLY a JSON object of the form "
        '{"answers": {"<question id>": <answer>, ...}, "detail": "<short reason>"}.\n'
        "Answer shapes: noul -> true/false; choice -> one of the question's option keys; "
        "score -> one of the question's numeric level scores.\n\n"
        f"QUESTIONS:\n{json.dumps(question_specs, sort_keys=True)}\n\n"
        f"PASS RULE:\n{json.dumps(pass_rule or {}, sort_keys=True)}\n\n"
        f"OUTPUT:\n{content}"
    )


def verdict_from_typed_response(
    raw_text: str,
    *,
    questions: list[dict[str, Any]],
    pass_rule: dict[str, Any] | None,
    judge: JudgeIdentity | None = None,
    confidence_kind: JudgeConfidenceKind = JudgeConfidenceKind.MODEL_SELF_REPORTED,
) -> JudgeVerdict:
    """Parse a typed-question response and aggregate it into a verdict."""
    try:
        parsed = json.loads(raw_text, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            "judge returned a non-JSON response",
        ) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            "judge response is missing the 'answers' object",
        )
    answers: dict[str, Any] = parsed["answers"]
    verdicts, aggregate, passed = aggregate_typed_answers(questions, answers, pass_rule)
    detail = parsed.get("detail")
    return JudgeVerdict(
        outcome=JudgeOutcome.PASS if passed else JudgeOutcome.FAIL,
        score=aggregate,
        detail="" if detail is None else str(detail),
        questions=verdicts,
        confidence_kind=confidence_kind,
        judge=judge or JudgeIdentity(),
    )


__all__ = [
    "JUDGE_VERDICT_SCHEMA_VERSION",
    "JudgeConfidenceKind",
    "JudgeErrorCode",
    "JudgeExecutionError",
    "JudgeIdentity",
    "JudgeOutcome",
    "JudgeVerdict",
    "QuestionVerdict",
    "aggregate_typed_answers",
    "build_typed_judge_prompt",
    "error_verdict",
    "from_judge_callable_dict",
    "verdict_from_typed_response",
]
