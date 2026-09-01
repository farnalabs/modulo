"""Unit tests for GraphValidator.check_retry_policy.

A malformed pipeline ``retry_policy`` would silently disable retries at run
time, so it is surfaced as a hard error (RETRY_POLICY_MALFORMED). ``None`` /
``{}`` (no policy) are the valid no-policy defaults.
"""

from modulo.core.graph_validator import GraphValidator, ValidationResult


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


def test_retry_policy_none_is_default() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy(None, result)
    assert not result.issues
    assert result.is_valid


def test_retry_policy_empty_dict_is_default() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy({}, result)
    assert not result.issues
    assert result.is_valid


def test_retry_policy_non_dict_is_malformed() -> None:
    for bad in ("stall", ["stall"], 5, True):
        result = ValidationResult()
        GraphValidator.check_retry_policy(bad, result)
        assert "RETRY_POLICY_MALFORMED" in _codes(result)
        assert not result.is_valid


def test_retry_policy_on_not_a_list_is_malformed() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": "stall", "max_retries": 2}, result)
    assert "RETRY_POLICY_MALFORMED" in _codes(result)
    assert not result.is_valid


def test_retry_policy_on_with_non_string_is_malformed() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": ["stall", 42], "max_retries": 2}, result)
    assert "RETRY_POLICY_MALFORMED" in _codes(result)
    assert not result.is_valid


def test_retry_policy_unknown_events_are_malformed() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": ["stall", "bogus"], "max_retries": 2}, result)
    assert "RETRY_POLICY_MALFORMED" in _codes(result)
    message = next(i.message for i in result.issues if i.code == "RETRY_POLICY_MALFORMED")
    assert "bogus" in message
    assert not result.is_valid


def test_retry_policy_max_retries_non_int_is_malformed() -> None:
    for bad in ("lots", 2.5, True):
        result = ValidationResult()
        GraphValidator.check_retry_policy({"on": ["stall"], "max_retries": bad}, result)
        assert "RETRY_POLICY_MALFORMED" in _codes(result)
        assert not result.is_valid


def test_retry_policy_max_retries_out_of_range_is_malformed() -> None:
    for bad in (-1, 6):
        result = ValidationResult()
        GraphValidator.check_retry_policy({"on": ["stall"], "max_retries": bad}, result)
        assert "RETRY_POLICY_MALFORMED" in _codes(result)
        assert not result.is_valid


def test_retry_policy_valid_policy_passes() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": ["stall", "timeout", "failure"], "max_retries": 5}, result)
    assert not result.issues
    assert result.is_valid


def test_retry_policy_eval_failed_event_is_valid() -> None:
    """FAR-503: "eval_failed" is a first-class retry event — a policy that
    re-dispatches guardrail-blocked runs must pass validation."""
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": ["eval_failed"], "max_retries": 1}, result)
    assert not result.issues
    assert result.is_valid


def test_retry_policy_all_events_including_eval_failed_pass() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": ["stall", "timeout", "failure", "eval_failed"], "max_retries": 5}, result)
    assert not result.issues
    assert result.is_valid


def test_retry_policy_unknown_events_still_rejected_alongside_eval_failed() -> None:
    """Adding eval_failed must not loosen the set: near-miss spellings stay malformed."""
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": ["eval_failed", "eval_fail"], "max_retries": 1}, result)
    assert "RETRY_POLICY_MALFORMED" in _codes(result)
    message = next(i.message for i in result.issues if i.code == "RETRY_POLICY_MALFORMED")
    assert "eval_fail" in message
    assert not result.is_valid


def test_retry_policy_default_max_retries_is_valid() -> None:
    result = ValidationResult()
    GraphValidator.check_retry_policy({"on": ["stall"]}, result)
    assert not result.issues
    assert result.is_valid
