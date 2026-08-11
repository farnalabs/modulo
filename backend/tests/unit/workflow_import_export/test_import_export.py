"""Unit tests for workflow-bundle import retry_policy sanitisation.

``materialize_import`` writes a bundle's pipeline ``retry_policy`` straight into
the imported pipeline. A malformed policy would hard-fail EVERY run of the
imported pipeline at pre-run validation (``GraphValidator.check_retry_policy``
-> ``GraphValidationError``), so the import layer must coerce invalid policies
to the no-policy default ``{}`` instead of persisting a run-breaker.
"""

from __future__ import annotations

from modulo.core.workflow_import_export import _sanitize_retry_policy


def test_sanitize_retry_policy_keeps_valid_dict() -> None:
    policy = {"on": ["stall", "timeout", "failure"], "max_retries": 3}
    assert _sanitize_retry_policy(policy) == policy


def test_sanitize_retry_policy_keeps_minimal_valid_dict() -> None:
    policy = {"on": ["timeout"], "max_retries": 1}
    assert _sanitize_retry_policy(policy) == policy


def test_sanitize_retry_policy_drops_unknown_event() -> None:
    assert _sanitize_retry_policy({"on": ["bogus"], "max_retries": 2}) == {}


def test_sanitize_retry_policy_drops_out_of_range_budget() -> None:
    assert _sanitize_retry_policy({"on": ["failure"], "max_retries": 9}) == {}


def test_sanitize_retry_policy_drops_non_integer_budget() -> None:
    assert _sanitize_retry_policy({"on": ["failure"], "max_retries": "lots"}) == {}


def test_sanitize_retry_policy_drops_non_dict_values() -> None:
    assert _sanitize_retry_policy("stall") == {}
    assert _sanitize_retry_policy(["stall"]) == {}


def test_sanitize_retry_policy_keeps_empty_policy() -> None:
    # An explicit empty policy is the documented "no retry" default — valid.
    assert _sanitize_retry_policy({}) == {}


def test_sanitize_retry_policy_returns_copy_not_reference() -> None:
    # The caller mutates the pipeline attribute afterwards; the sanitizer must
    # not hand back the caller's own dict object.
    policy = {"on": ["failure"], "max_retries": 2}
    sanitized = _sanitize_retry_policy(policy)
    assert sanitized is not policy
    assert sanitized == policy
