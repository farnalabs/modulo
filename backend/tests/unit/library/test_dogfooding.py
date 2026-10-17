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
from modulo.core.seed_data.library_schemas import SCHEMAS

from .helpers import KNOWN_AGENTS


def test_name_and_version() -> None:
    assert DOGFOODING_PIPELINE["name"] == "Dogfooding Pipeline"
    assert DOGFOODING_PIPELINE["version"] == "1.0.0"
    assert DOGFOODING_PIPELINE["author"] == "Modulo"


def test_tags_include_canonical() -> None:
    tags = DOGFOODING_PIPELINE["tags"]
    assert "canonical" in tags
    assert "dogfooding" in tags
    assert "issue-to-pr" in tags


def test_step_identifiers() -> None:
    steps = DOGFOODING_PIPELINE["pipeline_steps"]
    expected_ids = ["read-issue", "generate-diff", "validate", "review-gate", "create-pr"]
    actual_ids = [s["id"] for s in steps]
    assert len(actual_ids) == 5
    assert actual_ids == expected_ids


@pytest.mark.parametrize(
    ("step_id", "expected"),
    [
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
    ],
)
def test_step_properties(step_id: str, expected: dict[str, Any]) -> None:
    step = _step_by_id(step_id)
    assert step.get("agent") == expected["agent"]
    assert step.get("depends_on") == expected["depends_on"]
    assert step.get("connector_binding") == expected["connector_binding"]


def test_known_agents_cover_spec_implementer() -> None:
    """The simplest workflow uses spec-implementer; the catalog must know it."""
    assert "spec-implementer" in KNOWN_AGENTS


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


def _step_by_id(step_id: str) -> dict[str, Any]:
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        if step["id"] == step_id:
            return step
    msg = f"Step '{step_id}' not found"
    raise AssertionError(msg)


def test_dogfood_input_step_binds_defined_schema() -> None:
    """The Dogfooding Pipeline's input step binds a registered reusable schema."""
    schemas = {entry["name"]: entry["definition"] for entry in SCHEMAS}

    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        bound = step.get("input_schema")
        if bound is None:
            continue
        assert bound in schemas, f"Step '{step['id']}' binds unknown schema '{bound}'"

    read_issue = _step_by_id("read-issue")
    assert read_issue.get("input_schema") == "ticket-input"
    assert "ticket-input" in schemas
