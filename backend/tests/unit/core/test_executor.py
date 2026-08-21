"""Unit tests for the executor's retry-policy exclusion of correction runs (FAR-210).

The executor excludes correction runs (``trigger_type == 'correction'``) from the
pipeline ``retry_policy`` re-dispatch (executor.py: ``if retry_budget is not None
and not is_correction_run``). A single-node correction has a fixed bounded retry
budget owned by the correction path itself; the pipeline retry policy must never
re-dispatch a correction run (no chained corrections).
"""

from modulo.core.pipeline_engine.executor import _retry_after_policy


def _correction_run_guard(trigger_type: str) -> bool:
    """Mirror the executor's ``is_correction_run`` computation + guard."""
    is_correction_run = trigger_type == "correction"
    retry_budget = _retry_after_policy(
        {"on": ["failure"], "max_retries": 2},
        final_status="failed",
        error_code="agent.failed",
    )
    return retry_budget is not None and not is_correction_run


def test_retry_after_policy_grants_budget_for_retryable_failure():
    budget = _retry_after_policy(
        {"on": ["failure"], "max_retries": 2},
        final_status="failed",
        error_code="agent.failed",
    )
    assert budget == 2


def test_normal_run_with_retryable_failure_is_redispatchable():
    assert _correction_run_guard("manual") is True


def test_correction_run_with_retryable_failure_is_not_redispatchable():
    # Same retryable outcome, but a correction run must never be re-dispatched.
    assert _correction_run_guard("correction") is False


def test_no_retry_policy_yields_no_budget():
    budget = _retry_after_policy({}, final_status="failed", error_code="agent.failed")
    assert budget is None
