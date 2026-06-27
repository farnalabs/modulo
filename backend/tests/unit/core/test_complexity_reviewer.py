"""Unit tests for the complexity-reviewer canonical library primitive.

Verifies that the primitive definition is well-formed, that the
context-setter role allows run_context writes, and that downstream
nodes can read the written values.
"""

from typing import Any

import pytest

from modulo.core.library.complexity_reviewer import COMPLEXITY_REVIEWER
from modulo.core.pipeline_engine.decorator import (
    ContextSetterViolationError,
    cancellable_node,
)


class TestComplexityReviewerDefinition:
    """Tests that the primitive definition is well-formed."""

    def test_has_required_fields(self):
        """The COMPLEXITY_REVIEWER dict should have all required fields."""
        assert "name" in COMPLEXITY_REVIEWER
        assert "description" in COMPLEXITY_REVIEWER
        assert "prompt_template" in COMPLEXITY_REVIEWER
        assert "output_schema" in COMPLEXITY_REVIEWER
        assert "role" in COMPLEXITY_REVIEWER
        assert COMPLEXITY_REVIEWER["role"] == "context_setter"
        assert "run_context_writes" in COMPLEXITY_REVIEWER

    def test_output_schema_has_required_fields(self):
        """The output schema should include model_tier, estimated_tokens, complexity_reason."""
        schema = COMPLEXITY_REVIEWER["output_schema"]
        required = schema.get("required", [])
        assert "model_tier" in required
        assert "estimated_tokens" in required
        assert "complexity_reason" in required

    def test_run_context_writes_are_specified(self):
        """The documented run_context writes should match expected fields."""
        writes = COMPLEXITY_REVIEWER.get("run_context_writes", [])
        assert "model_tier" in writes
        assert "estimated_tokens" in writes
        assert "complexity_reason" in writes

    def test_model_tier_has_valid_enums(self):
        """model_tier should have valid enum values."""
        model_tier = COMPLEXITY_REVIEWER["output_schema"]["properties"]["model_tier"]
        enums = model_tier.get("enum", [])
        assert "tier-1" in enums
        assert "tier-2" in enums
        assert "tier-3" in enums

    def test_prompt_template_uses_artifact_placeholder(self):
        """The prompt template should reference the artifact placeholder."""
        template = COMPLEXITY_REVIEWER["prompt_template"]
        assert "{artifact}" in template

    def test_content_json_is_serializable(self):
        """The content_json should be JSON-serializable."""
        import json

        serialized = json.dumps(COMPLEXITY_REVIEWER)
        deserialized = json.loads(serialized)
        assert deserialized["name"] == "Complexity Reviewer"
        assert deserialized["role"] == "context_setter"


class TestContextSetterRole:
    """Tests that the context_setter role allows run_context writes."""

    @pytest.mark.asyncio
    async def test_context_setter_can_write_to_run_context(self):
        """A node with role='context_setter' should be allowed to write to run_context."""

        @cancellable_node(role="context_setter")
        async def context_setter_node(state: dict[str, Any]) -> dict[str, Any]:
            return {
                "run_context": {
                    "model_tier": "tier-2",
                    "estimated_tokens": 1500,
                    "complexity_reason": "Moderate complexity analysis required",
                }
            }

        result = await context_setter_node(
            {
                "run_context": {"cancelled": False, "input": {}},
            }
        )
        assert result["run_context"]["model_tier"] == "tier-2"
        assert result["run_context"]["estimated_tokens"] == 1500
        assert "Moderate" in result["run_context"]["complexity_reason"]

    @pytest.mark.asyncio
    async def test_non_context_setter_cannot_write_to_run_context(self):
        """A node without context_setter role should raise ContextSetterViolationError."""

        @cancellable_node(role="standard")
        async def standard_node(state: dict[str, Any]) -> dict[str, Any]:
            return {
                "run_context": {
                    "model_tier": "tier-2",
                }
            }

        with pytest.raises(ContextSetterViolationError) as exc_info:
            await standard_node(
                {
                    "run_context": {"cancelled": False, "input": {}},
                }
            )
        assert "context_setter" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_context_setter_without_run_context_ok(self):
        """A context_setter can return state without run_context."""

        @cancellable_node(role="context_setter")
        async def context_setter_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"artifacts": [{"status": "ok"}]}

        result = await context_setter_node(
            {
                "run_context": {"cancelled": False, "input": {}},
            }
        )
        assert len(result["artifacts"]) == 1

    @pytest.mark.asyncio
    async def test_complexity_reviewer_style_node(self):
        """A full style test mimicking the complexity reviewer's expected behaviour."""

        @cancellable_node(role="context_setter")
        async def complexity_reviewer(state: dict[str, Any]) -> dict[str, Any]:
            # Read artifact from state
            artifact = state.get("run_context", {}).get("input", {}).get("code", "")
            token_estimate = len(artifact) // 4  # Rough estimate

            return {
                "run_context": {
                    "model_tier": "tier-2" if token_estimate > 500 else "tier-1",
                    "estimated_tokens": token_estimate,
                    "complexity_reason": (
                        f"Input is {token_estimate} tokens, {'complex' if token_estimate > 500 else 'simple'} analysis"
                    ),
                }
            }

        result = await complexity_reviewer(
            {
                "run_context": {
                    "cancelled": False,
                    "input": {"code": "def hello(): pass"},
                },
            }
        )

        assert "model_tier" in result["run_context"]
        assert "estimated_tokens" in result["run_context"]
        assert "complexity_reason" in result["run_context"]
        assert result["run_context"]["model_tier"] == "tier-1"

    @pytest.mark.asyncio
    async def test_context_setter_cancellation_still_works(self):
        """Cancellation should work even for context_setter nodes."""

        @cancellable_node(role="context_setter")
        async def context_setter_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"run_context": {"model_tier": "tier-1"}}

        with pytest.raises(RuntimeError) as exc_info:
            await context_setter_node(
                {
                    "run_context": {"cancelled": True, "input": {}},
                }
            )
        assert "cancelled" in str(exc_info.value).lower()


class TestComplexityReviewerPrompt:
    """Tests for the prompt content itself."""

    def test_prompt_contains_instruction(self):
        """The prompt should instruct the LLM to analyse and output required fields."""
        prompt = COMPLEXITY_REVIEWER["prompt_template"]
        assert "model_tier" in prompt
        assert "estimated_tokens" in prompt
        assert "complexity_reason" in prompt
        assert "JSON" in prompt or "json" in prompt
