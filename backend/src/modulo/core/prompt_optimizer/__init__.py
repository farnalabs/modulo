"""PromptOptimizer — analyses eval failures and suggests prompt improvements via LLM."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

__all__ = [
    "SYSTEM_PROMPT",
    "LLMCallable",
    "OptimizationResult",
    "PromptOptimizer",
    "_build_failure_context",
    "_parse_llm_response",
]

_log = logging.getLogger(__name__)

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


@dataclass
class OptimizationResult:
    suggested_prompt: str
    rationale: str
    analysis: str


class LLMCallable(Protocol):
    async def __call__(self, messages: list[BaseMessage]) -> str: ...


def _build_failure_context(
    current_prompt: str,
    eval_results: list[dict[str, Any]],
    eval_definitions: dict[str, Any],
) -> str:
    failures = []
    for er in eval_results:
        if not isinstance(er, dict):
            continue
        eval_id = str(er.get("eval_id", ""))
        edef = eval_definitions.get(eval_id, {})
        failures.append(
            {
                "eval_name": edef.get("name", "unknown"),
                "eval_type": edef.get("eval_type", "unknown"),
                "passed": er.get("passed", False),
                "score": er.get("score"),
                "detail": er.get("detail", ""),
                "eval_config": edef.get("config_json", {}),
            }
        )

    return f"""<current_prompt>
{current_prompt}
</current_prompt>

<failing_evals>
{json.dumps(failures, indent=2)}
</failing_evals>"""


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _parse_llm_response(raw: str) -> OptimizationResult:
    cleaned = raw.strip()
    if not cleaned:
        raise json.JSONDecodeError("Empty LLM response", raw, 0)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _CODE_FENCE_RE.search(cleaned)
        if match:
            cleaned = match.group(1).strip()
            parsed = json.loads(cleaned)
        else:
            raise

    return OptimizationResult(
        suggested_prompt=parsed["suggested_prompt"],
        rationale=parsed["rationale"],
        analysis=parsed.get("analysis", ""),
    )


class PromptOptimizer:
    def __init__(self, llm_call: LLMCallable) -> None:
        self._llm_call = llm_call

    async def optimize(
        self,
        current_prompt: str,
        eval_results: list[dict[str, Any]],
        eval_definitions: dict[str, Any],
    ) -> OptimizationResult:
        context = _build_failure_context(current_prompt, eval_results, eval_definitions)

        messages: list[BaseMessage] = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=context),
        ]

        raw = await self._llm_call(messages)
        return _parse_llm_response(raw)
