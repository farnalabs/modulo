"""Our own typed-question transport over a model backend (ADR 066 D9/D10).

One typed judge call asks **one question per backend call**, bounded in
concurrency, each with its own deadline, aggregated in code. The transport
owns everything the ADR assigns to us:

* **Output shape.** A provider that declares native structured output gets the
  answer JSON schema through ``output_schema``; every other provider (all
  local providers disable it — a constraint, not a bug) gets the schema
  embedded in the prompt. The same response shape is expected either way.
* **Corrective retries.** A malformed answer is retried with the rejection fed
  back into the prompt, a bounded number of times. A final failure is a coded
  ``judge.malformed_response`` — never a failing verdict.
* **Deadlines.** Per-question (``asyncio.wait_for`` per call) and
  per-definition (the whole batch), both from the definition's ``timeouts``.
* **Circuit breaker.** Repeated timeouts quarantine the backend so later calls
  fail fast instead of queueing behind a dead provider.
* **Usage capture.** Provider-reported token usage is recorded as provenance
  (D14), never as money.

Provider exceptions other than timeouts propagate to the caller, which owns the
run/judge error taxonomy mapping (``llm_judge._backend_failure``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from modulo.core.eval_engine.typed_judge.envelope import (
    JudgeConfidenceKind,
    JudgeErrorCode,
    JudgeExecutionError,
    JudgeIdentity,
    JudgeOutcome,
    JudgeVerdict,
    aggregate_typed_answers,
    build_question_prompt,
    derived_pass_confidence,
    split_answer_payload,
)

_log = logging.getLogger(__name__)

#: Judge calls run with bounded concurrency above one (ADR 066 D10): a question
#: set of N is judged in parallel batches, never strictly serially and never
#: unbounded.
MAX_CONCURRENCY = 4
#: Corrective retries WE own for a malformed answer (not a provider retry).
MAX_CORRECTIVE_RETRIES = 2
#: Consecutive timeouts before a backend is quarantined (ADR 066 appendix).
CIRCUIT_OPEN_AFTER_FAILURES = 3
#: How long a quarantined backend fails fast.
CIRCUIT_QUARANTINE_SECONDS = 60.0

#: ``(messages, output_schema) -> response``. ``output_schema`` is ``None`` for
#: the prompt-embedded path.
BackendInvoke = Callable[[list[Any], dict[str, Any] | None], Awaitable[Any]]


@dataclass(frozen=True)
class JudgeBackendHandle:
    """Everything the transport needs to talk to one resolved judge backend."""

    key: str
    invoke: BackendInvoke
    supports_native_structured_output: bool = False


class _CircuitBreaker:
    """Per-backend quarantine after consecutive timeouts (ADR 066 D10)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def is_quarantined(self, key: str) -> bool:
        with self._lock:
            until = self._blocked_until.get(key)
            if until is None:
                return False
            if time.monotonic() >= until:
                self._blocked_until.pop(key, None)
                self._consecutive.pop(key, None)
                return False
            return True

    def record_timeout(self, key: str) -> bool:
        """Count a timeout; return True when this one opened the breaker."""
        with self._lock:
            failures = self._consecutive.get(key, 0) + 1
            self._consecutive[key] = failures
            if failures < CIRCUIT_OPEN_AFTER_FAILURES:
                return False
            self._blocked_until[key] = time.monotonic() + CIRCUIT_QUARANTINE_SECONDS
            self._consecutive[key] = 0
            return True

    def record_success(self, key: str) -> None:
        with self._lock:
            self._consecutive.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._consecutive.clear()
            self._blocked_until.clear()


_BREAKER = _CircuitBreaker()


def reset_circuit_breaker() -> None:
    """Clear every quarantine (test isolation / operator recovery)."""
    _BREAKER.reset()


def _question_id(question: dict[str, Any]) -> str:
    """The question's id for error detail (``"?"`` when it has none)."""
    raw = question.get("id")
    return str(raw) if raw is not None else "?"


def _quarantine_error(key: str) -> JudgeExecutionError:
    return JudgeExecutionError(
        JudgeErrorCode.JUDGE_UNAVAILABLE,
        f"judge backend {key!r} is quarantined after repeated timeouts; "
        "no verdict was produced and later calls fail fast until the quarantine expires",
        judge=JudgeIdentity(mechanism="model_backend", judge_backend_id=key),
    )


def _question_answer_schema(question: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema for ONE question's answer payload.

    ``additionalProperties: false`` and an enumerated answer keep a provider's
    native structured-output decoder inside the declared vocabulary. Only
    ``answer`` is required — a provider that cannot emit a distribution still
    produces a usable verdict (its pass-confidence falls back to the pass
    indicator), while a provider that can gives us the real mass.
    """
    question_type = question.get("type")
    qid = _question_id(question)
    if question_type == "noul":
        answer_schema: dict[str, Any] = {"type": "boolean"}
        probability_keys = ["yes", "no"]
    elif question_type == "choice":
        keys = [str(option.get("key")) for option in question.get("options") or [] if isinstance(option, dict)]
        answer_schema = {"type": "string", "enum": keys}
        probability_keys = keys
    elif question_type == "score":
        keys = [
            str(level.get("score"))
            for level in question.get("levels") or []
            if isinstance(level, dict) and level.get("score") is not None
        ]
        answer_schema = {"type": "number", "enum": [float(key) for key in keys]}
        probability_keys = keys
    else:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            f"question {qid!r} has an unsupported type {question_type!r}",
        )
    return {
        "title": "TypedQuestionAnswer",
        "type": "object",
        "properties": {
            "answer": answer_schema,
            "probabilities": {
                "type": "object",
                "properties": {key: {"type": "number", "minimum": 0, "maximum": 1} for key in probability_keys},
                "additionalProperties": False,
            },
            "detail": {"type": "string"},
        },
        "required": ["answer"],
        "additionalProperties": False,
    }


def _response_text(response: Any) -> str:
    return response.content if hasattr(response, "content") else str(response)


def _reject_json_constant(value: str) -> Any:
    raise JudgeExecutionError(
        JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
        f"judge returned the non-finite JSON constant {value}",
    )


def _extract_payload(parsed: dict[str, Any], qid: str) -> Any:
    """Accept both the per-question shape and the S1 all-answers shape."""
    if "answers" in parsed:
        answers = parsed.get("answers")
        if not isinstance(answers, dict) or qid not in answers:
            raise JudgeExecutionError(
                JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
                f"judge response is missing an answer for question {qid!r}",
            )
        return answers[qid]
    return parsed


@dataclass(frozen=True)
class QuestionAnswer:
    """One parsed question answer, before aggregation."""

    answer: Any
    probabilities: Any
    detail: str


def parse_question_answer(raw_text: str, question: dict[str, Any]) -> QuestionAnswer:
    """Parse one question's response, coded malformed never inferred."""
    qid = _question_id(question)
    try:
        parsed = json.loads(raw_text, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            "judge returned a non-JSON response; no verdict was produced",
        ) from exc
    if not isinstance(parsed, dict):
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned a non-object JSON response for question {qid!r}; no verdict was produced",
        )
    payload = _extract_payload(parsed, qid)
    answer, probabilities = split_answer_payload(payload)
    if answer is None:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
            f"judge returned no answer for question {qid!r}; no verdict was produced",
        )
    raw_detail = parsed.get("detail")
    if isinstance(payload, dict) and payload.get("detail") is not None:
        raw_detail = payload.get("detail")
    return QuestionAnswer(
        answer=answer,
        probabilities=probabilities,
        detail="" if raw_detail is None else str(raw_detail),
    )


def _usage_from_response(response: Any) -> dict[str, Any] | None:
    """Extract provider-reported usage as provenance (never fabricated)."""
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        metadata = getattr(response, "response_metadata", None)
        usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return None
    cleaned = {
        key: int(value)
        for key, value in usage.items()
        if key in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens")
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }
    return cleaned or None


def _merge_usage(target: dict[str, Any], usage: dict[str, Any] | None) -> None:
    if not usage:
        return
    for key, value in usage.items():
        target[key] = int(target.get(key, 0)) + int(value)
    target["requests"] = int(target.get("requests", 0)) + 1


async def _invoke_question(
    handle: JudgeBackendHandle,
    question: dict[str, Any],
    content: str,
    per_question_seconds: float,
) -> tuple[QuestionAnswer, dict[str, Any] | None]:
    """Ask one question, with our own bounded corrective retries."""
    qid = _question_id(question)
    prompt = build_question_prompt(question, content)
    schema = _question_answer_schema(question) if handle.supports_native_structured_output else None
    last_error: JudgeExecutionError | None = None
    for attempt in range(MAX_CORRECTIVE_RETRIES + 1):
        try:
            response = await asyncio.wait_for(
                handle.invoke([_human_message(prompt)], schema),
                timeout=per_question_seconds,
            )
        except TimeoutError as exc:
            opened = _BREAKER.record_timeout(handle.key)
            _log.error(
                "judge.question_timeout",
                extra={
                    "backend": handle.key,
                    "question_id": qid,
                    "timeout_seconds": per_question_seconds,
                    "circuit_opened": opened,
                },
            )
            suffix = " (backend quarantined after repeated timeouts)" if opened else ""
            raise JudgeExecutionError(
                JudgeErrorCode.JUDGE_TIMEOUT,
                f"question {qid!r} timed out after {per_question_seconds:g}s{suffix}; no verdict was produced",
                judge=JudgeIdentity(mechanism="model_backend", judge_backend_id=handle.key),
            ) from exc
        try:
            parsed = parse_question_answer(_response_text(response), question)
        except JudgeExecutionError as exc:
            last_error = exc
            if exc.code is not JudgeErrorCode.JUDGE_MALFORMED_RESPONSE or attempt >= MAX_CORRECTIVE_RETRIES:
                raise
            prompt = (
                f"{prompt}\n\nYour previous response was rejected: {exc.detail}. "
                "Return ONLY the required JSON object for this question."
            )
            continue
        _BREAKER.record_success(handle.key)
        return parsed, _usage_from_response(response)
    # Unreachable: the loop returns or raises on its final attempt.
    raise last_error or JudgeExecutionError(
        JudgeErrorCode.JUDGE_MALFORMED_RESPONSE,
        f"question {qid!r} produced no usable answer",
    )


def _human_message(prompt: str) -> Any:
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=prompt)


def _aggregate(
    questions: list[dict[str, Any]],
    answers: list[QuestionAnswer],
    pass_rule: dict[str, Any] | None,
    *,
    identity: JudgeIdentity,
    confidence_kind: JudgeConfidenceKind,
    usage: dict[str, Any] | None,
) -> JudgeVerdict:
    answer_map = {
        str(question.get("id", "")): {"answer": answer.answer, "probabilities": answer.probabilities}
        for question, answer in zip(questions, answers, strict=True)
    }
    verdicts, aggregate, passed = aggregate_typed_answers(questions, answer_map, pass_rule)
    confidence = derived_pass_confidence(verdicts, questions)
    detail = "; ".join(answer.detail for answer in answers if answer.detail)
    return JudgeVerdict(
        outcome=JudgeOutcome.PASS if passed else JudgeOutcome.FAIL,
        score=aggregate,
        detail=detail[:2000],
        questions=verdicts,
        confidence=confidence,
        confidence_kind=confidence_kind,
        judge=identity,
        usage=usage,
    )


async def judge_questions(
    handle: JudgeBackendHandle,
    questions: list[dict[str, Any]],
    content: str,
    *,
    pass_rule: dict[str, Any] | None,
    identity: JudgeIdentity,
    per_question_seconds: float,
    per_definition_seconds: float,
    confidence_kind: JudgeConfidenceKind = JudgeConfidenceKind.MODEL_SELF_REPORTED,
    max_concurrency: int = MAX_CONCURRENCY,
) -> JudgeVerdict:
    """Judge a whole question set: bounded concurrency, deadlines, one verdict.

    Any question erroring raises (the caller records ``outcome=error``) — no
    partially judged verdict is ever produced. Sibling calls are cancelled when
    one fails.
    """
    if not questions:
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_CONFIGURATION,
            "typed judge definition has no questions",
        )
    if _BREAKER.is_quarantined(handle.key):
        raise _quarantine_error(handle.key)

    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _one(question: dict[str, Any]) -> tuple[QuestionAnswer, dict[str, Any] | None]:
        async with semaphore:
            return await _invoke_question(handle, question, content, per_question_seconds)

    async def _all() -> list[tuple[QuestionAnswer, dict[str, Any] | None]]:
        # ``return_exceptions`` keeps every bounded sibling call settling; the
        # first failure is then re-raised so no partially judged verdict can be
        # assembled from the survivors (ADR 066 D4).
        outcomes = await asyncio.gather(*(_one(question) for question in questions), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        return list(outcomes)  # type: ignore[arg-type]

    try:
        results = await asyncio.wait_for(_all(), timeout=per_definition_seconds)
    except TimeoutError as exc:
        opened = _BREAKER.record_timeout(handle.key)
        _log.error(
            "judge.definition_timeout",
            extra={
                "backend": handle.key,
                "timeout_seconds": per_definition_seconds,
                "circuit_opened": opened,
            },
        )
        raise JudgeExecutionError(
            JudgeErrorCode.JUDGE_TIMEOUT,
            f"typed judge definition timed out after {per_definition_seconds:g}s; no verdict was produced",
            judge=identity,
        ) from exc

    usage: dict[str, Any] = {}
    for _answer, question_usage in results:
        _merge_usage(usage, question_usage)
    return _aggregate(
        questions,
        [answer for answer, _ in results],
        pass_rule,
        identity=identity,
        confidence_kind=confidence_kind,
        usage=usage or None,
    )


__all__ = [
    "CIRCUIT_OPEN_AFTER_FAILURES",
    "CIRCUIT_QUARANTINE_SECONDS",
    "MAX_CONCURRENCY",
    "MAX_CORRECTIVE_RETRIES",
    "BackendInvoke",
    "JudgeBackendHandle",
    "QuestionAnswer",
    "judge_questions",
    "parse_question_answer",
    "reset_circuit_breaker",
]
