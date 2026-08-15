"""Tests for the daily reviewer pipeline workflow definition."""

from __future__ import annotations

import json
from typing import Any

import pytest

from modulo.core.library import DAILY_REVIEWER as IMPORTED_DAILY_REVIEWER
from modulo.core.library.workflows.definitions import DAILY_REVIEWER

from .helpers import KNOWN_AGENTS, VALID_CONNECTOR_TYPES


def test_workflow_exists() -> None:
    assert DAILY_REVIEWER is not None


def test_importable_via_library_package() -> None:
    assert IMPORTED_DAILY_REVIEWER is DAILY_REVIEWER


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
    assert "daily-reviewer" in tags
    assert "collection-level" in tags


def test_step_identifiers() -> None:
    steps = DAILY_REVIEWER["pipeline_steps"]
    expected_ids = ["collect-changes", "review-main", "group-findings", "implement-batches"]
    actual_ids = [s["id"] for s in steps]
    assert len(actual_ids) == 4
    assert actual_ids == expected_ids


def test_each_step_has_description() -> None:
    for step in DAILY_REVIEWER["pipeline_steps"]:
        assert step.get("description"), f"Step '{step['id']}' is missing description"


@pytest.mark.parametrize(
    ("step_id", "expected"),
    [
        (
            "collect-changes",
            {"agent": None, "depends_on": None, "connector_binding": {"type": "source_control", "required": True}},
        ),
        (
            "review-main",
            {"agent": "main-reviewer", "depends_on": ["collect-changes"], "connector_binding": None},
        ),
        (
            "group-findings",
            {"agent": "finding-grouper", "depends_on": ["review-main"], "connector_binding": None},
        ),
        (
            "implement-batches",
            {
                "agent": "spec-implementer",
                "depends_on": ["group-findings"],
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
    for step in DAILY_REVIEWER["pipeline_steps"]:
        agent = step.get("agent")
        if agent is not None:
            assert agent in KNOWN_AGENTS, f"Step '{step['id']}' references unknown agent '{agent}'"


def test_known_agents_cover_daily_reviewer_roles() -> None:
    assert "main-reviewer" in KNOWN_AGENTS
    assert "finding-grouper" in KNOWN_AGENTS
    assert "spec-implementer" in KNOWN_AGENTS


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


def test_default_config_keys() -> None:
    config = DAILY_REVIEWER["default_config"]
    expected = {
        "schedule",
        "base_branch",
        "review_window_hours",
        "repository",
        "branch_prefix",
        "team",
        "ticket_labels",
        "max_findings_per_batch",
        "parallel_coders",
        "tracking_system",
        "auto_create_pr",
        "test_command",
        "notification_channels",
    }
    assert config.keys() == expected


def test_default_config_schedules_daily_at_0500_utc() -> None:
    assert DAILY_REVIEWER["default_config"]["schedule"] == "cron(0 5 * * *)"
    assert DAILY_REVIEWER["default_config"]["review_window_hours"] == 24


def test_phase1_defaults_to_sequential_coders() -> None:
    assert DAILY_REVIEWER["default_config"]["parallel_coders"] is False


def test_json_roundtrip() -> None:
    serialized = json.dumps(DAILY_REVIEWER, default=str)
    deserialized = json.loads(serialized)
    assert deserialized["name"] == "Daily Reviewer"
    assert len(deserialized["pipeline_steps"]) == 4


def _step_by_id(step_id: str) -> dict[str, Any]:
    for step in DAILY_REVIEWER["pipeline_steps"]:
        if step["id"] == step_id:
            return step
    msg = f"Step '{step_id}' not found"
    raise AssertionError(msg)
