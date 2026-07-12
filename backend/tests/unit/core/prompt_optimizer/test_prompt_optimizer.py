"""Unit tests for PromptOptimizer."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from modulo.core.prompt_optimizer import (
    SYSTEM_PROMPT,
    OptimizationFailedError,
    OptimizationResult,
    PromptOptimizer,
    _build_failure_context,
    _parse_llm_response,
)

_AGENT_ID = uuid.uuid4()


def _sample_eval_results() -> list[dict]:
    return [
        {
            "id": str(uuid.uuid4()),
            "eval_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "passed": False,
            "score": 0.0,
            "detail": "Expected 'summary' field to contain at least 3 bullet points, found 1",
        },
        {
            "id": str(uuid.uuid4()),
            "eval_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "passed": False,
            "score": 0.3,
            "detail": "Output length exceeded 2000 tokens",
        },
    ]


def _sample_eval_defs() -> dict[str, dict]:
    return {
        str(eid): {
            "id": str(eid),
            "name": "Summary Quality Check",
            "eval_type": "regex",
            "config_json": {"pattern": r"\*\s+.{20,}", "field": "summary"},
            "failure_behaviour": "warn",
        }
        for eid in [uuid.uuid4(), uuid.uuid4()]
    }


class TestBuildFailureContext:
    def test_renders_current_prompt_and_failures(self) -> None:
        prompt = "You are an assistant. Answer: {{query}}"
        eval_id = str(uuid.uuid4())
        results = [
            {
                "id": str(uuid.uuid4()),
                "eval_id": eval_id,
                "run_id": str(uuid.uuid4()),
                "passed": False,
                "score": 0.0,
                "detail": "Expected more detail",
            }
        ]
        defs = {
            eval_id: {
                "id": eval_id,
                "name": "Summary Quality Check",
                "eval_type": "regex",
                "config_json": {},
                "failure_behaviour": "warn",
            }
        }

        context = _build_failure_context(prompt, results, defs)

        assert "<current_prompt>" in context
        assert prompt in context
        assert "<failing_evals>" in context
        assert "Summary Quality Check" in context
        assert "regex" in context
        assert json.loads(context.split("<failing_evals>\n")[1].split("\n</failing_evals>")[0])

    def test_handles_empty_results(self) -> None:
        context = _build_failure_context("prompt", [], {})
        assert "<failing_evals>" in context
        assert "[]" in context

    def test_handles_missing_eval_definitions(self) -> None:
        missing_id = str(uuid.uuid4())
        results = [
            {
                "id": str(uuid.uuid4()),
                "eval_id": missing_id,
                "passed": False,
                "score": 0.0,
                "detail": "fail",
            }
        ]
        context = _build_failure_context("prompt", results, {})
        assert "unknown" in context

    def test_handles_non_dict_eval_definitions(self) -> None:
        eval_id = str(uuid.uuid4())
        results = [
            {
                "id": str(uuid.uuid4()),
                "eval_id": eval_id,
                "passed": False,
                "score": 0.0,
                "detail": "fail",
            }
        ]
        defs = {eval_id: "not_a_dict"}
        context = _build_failure_context("prompt", results, defs)
        assert "unknown" in context


class TestParseLLMResponse:
    def test_parses_plain_json(self) -> None:
        raw = json.dumps({"analysis": "A", "suggested_prompt": "New prompt", "rationale": "R"})
        result = _parse_llm_response(raw)
        assert result.analysis == "A"
        assert result.suggested_prompt == "New prompt"
        assert result.rationale == "R"

    def test_parses_json_in_markdown_code_block_with_preamble(self) -> None:
        raw = """Here is my analysis:
```json
{
  "analysis": "Failure pattern: missing detail",
  "suggested_prompt": "You are a helpful assistant. Provide detailed answers.",
  "rationale": "Added detail instructions to address brevity failures"
}
```"""
        result = _parse_llm_response(raw)
        assert result.analysis == "Failure pattern: missing detail"
        assert "detailed answers" in result.suggested_prompt
        assert "brevity failures" in result.rationale

    def test_parses_json_in_markdown_without_lang(self) -> None:
        raw = """```
{"analysis": "test", "suggested_prompt": "hi", "rationale": "because"}
```"""
        result = _parse_llm_response(raw)
        assert result.analysis == "test"
        assert result.suggested_prompt == "hi"

    def test_raises_on_bad_json(self) -> None:
        with pytest.raises(OptimizationFailedError, match="not valid JSON"):
            _parse_llm_response("not json")

    def test_raises_on_missing_required_keys(self) -> None:
        with pytest.raises(OptimizationFailedError, match="must be a string"):
            _parse_llm_response(json.dumps({"analysis": "A"}))

    def test_raises_on_non_string_key(self) -> None:
        with pytest.raises(OptimizationFailedError, match="must be a string"):
            _parse_llm_response(json.dumps({"suggested_prompt": [], "rationale": "R", "analysis": "A"}))


class TestPromptOptimizer:
    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        llm = AsyncMock()
        llm.return_value = json.dumps(
            {
                "analysis": "Failures show brevity issue",
                "suggested_prompt": "You are helpful. Provide full answers. Query: {{query}}",
                "rationale": "Added detail requirement to fix brevity failures",
            }
        )
        return llm

    @pytest.fixture
    def optimizer(self, mock_llm: AsyncMock) -> PromptOptimizer:
        return PromptOptimizer(mock_llm)

    async def test_optimize_calls_llm_with_system_and_human_messages(
        self, optimizer: PromptOptimizer, mock_llm: AsyncMock
    ) -> None:
        results = _sample_eval_results()
        defs = _sample_eval_defs()

        await optimizer.optimize("Hello {{name}}", results, defs)

        mock_llm.assert_awaited_once()
        assert mock_llm.await_args is not None
        args = mock_llm.await_args[0][0]
        assert len(args) == 2
        assert isinstance(args[0], SystemMessage)
        assert args[0].content == SYSTEM_PROMPT
        assert isinstance(args[1], HumanMessage)
        assert "Hello {{name}}" in args[1].content

    async def test_optimize_returns_optimization_result(self, optimizer: PromptOptimizer) -> None:
        result = await optimizer.optimize(
            "Hello {{name}}",
            _sample_eval_results(),
            _sample_eval_defs(),
        )
        assert isinstance(result, OptimizationResult)
        assert result.suggested_prompt == "You are helpful. Provide full answers. Query: {{query}}"
        assert result.rationale == "Added detail requirement to fix brevity failures"
        assert result.analysis == "Failures show brevity issue"

    async def test_optimize_raises_optimization_failed_after_retry_exhaustion(self) -> None:
        mock = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        optimizer = PromptOptimizer(mock)

        with pytest.raises(OptimizationFailedError, match="after 3 attempts"):
            await optimizer.optimize("p", [], {})

    async def test_optimize_handles_empty_eval_results(self) -> None:
        mock = AsyncMock()
        mock.return_value = json.dumps(
            {
                "analysis": "No data",
                "suggested_prompt": "Hello {{name}}",
                "rationale": "No failures to analyse",
            }
        )
        optimizer = PromptOptimizer(mock)

        result = await optimizer.optimize("Hello {{name}}", [], {})
        assert result.suggested_prompt == "Hello {{name}}"
        assert result.rationale == "No failures to analyse"
