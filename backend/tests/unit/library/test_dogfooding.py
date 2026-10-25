"""Tests for the dogfooding pipeline workflow definition.

Generic structural invariants (required keys, step descriptions, agent refs,
connector bindings, dependency edges, JSON roundtrip) are asserted for every
workflow by ``test_workflows.py``; this module pins the dogfooding pipeline's
own contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from modulo.core.library.workflows.definitions import DOGFOODING_PIPELINE

from .helpers import assert_step_matches, step_by_id

STEP_CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "read-issue",
        {"agent": None, "depends_on": None, "connector_binding": {"type": "source_control", "required": True}},
    ),
    (
        "generate-diff",
        {"agent": "correction-proposer", "depends_on": ["read-issue"], "connector_binding": None},
    ),
    (
        "validate",
        {
            "agent": "test-generator",
            "depends_on": ["generate-diff"],
            "connector_binding": {"type": "ci_runner", "required": False},
        },
    ),
    (
        "review-gate",
        {"agent": "code-reviewer", "depends_on": ["validate"], "connector_binding": None},
    ),
    (
        "create-pr",
        {
            "agent": None,
            "depends_on": ["review-gate"],
            "connector_binding": {"type": "source_control", "required": True},
        },
    ),
]


def test_name() -> None:
    assert DOGFOODING_PIPELINE["name"] == "Dogfooding Pipeline"


def test_tags_include_canonical() -> None:
    tags = DOGFOODING_PIPELINE["tags"]
    assert "canonical" in tags
    assert "dogfooding" in tags
    assert "issue-to-pr" in tags


def test_step_identifiers() -> None:
    actual_ids = [step["id"] for step in DOGFOODING_PIPELINE["pipeline_steps"]]
    assert actual_ids == [step_id for step_id, _ in STEP_CASES]


@pytest.mark.parametrize(("step_id", "expected"), STEP_CASES, ids=[case[0] for case in STEP_CASES])
def test_step_properties(step_id: str, expected: dict[str, Any]) -> None:
    assert_step_matches(
        DOGFOODING_PIPELINE,
        step_id,
        agent=expected["agent"],
        depends_on=expected["depends_on"],
        connector_binding=expected["connector_binding"],
    )


def test_default_config_keys() -> None:
    config = DOGFOODING_PIPELINE["default_config"]
    expected = {
        "repo_owner",
        "repo_name",
        "base_branch",
        "branch_prefix",
        "require_human_approval",
        "auto_create_pr",
        "pr_template",
        "test_command",
        "notification_channels",
    }
    assert config.keys() == expected


def test_input_step_binds_ticket_input_schema() -> None:
    """The Dogfooding Pipeline's input step binds the shared ticket-input schema."""
    read_issue = step_by_id(DOGFOODING_PIPELINE, "read-issue")
    assert read_issue.get("input_schema") == "ticket-input"
