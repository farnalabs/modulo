"""Tests for the dogfooding pipeline workflow definition."""

from __future__ import annotations

from typing import Any

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


def test_has_five_steps() -> None:
    steps = DOGFOODING_PIPELINE["pipeline_steps"]
    assert len(steps) == 5


def test_step_identifiers() -> None:
    steps = DOGFOODING_PIPELINE["pipeline_steps"]
    expected_ids = ["read-issue", "generate-diff", "validate", "review-gate", "create-pr"]
    actual_ids = [s["id"] for s in steps]
    assert actual_ids == expected_ids


def test_each_step_has_description() -> None:
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        assert step.get("description"), f"Step '{step['id']}' is missing description"


def test_step_read_issue() -> None:
    step = _step_by_id("read-issue")
    assert step["agent"] is None
    assert step["connector_binding"] == {"type": "source_control", "required": True}


def test_step_generate_diff() -> None:
    step = _step_by_id("generate-diff")
    assert step["agent"] == "correction-proposer"
    assert step["depends_on"] == ["read-issue"]


def test_step_validate() -> None:
    step = _step_by_id("validate")
    assert step["agent"] == "test-generator"
    assert step["depends_on"] == ["generate-diff"]
    assert step["connector_binding"] == {"type": "ci_runner", "required": False}


def test_step_review_gate() -> None:
    step = _step_by_id("review-gate")
    assert step["agent"] == "code-reviewer"
    assert step["depends_on"] == ["validate"]


def test_step_create_pr() -> None:
    step = _step_by_id("create-pr")
    assert step["agent"] is None
    assert step["depends_on"] == ["review-gate"]
    assert step["connector_binding"] == {"type": "source_control", "required": True}


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
