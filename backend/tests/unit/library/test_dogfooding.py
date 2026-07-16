"""Tests for the dogfooding pipeline workflow definition."""

from __future__ import annotations

from typing import Any

import pytest

from modulo.core.library import DOGFOODING_PIPELINE as IMPORTED_DOGFOODING
from modulo.core.library.workflows.definitions import DOGFOODING_PIPELINE

KNOWN_AGENTS: set[str] = {
    "changelog-aggregator",
    "changelog-writer",
    "code-reviewer",
    "compliance-checker",
    "correction-proposer",
    "dependency-analyzer",
    "doc-generator",
    "eval-proposal-writer",
    "feedback-analyzer",
    "migration-planner",
    "prd-summarizer",
    "prompt-improver",
    "release-note-generator",
    "rollback-planner",
    "schema-inferrer",
    "security-reviewer",
    "status-reporter",
    "test-generator",
    "ticket-estimator",
    "ticket-triager",
    "ticket-writer",
    "complexity-reviewer",
}

VALID_CONNECTOR_TYPES: set[str] = {
    "source_control",
    "ci_runner",
    "ci_cd",
    "incident_management",
    "issue_tracking",
    "filesystem",
    "monitoring",
    "messaging",
}


def test_workflow_exists() -> None:
    assert DOGFOODING_PIPELINE is not None


def test_importable_via_library_package() -> None:
    assert IMPORTED_DOGFOODING is DOGFOODING_PIPELINE


def test_required_top_level_keys() -> None:
    required = {"name", "description", "version", "author", "tags", "pipeline_steps", "default_config"}
    assert required.issubset(DOGFOODING_PIPELINE.keys())


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


def test_each_step_has_description() -> None:
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        assert step.get("description"), f"Step '{step['id']}' is missing description"


@pytest.mark.parametrize(
    "step_id,expected",
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


def test_all_agent_refs_are_known() -> None:
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        agent = step.get("agent")
        if agent is not None:
            assert agent in KNOWN_AGENTS, f"Step '{step['id']}' references unknown agent '{agent}'"


def test_all_connector_bindings_are_valid() -> None:
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        binding = step.get("connector_binding")
        if binding is not None:
            assert binding["type"] in VALID_CONNECTOR_TYPES, (
                f"Step '{step['id']}' has invalid connector type '{binding['type']}'"
            )
            assert "required" in binding


def test_dependency_chain_is_valid() -> None:
    step_ids = {s["id"] for s in DOGFOODING_PIPELINE["pipeline_steps"]}
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        for dep in step.get("depends_on", []):
            assert dep in step_ids, f"Step '{step['id']}' depends on '{dep}' which does not exist"


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


def test_json_roundtrip() -> None:
    import json

    serialized = json.dumps(DOGFOODING_PIPELINE, default=str)
    deserialized = json.loads(serialized)
    assert deserialized["name"] == "Dogfooding Pipeline"
    assert len(deserialized["pipeline_steps"]) == 5


def _step_by_id(step_id: str) -> dict[str, Any]:
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        if step["id"] == step_id:
            return step
    msg = f"Step '{step_id}' not found"
    raise AssertionError(msg)
