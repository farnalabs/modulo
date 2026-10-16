"""Tests for the daily reviewer pipeline workflow definition.

Generic structural invariants (required keys, step descriptions, agent refs,
connector bindings, dependency edges, JSON roundtrip) are asserted for every
workflow by ``test_workflows.py``; this module pins the daily reviewer
pipeline's own contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from modulo.core.library.workflows.definitions import DAILY_REVIEWER

from .helpers import KNOWN_AGENTS


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


def test_known_agents_cover_daily_reviewer_roles() -> None:
    assert "main-reviewer" in KNOWN_AGENTS
    assert "finding-grouper" in KNOWN_AGENTS
    assert "spec-implementer" in KNOWN_AGENTS


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


def _step_by_id(step_id: str) -> dict[str, Any]:
    for step in DAILY_REVIEWER["pipeline_steps"]:
        if step["id"] == step_id:
            return step
    msg = f"Step '{step_id}' not found"
    raise AssertionError(msg)
