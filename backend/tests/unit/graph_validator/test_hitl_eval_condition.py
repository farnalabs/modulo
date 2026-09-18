"""Unit tests for HITL gate eval-condition validation.

Covers ``_check_hitl_eval_condition`` (HITL_EVAL_CONDITION_* codes), which
validates the ``eval_condition`` block of a HITL gate edge.
"""

import uuid

from modulo.core.graph_validator import GraphValidator, ValidationResult


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


def _edge(**overrides) -> dict:
    edge: dict = {
        "source": str(uuid.uuid4()),
        "target": str(uuid.uuid4()),
        "type": "normal",
        "hitl_gate_config": {"eval_condition": {"eval_name": "quality", "threshold": 0.8, "operator": "gte"}},
    }
    edge.update(overrides)
    return edge


def _check(edge: dict) -> ValidationResult:
    result = ValidationResult()
    GraphValidator._check_hitl_eval_condition(edge, result)
    return result


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


def test_no_hitl_config_skipped():
    """Edges without hitl_gate_config are not checked."""
    result = _check(_edge(hitl_gate_config=None))
    assert not result.issues


def test_no_eval_condition_skipped():
    """hitl_gate_config without eval_condition is not checked."""
    result = _check(_edge(hitl_gate_config={"claim_timeout_seconds": 900}))
    assert not result.issues


def test_non_dict_hitl_config_skipped():
    result = _check(_edge(hitl_gate_config="not-a-dict"))
    assert not result.issues


def test_non_dict_eval_condition_skipped():
    result = _check(_edge(hitl_gate_config={"eval_condition": "fast"}))
    assert not result.issues


# ---------------------------------------------------------------------------
# Missing / empty eval_name
# ---------------------------------------------------------------------------


def test_missing_eval_name_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"threshold": 0.8, "operator": "gte"}}))
    assert "HITL_EVAL_CONDITION_MISSING_NAME" in _codes(result)
    assert not result.is_valid


def test_empty_eval_name_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "  ", "threshold": 0.8}}))
    assert "HITL_EVAL_CONDITION_MISSING_NAME" in _codes(result)


def test_missing_name_reports_source_node():
    source = str(uuid.uuid4())
    result = _check(_edge(source=source, hitl_gate_config={"eval_condition": {"threshold": 0.8}}))
    issue = next(i for i in result.issues if i.code == "HITL_EVAL_CONDITION_MISSING_NAME")
    assert issue.node_id == source
    assert source in issue.message


# ---------------------------------------------------------------------------
# Invalid threshold
# ---------------------------------------------------------------------------


def test_threshold_none_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": None}}))
    assert "HITL_EVAL_CONDITION_INVALID_THRESHOLD" in _codes(result)


def test_threshold_bool_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": True}}))
    assert "HITL_EVAL_CONDITION_INVALID_THRESHOLD" in _codes(result)


def test_threshold_string_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": "0.8"}}))
    assert "HITL_EVAL_CONDITION_INVALID_THRESHOLD" in _codes(result)


# ---------------------------------------------------------------------------
# Threshold range
# ---------------------------------------------------------------------------


def test_threshold_above_range_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": 1.5}}))
    assert "HITL_EVAL_CONDITION_THRESHOLD_RANGE" in _codes(result)


def test_threshold_below_range_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": -0.1}}))
    assert "HITL_EVAL_CONDITION_THRESHOLD_RANGE" in _codes(result)


def test_threshold_boundaries_valid():
    for threshold in (0.0, 1.0):
        result = _check(
            _edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": threshold, "operator": "gte"}})
        )
        assert result.is_valid


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------


def test_invalid_operator_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": 0.5, "operator": "=="}}))
    assert "HITL_EVAL_CONDITION_INVALID_OPERATOR" in _codes(result)


def test_missing_operator_is_error():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": 0.5}}))
    assert "HITL_EVAL_CONDITION_INVALID_OPERATOR" in _codes(result)


def test_valid_operators_accepted():
    for op in ("lt", "gt", "lte", "gte", "eq", "neq"):
        cond = {"eval_name": "q", "threshold": 0.5, "operator": op}
        result = _check(_edge(hitl_gate_config={"eval_condition": cond}))
        assert result.is_valid, op


def test_invalid_operator_message_lists_valid_ops():
    result = _check(_edge(hitl_gate_config={"eval_condition": {"eval_name": "q", "threshold": 0.5, "operator": "=="}}))
    issue = next(i for i in result.issues if i.code == "HITL_EVAL_CONDITION_INVALID_OPERATOR")
    assert "gte" in issue.message


# ---------------------------------------------------------------------------
# Full valid config
# ---------------------------------------------------------------------------


def test_valid_eval_condition_no_issues():
    result = _check(_edge())
    assert not result.issues
    assert result.is_valid
