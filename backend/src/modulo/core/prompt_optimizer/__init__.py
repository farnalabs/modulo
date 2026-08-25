"""PromptOptimizer — analyses eval failures and suggests prompt improvements via LLM."""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

__all__ = [
    "SYSTEM_PROMPT",
    "LLMCallable",
    "OptimizationFailedError",
    "OptimizationResult",
    "PromptOptimizer",
    "PromptOptimizerError",
]

_log = logging.getLogger(__name__)

_UNKNOWN_LABEL = "unknown"
_LLM_TIMEOUT = 60.0
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_BACKOFF_MULTIPLIER = 2.0
_RETRY_JITTER_FACTOR = 0.1

SYSTEM_PROMPT = """You are an expert prompt engineer. Your task is to analyse eval failures
for an AI agent prompt and suggest concrete improvements.

You will receive:
1. The current prompt template (with variables in {{mustache}} syntax)
2. A list of failing eval results, each with:
   - The eval definition name and type
   - Whether it passed/failed
   - The score
   - A detail message describing what went wrong
   - The eval's configuration

Analyse the failure patterns and produce a response in this exact JSON format:
{
  "analysis": "Brief summary of what patterns you see in the failures (2-3 sentences)",
  "suggested_prompt": "The improved prompt template (keep {{mustache}} variables)",
  "rationale": "Explanation of what changed and why, referencing specific eval failures"
}

Rules:
- Keep all original {{variable}} placeholders intact unless the failures indicate a schema issue
- The suggested_prompt should be a complete drop-in replacement
- Address the specific failure modes seen in the eval results
- Do NOT remove or add any {{variable}} placeholders
"""


class PromptOptimizerError(Exception):
    """Base exception for prompt optimizer errors."""


class OptimizationFailedError(PromptOptimizerError):
    """Raised when the LLM response cannot be parsed or the LLM call fails."""


@dataclass
class OptimizationResult:
    suggested_prompt: str
    rationale: str
    analysis: str


class LLMCallable(Protocol):
    async def __call__(self, messages: list[BaseMessage]) -> str: ...


def _ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            if isinstance(result, dict):
                return result
            _log.warning("JSON parsed value is not a dict: got %s", type(result).__name__)
            return {}
        except json.JSONDecodeError:
            _log.warning("Failed to parse JSON string as dict", exc_info=True)
            return {}
    _log.warning("Unexpected type for _ensure_dict: %s", type(value).__name__)
    return {}


def _build_failure_context(
    current_prompt: str,
    eval_results: list[dict[str, Any]],
    eval_definitions: dict[str, Any],
) -> str:
    failures = []
    for er in eval_results:
        if not isinstance(er, dict):
            _log.warning("Skipping non-dict eval result: %s", type(er).__name__)
            continue
        if er.get("passed"):
            continue
        eval_id = er.get("eval_id")
        eval_id_str = str(eval_id) if eval_id is not None else ""
        edef = eval_definitions.get(eval_id_str, {})
        if not isinstance(edef, dict):
            _log.warning("Skipping non-dict eval definition for %s: got %s", eval_id_str, type(edef).__name__)
            edef = {}
        failures.append(
            {
                "eval_name": edef.get("name", _UNKNOWN_LABEL),
                "eval_type": edef.get("eval_type", _UNKNOWN_LABEL),
                "passed": er.get("passed", False),
                "score": er.get("score"),
                "detail": er.get("detail", ""),
                "eval_config": _ensure_dict(edef.get("config_json", {})),
            }
        )

    if not failures:
        _log.warning(
            "No failing evals found in %d results; optimizer will have no failure data",
            len(eval_results),
        )

    return f"""<current_prompt>
{current_prompt}
</current_prompt>

<failing_evals>
{json.dumps(failures, indent=2, default=str)}
</failing_evals>"""


# Matches a fenced code block with optional json language tag
_CODE_FENCE_RE = re.compile(r"```\s*(?:json)?\s*\n?\s*(.*?)```", re.DOTALL)


def _parse_llm_response(raw: str) -> OptimizationResult:
    cleaned = raw.strip()
    if not cleaned:
        _log.error("LLM response is empty after stripping")
        raise OptimizationFailedError("Empty LLM response")

    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _CODE_FENCE_RE.search(cleaned)
        if match:
            extracted = match.group(1).strip()
            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError as exc:
                raise OptimizationFailedError(f"Failed to parse JSON from code-fenced response: {exc}") from exc
        else:
            raise OptimizationFailedError("LLM response is not valid JSON and contains no code-fenced block") from None

    if not isinstance(parsed, dict):
        _log.error("LLM response is valid JSON but not an object: got %s", type(parsed).__name__)
        raise OptimizationFailedError(f"LLM response is valid JSON but not an object: got {type(parsed).__name__}")

    suggested_prompt = parsed.get("suggested_prompt")
    rationale = parsed.get("rationale")
    if not isinstance(suggested_prompt, str):
        raise OptimizationFailedError(
            f"LLM response 'suggested_prompt' must be a string, got {type(suggested_prompt).__name__}"
        )
    if not isinstance(rationale, str):
        raise OptimizationFailedError(f"LLM response 'rationale' must be a string, got {type(rationale).__name__}")
    analysis = parsed.get("analysis")
    if not isinstance(analysis, str):
        analysis = ""
    return OptimizationResult(
        suggested_prompt=suggested_prompt,
        rationale=rationale,
        analysis=analysis,
    )


class PromptOptimizer:
    def __init__(
        self,
        llm_call: LLMCallable,
        system_prompt: str | None = None,
    ) -> None:
        if llm_call is None:
            raise ValueError("llm_call must not be None")
        if not callable(llm_call):
            raise ValueError("llm_call must be callable")
        self._llm_call = llm_call
        self._system_prompt = system_prompt or SYSTEM_PROMPT

    async def optimize(
        self,
        current_prompt: str,
        eval_results: list[dict[str, Any]] | None = None,
        eval_definitions: dict[str, Any] | None = None,
    ) -> OptimizationResult:
        if current_prompt is None:
            raise ValueError("current_prompt must not be None")
        if not current_prompt.strip():
            raise ValueError("current_prompt must not be empty or whitespace-only")
        if eval_results is None:
            eval_results = []
        if eval_definitions is None:
            eval_definitions = {}

        context = _build_failure_context(current_prompt, eval_results, eval_definitions)

        messages: list[BaseMessage] = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=context),
        ]

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                _log.info(
                    "Calling LLM for prompt optimization (attempt %d/%d)",
                    attempt + 1,
                    _MAX_RETRIES,
                )
                raw = await asyncio.wait_for(
                    self._llm_call(messages),
                    timeout=_LLM_TIMEOUT,
                )
                _log.info("LLM response received (%d chars)", len(raw))
                return _parse_llm_response(raw)
            except TimeoutError as exc:
                _log.warning(
                    "LLM call timed out after %ss (attempt %d/%d)",
                    _LLM_TIMEOUT,
                    attempt + 1,
                    _MAX_RETRIES,
                    exc_info=True,
                )
                last_exc = exc
            except OptimizationFailedError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.exception(
                    "LLM call failed (attempt %d/%d): %s",
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                    exc_info=True,
                )
                last_exc = exc

            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (_RETRY_BACKOFF_MULTIPLIER**attempt)
                jitter = delay * _RETRY_JITTER_FACTOR * (2 * random.random() - 1)  # noqa: S311  # NOSONAR S2245 — non-cryptographic retry-timing jitter only (no security impact)
                total_delay = delay + jitter
                _log.info("Retrying in %.1fs", total_delay)
                await asyncio.sleep(total_delay)

        _log.error("LLM call failed after %d attempts", _MAX_RETRIES)
        raise OptimizationFailedError(f"LLM call failed after {_MAX_RETRIES} attempts") from last_exc
