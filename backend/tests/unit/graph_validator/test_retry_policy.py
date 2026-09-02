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


# ---------------------------------------------------------------------------
# FAR-525 — check_retry_policy_schedule (the OPTIONAL backoff_schedule key)
# ---------------------------------------------------------------------------


def _schedule_errors(policy: object) -> list[str]:
    result = ValidationResult()
    GraphValidator.check_retry_policy_schedule(policy, result)
    return [i.message for i in result.issues if i.code == "RETRY_POLICY_SCHEDULE_MALFORMED"]


def test_schedule_absent_or_empty_passes() -> None:
    for policy in (
        None,
        {},
        {"on": ["failure"], "max_retries": 1},
        {"on": ["failure"], "max_retries": 1, "backoff_schedule": None},
        {"on": ["failure"], "max_retries": 1, "backoff_schedule": {}},
    ):
        assert not _schedule_errors(policy)


def test_schedule_non_dict_policy_passes() -> None:
    # A non-dict policy is the core validator's fault, not the schedule's.
    assert not _schedule_errors("stall")


def test_schedule_non_dict_schedule_is_malformed() -> None:
    assert _schedule_errors({"backoff_schedule": 45})


def test_schedule_missing_delay_seconds_is_malformed() -> None:
    errors = _schedule_errors({"backoff_schedule": {"multiplier": 2.0}})
    assert len(errors) == 1
    assert "delay_seconds" in errors[0]


def test_schedule_bounds_edges() -> None:

    # Accepted
    for delay in (1, 300, 300.0, 1.0):
        assert not _schedule_errors({"backoff_schedule": {"delay_seconds": delay}}), delay
    for mult in (1, 1.0, 2, 2.0, 10, 10.0):
        assert not _schedule_errors({"backoff_schedule": {"delay_seconds": 45, "multiplier": mult}}), mult
    # Rejected: bools, non-integral, out of bounds, NaN/Infinity
    for bad in (0, 301, -1, 0.9, 300.5, 1.5, True, False, float("nan"), float("inf"), "45", None):
        assert _schedule_errors({"backoff_schedule": {"delay_seconds": bad}}), bad
    for bad in (0.9, 10.1, -1, True, False, float("nan"), float("inf"), "2", None):
        assert _schedule_errors({"backoff_schedule": {"delay_seconds": 45, "multiplier": bad}}), bad


def test_schedule_huge_int_delay_is_malformed_not_a_crash() -> None:
    """FAR-525 qa gate: a JSON int literal with >308 digits (10**309 / 10**400)
    parses to an arbitrary-precision Python int whose float() conversion raises
    OverflowError. The validator must contain that and emit the STANDARD
    RETRY_POLICY_SCHEDULE_MALFORMED issue (a 422 at the write sites, a warning
    at run start) — never raise."""
    for huge in (10**309, 10**400, -(10**400)):
        errors = _schedule_errors({"backoff_schedule": {"delay_seconds": huge}})
        assert len(errors) == 1, huge
    for huge in (10**309, 10**400):
        errors = _schedule_errors({"backoff_schedule": {"delay_seconds": 45, "multiplier": huge}})
        assert len(errors) == 1, huge


def test_schedule_unknown_inner_key_rejected() -> None:
    errors = _schedule_errors({"backoff_schedule": {"delay_seconds": 45, "backof": 2}})
    assert len(errors) == 1
    assert "backof" in errors[0]


def test_schedule_valid_schedule_emits_nothing() -> None:
    assert not _schedule_errors({"backoff_schedule": {"delay_seconds": 30, "multiplier": 1.5}})


def test_schedule_malformed_does_not_affect_core_check() -> None:
    """Layering: the schedule fault is a DISTINCT code — a policy with a valid
    core shape but a malformed schedule passes the CORE check clean, and the
    schedule check alone carries RETRY_POLICY_SCHEDULE_MALFORMED."""
    policy = {"on": ["failure"], "max_retries": 1, "backoff_schedule": {"delay_seconds": 0}}
    core_result = ValidationResult()
    GraphValidator.check_retry_policy(policy, core_result)
    assert "RETRY_POLICY_MALFORMED" not in _codes(core_result)
    assert core_result.is_valid
    schedule_result = ValidationResult()
    GraphValidator.check_retry_policy_schedule(policy, schedule_result)
    assert "RETRY_POLICY_SCHEDULE_MALFORMED" in _codes(schedule_result)
    assert not schedule_result.is_valid
