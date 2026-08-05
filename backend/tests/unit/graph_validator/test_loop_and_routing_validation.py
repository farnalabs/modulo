"""Unit tests for loop-edge and LLM-routing node validation.

Covers the error branches of ``_check_loop_edges`` (LOOP_* codes) and
``_check_llm_routing`` (LLM_ROUTING_* codes) that the topology suite does not
exercise: missing default targets, invalid max_iterations, invalid JMESPath
expressions, missing/duplicate routing labels, and missing default targets.
"""

from __future__ import annotations

from typing import Any

from modulo.core.graph_validator import GraphValidator, ValidationResult


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


# ---------------------------------------------------------------------------
# _check_loop_edges
# ---------------------------------------------------------------------------


def test_loop_edges_non_loop_edges_skipped():
    """Edges that are not type 'loop' are ignored entirely."""
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {"source": "a", "target": "b", "type": "normal"},
            {"source": "b", "target": "c", "type": "conditional"},
        ],
        {"a", "b", "c"},
        result,
    )
    assert result.is_valid
    assert not result.issues


def test_loop_edge_missing_default_target_is_error():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [{"source": "a", "target": "b", "type": "loop"}],
        {"a", "b"},
        result,
    )
    assert "LOOP_MISSING_DEFAULT_TARGET" in _codes(result)
    assert not result.is_valid


def test_loop_edge_missing_default_target_reports_source():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [{"source": "a", "target": "b", "type": "loop"}],
        {"a", "b"},
        result,
    )
    issue = next(i for i in result.issues if i.code == "LOOP_MISSING_DEFAULT_TARGET")
    assert issue.node_id == "a"
    assert "'a'" in issue.message


def test_loop_edge_unknown_default_target_is_error():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [{"source": "a", "target": "b", "type": "loop", "default_target": "ghost"}],
        {"a", "b"},
        result,
    )
    assert "LOOP_DEFAULT_TARGET_NOT_FOUND" in _codes(result)
    assert not result.is_valid


def test_loop_edge_valid_default_target_is_valid():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [{"source": "a", "target": "b", "type": "loop", "default_target": "a"}],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_loop_edge_negative_max_iterations_is_error():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "default_target": "a",
                "max_iterations": -1,
            }
        ],
        {"a", "b"},
        result,
    )
    assert "LOOP_INVALID_MAX_ITERATIONS" in _codes(result)


def test_loop_edge_boolean_max_iterations_is_error():
    """bool is not a valid max_iterations (it is an int subclass)."""
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "default_target": "a",
                "max_iterations": True,
            }
        ],
        {"a", "b"},
        result,
    )
    assert "LOOP_INVALID_MAX_ITERATIONS" in _codes(result)


def test_loop_edge_string_max_iterations_is_error():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "default_target": "a",
                "max_iterations": "5",
            }
        ],
        {"a", "b"},
        result,
    )
    assert "LOOP_INVALID_MAX_ITERATIONS" in _codes(result)


def test_loop_edge_zero_max_iterations_is_valid():
    """0 is a permitted (non-negative) iteration cap."""
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "default_target": "a",
                "max_iterations": 0,
            }
        ],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_loop_edge_invalid_condition_expression_is_error():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "default_target": "a",
                "condition_expression": "foo[",
            }
        ],
        {"a", "b"},
        result,
    )
    assert "LOOP_INVALID_EXPRESSION" in _codes(result)


def test_loop_edge_valid_condition_expression_is_valid():
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "default_target": "a",
                "condition_expression": "iterations",
            }
        ],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_loop_edge_non_string_condition_expression_skipped():
    """A non-string condition_expression is not compiled (no crash)."""
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "default_target": "a",
                "condition_expression": {"bad": "shape"},
            }
        ],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_loop_edge_multiple_issues_collected():
    """A malformed loop edge surfaces every problem at once."""
    result = ValidationResult()
    GraphValidator._check_loop_edges(
        [
            {
                "source": "a",
                "target": "b",
                "type": "loop",
                "max_iterations": -3,
                "condition_expression": "foo[",
            }
        ],
        {"a", "b"},
        result,
    )
    codes = _codes(result)
    assert {
        "LOOP_MISSING_DEFAULT_TARGET",
        "LOOP_INVALID_MAX_ITERATIONS",
        "LOOP_INVALID_EXPRESSION",
    }.issubset(codes)


# ---------------------------------------------------------------------------
# _check_llm_routing
# ---------------------------------------------------------------------------


def _llm_node(**overrides: Any) -> dict:
    node: dict[str, Any] = {
        "id": "a",
        "routing_mode": "llm",
        "routing_prompt": "route based on intent",
        "default_target": "b",
    }
    node.update(overrides)
    return node


def test_llm_routing_non_llm_nodes_skipped():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [{"id": "a", "routing_mode": "manual"}, {"id": "b"}],
        [],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_llm_routing_missing_prompt_is_error():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node(routing_prompt=None)],
        [],
        {"a", "b"},
        result,
    )
    assert "LLM_ROUTING_MISSING_PROMPT" in _codes(result)
    assert not result.is_valid


def test_llm_routing_whitespace_prompt_is_error():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node(routing_prompt="   ")],
        [],
        {"a", "b"},
        result,
    )
    assert "LLM_ROUTING_MISSING_PROMPT" in _codes(result)


def test_llm_routing_missing_label_is_error():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node()],
        [{"source": "a", "target": "b", "type": "conditional", "routing_label": ""}],
        {"a", "b"},
        result,
    )
    assert "LLM_ROUTING_MISSING_LABEL" in _codes(result)


def test_llm_routing_duplicate_label_is_error():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node()],
        [
            {"source": "a", "target": "b", "type": "conditional", "routing_label": "cont"},
            {"source": "a", "target": "c", "type": "conditional", "routing_label": "cont"},
        ],
        {"a", "b", "c"},
        result,
    )
    assert "LLM_ROUTING_DUPLICATE_LABEL" in _codes(result)


def test_llm_routing_reject_edges_skipped_for_label_check():
    """Reject edges do not require a routing_label."""
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node()],
        [{"source": "a", "target": "b", "type": "reject"}],
        {"a", "b"},
        result,
    )
    assert result.is_valid


def test_llm_routing_missing_default_target_is_error():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node(default_target=None)],
        [],
        {"a", "b"},
        result,
    )
    assert "LLM_ROUTING_MISSING_DEFAULT" in _codes(result)
    assert not result.is_valid


def test_llm_routing_unknown_default_target_is_error():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node(default_target="ghost")],
        [],
        {"a", "b"},
        result,
    )
    assert "LLM_ROUTING_DEFAULT_NOT_FOUND" in _codes(result)


def test_llm_routing_valid_config_is_valid():
    result = ValidationResult()
    GraphValidator._check_llm_routing(
        [_llm_node()],
        [
            {"source": "a", "target": "b", "type": "conditional", "routing_label": "cont"},
            {"source": "a", "target": "c", "type": "conditional", "routing_label": "stop"},
        ],
        {"a", "b", "c"},
        result,
    )
    assert result.is_valid
