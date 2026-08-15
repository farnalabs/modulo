"""Tests for the Daily Reviewer pipeline (FAR-172).

Collection-level review of main-as-a-whole: the workflow must gather the
day's merged commits, run collection-level lenses that PR-scope review
cannot see, group findings into bounded work batches, and implement each
batch as a pull request (Phase 1 sequential, Phase 2 parallel fan-out).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from modulo.core.library import COLLECTION_REVIEWER as IMPORTED_COLLECTION_REVIEWER
from modulo.core.library import DAILY_REVIEWER as IMPORTED_DAILY_REVIEWER
from modulo.core.library.agents.definitions import COLLECTION_REVIEWER
from modulo.core.library.workflows.definitions import DAILY_REVIEWER

KNOWN_AGENTS: set[str] = {
    "changelog-aggregator",
    "changelog-writer",
    "code-reviewer",
    "collection-reviewer",
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
    "spec-implementer",
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
    assert DAILY_REVIEWER is not None


def test_importable_via_library_package() -> None:
    assert IMPORTED_DAILY_REVIEWER is DAILY_REVIEWER


def test_agent_exists() -> None:
    assert COLLECTION_REVIEWER is not None


def test_agent_importable_via_library_package() -> None:
    assert IMPORTED_COLLECTION_REVIEWER is COLLECTION_REVIEWER


def test_required_top_level_keys() -> None:
    required = {"name", "description", "version", "author", "tags", "pipeline_steps", "default_config"}
    assert required.issubset(DAILY_REVIEWER.keys())


def test_name_and_version() -> None:
    assert DAILY_REVIEWER["name"] == "Daily Reviewer"
    assert DAILY_REVIEWER["version"] == "1.0.0"
    assert DAILY_REVIEWER["author"] == "Modulo"


def test_tags_include_canonical() -> None:
    tags = DAILY_REVIEWER["tags"]
    assert "canonical" in tags
    assert "daily" in tags
    assert "collection" in tags
    assert "review" in tags


def test_has_five_steps() -> None:
    steps = DAILY_REVIEWER["pipeline_steps"]
    assert len(steps) == 5


def test_step_identifiers() -> None:
    steps = DAILY_REVIEWER["pipeline_steps"]
    expected_ids = [
        "commits-collection",
        "collection-review",
        "batch-grouping",
        "batch-implementation",
        "pr-creation",
    ]
    actual_ids = [s["id"] for s in steps]
    assert actual_ids == expected_ids


@pytest.mark.parametrize(
    ("step_id", "expected"),
    [
        (
            "commits-collection",
            {
                "agent": None,
                "depends_on": None,
                "connector_binding": {"type": "source_control", "required": True},
            },
        ),
        (
            "collection-review",
            {
                "agent": "collection-reviewer",
                "depends_on": ["commits-collection"],
                "connector_binding": None,
            },
        ),
        (
            "batch-grouping",
            {
                "agent": "ticket-writer",
                "depends_on": ["collection-review"],
                "connector_binding": {"type": "issue_tracking", "required": True},
            },
        ),
        (
            "batch-implementation",
            {
                "agent": None,
                "depends_on": ["batch-grouping"],
                "connector_binding": {"type": "source_control", "required": True},
            },
        ),
        (
            "pr-creation",
            {
                "agent": None,
                "depends_on": ["batch-implementation"],
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


def test_collection_reviewer_agent_is_known() -> None:
    assert "collection-reviewer" in KNOWN_AGENTS
    assert COLLECTION_REVIEWER["name"].lower().replace(" ", "-") == "collection-reviewer"


def test_all_agent_refs_are_known() -> None:
    for step in DAILY_REVIEWER["pipeline_steps"]:
        agent = step.get("agent")
        if agent is not None:
            assert agent in KNOWN_AGENTS, f"Step '{step['id']}' references unknown agent '{agent}'"


def test_all_connector_bindings_are_valid() -> None:
    for step in DAILY_REVIEWER["pipeline_steps"]:
        binding = step.get("connector_binding")
        if binding is not None:
            assert binding["type"] in VALID_CONNECTOR_TYPES, (
                f"Step '{step['id']}' has invalid connector type '{binding['type']}'"
            )
            assert "required" in binding


def test_dependency_chain_is_valid() -> None:
    step_ids = {s["id"] for s in DAILY_REVIEWER["pipeline_steps"]}
    for step in DAILY_REVIEWER["pipeline_steps"]:
        for dep in step.get("depends_on", []):
            assert dep in step_ids, f"Step '{step['id']}' depends on '{dep}' which does not exist"


def test_reviewer_feeds_grouper_feeds_coder_chain() -> None:
    """The collection review must feed grouping, which must feed implementation."""
    by_id = {s["id"]: s for s in DAILY_REVIEWER["pipeline_steps"]}
    assert "collection-review" in by_id["batch-grouping"]["depends_on"]
    assert "batch-grouping" in by_id["batch-implementation"]["depends_on"]
    assert "batch-implementation" in by_id["pr-creation"]["depends_on"]


def test_default_config_keys() -> None:
    config = DAILY_REVIEWER["default_config"]
    expected = {
        "schedule",
        "review_window_hours",
        "base_branch",
        "max_findings_per_batch",
        "max_file_footprint_per_batch",
        "batch_size_hint",
        "phase",
        "branch_prefix",
        "ticket_labels",
        "auto_test",
        "pr_template",
    }
    assert config.keys() == expected


def test_schedule_is_daily_at_0500_utc() -> None:
    assert DAILY_REVIEWER["default_config"]["schedule"] == "cron(0 5 * * *)"


def test_phase_1_is_sequential() -> None:
    """Phase 1 ships the sequential-loop pattern; fan-out lands in Phase 2."""
    assert DAILY_REVIEWER["default_config"]["phase"] == "sequential"


def test_collection_reviewer_lenses() -> None:
    prompt = COLLECTION_REVIEWER["prompt_template"]
    assert "Logical conflict" in prompt
    assert "DRY across PRs" in prompt
    output = json.dumps(COLLECTION_REVIEWER["output_schema"])
    for lens in ("logical_conflict", "dr_across_prs", "convention_drift"):
        assert lens in output


def test_collection_reviewer_input_schema() -> None:
    schema = COLLECTION_REVIEWER["input_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"commits", "merged_diff"}
    assert "sha" in schema["properties"]["commits"]["items"]["required"]


def test_json_roundtrip() -> None:
    serialized = json.dumps(DAILY_REVIEWER, default=str)
    deserialized = json.loads(serialized)
    assert deserialized["name"] == "Daily Reviewer"
    assert len(deserialized["pipeline_steps"]) == 5


def _step_by_id(step_id: str) -> dict[str, Any]:
    for step in DAILY_REVIEWER["pipeline_steps"]:
        if step["id"] == step_id:
            return step
    msg = f"Step '{step_id}' not found"
    raise AssertionError(msg)
