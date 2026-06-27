"""Unit tests for input truncation in pipeline engine."""

import pytest

from modulo.core.pipeline_engine.input_truncation import truncate_input


class TestTruncateInput:
    def test_no_limit_passes_through(self) -> None:
        text = "Hello, world!" * 100
        result = truncate_input(text, None)
        assert result == text

    def test_under_limit_passes_through(self) -> None:
        text = "Short input"
        result = truncate_input(text, 1000)
        assert result == text

    def test_exact_limit_passes_through(self) -> None:
        text = "Exact length"
        result = truncate_input(text, len(text))
        assert result == text

    def test_over_limit_truncates_and_appends_notice(self) -> None:
        text = "A" * 100
        result = truncate_input(text, 20)
        assert result == "AAAAAAAAAAAAAAAAAAAA\n\n[Input truncated to 20 characters]"
        assert len(result) > 20

    def test_truncated_input_is_exactly_max_length(self) -> None:
        text = "X" * 50
        result = truncate_input(text, 10)
        truncated_part = result.split("\n\n")[0]
        assert len(truncated_part) == 10

    def test_notice_contains_max_length_value(self) -> None:
        text = "B" * 100
        result = truncate_input(text, 30)
        assert "[Input truncated to 30 characters]" in result

    def test_zero_length_limit(self) -> None:
        text = "Any text"
        result = truncate_input(text, 0)
        assert result.startswith("\n\n[Input truncated to 0 characters]")

    def test_empty_string_with_limit(self) -> None:
        text = ""
        result = truncate_input(text, 10)
        assert result == ""


@pytest.mark.asyncio
async def test_node_runner_truncates_input_in_state() -> None:
    """Verify that make_node_fn truncates run_context.input when max_input_length is set."""
    from modulo.core.pipeline_engine.node_runner import make_node_fn

    long_input = "A" * 200
    node_def = {"id": "test-node-id"}
    node_fn = make_node_fn(node_def, max_input_length=10)

    state = {
        "run_context": {
            "input": long_input,
        },
        "artifacts": [],
    }

    result = await node_fn(state)
    assert result["artifacts"][0]["status"] == "executed"
    # The state's run_context.input should have been truncated by the node function.
    # Since the function mutates state in-place, we check the original state dict.
    truncated = state["run_context"]["input"]
    assert truncated == "AAAAAAAAAA\n\n[Input truncated to 10 characters]"


@pytest.mark.asyncio
async def test_node_runner_passes_through_without_limit() -> None:
    """Verify that make_node_fn does NOT truncate when max_input_length is None."""
    from modulo.core.pipeline_engine.node_runner import make_node_fn

    long_input = "B" * 500
    node_def = {"id": "test-node-id"}
    node_fn = make_node_fn(node_def)

    state = {
        "run_context": {
            "input": long_input,
        },
        "artifacts": [],
    }

    result = await node_fn(state)
    assert result["artifacts"][0]["status"] == "executed"
    # Input should be unchanged.
    assert state["run_context"]["input"] == long_input
