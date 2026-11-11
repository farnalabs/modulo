"""Tests for the daily reviewer pipeline workflow definition.

Generic structural invariants (required keys, step descriptions, agent refs,
connector bindings, dependency edges, JSON roundtrip) are asserted for every
workflow by ``test_workflows.py``; this module pins the daily reviewer
pipeline's own contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from farna.core.library.workflows.definitions import DAILY_REVIEWER

from .helpers import assert_step_case_matches, assert_step_ids

STEP_CASES: list[tuple[str, dict[str, Any]]] = [
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
]


def test_name() -> None:
    assert DAILY_REVIEWER["name"] == "Daily Reviewer"


def test_tags_include_canonical() -> None:
    tags = DAILY_REVIEWER["tags"]
    assert "canonical" in tags
    assert "daily-reviewer" in tags
    assert "collection-level" in tags


def test_step_identifiers() -> None:
    assert_step_ids(DAILY_REVIEWER, STEP_CASES)


@pytest.mark.parametrize("case", STEP_CASES, ids=[case[0] for case in STEP_CASES])
def test_step_properties(case: tuple[str, dict[str, Any]]) -> None:
    assert_step_case_matches(DAILY_REVIEWER, case)


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
