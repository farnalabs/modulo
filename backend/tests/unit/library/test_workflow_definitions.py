"""Generic structural tests for every canonical library workflow primitive.

These invariants apply to all workflow templates shipped in
``modulo.core.library.workflows.definitions`` — unique step ids, valid
dependency chains, known agent references, well-formed connector bindings,
and JSON-serialisable definitions.  Keeping them parametrized across every
workflow means a structural regression in any template fails loudly, not
just the ones covered by dogfooding-specific assertions.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from modulo.core.library import (
    ADR_WORKFLOW,
    CICD_WORKFLOW,
    DOGFOODING_PIPELINE,
    FEATURE_PROPOSAL,
    FULL_SDLC,
    INCIDENT_TO_DEPLOY,
    MEETING_TO_TICKETS,
    RELEASE_CANDIDATE,
    REQUIREMENTS_TO_FILE,
    SCHEMA_INFERENCE_PIPELINE,
    SIMPLEST_WORKFLOW,
    SPRINT_RETROSPECTIVE,
    TICKET_TO_PR,
    WEEKLY_QUALITY_REPORT,
)

from .helpers import (
    KNOWN_AGENTS,
    REQUIRED_WORKFLOW_KEYS,
    VALID_CONNECTOR_TYPES,
    WORKFLOWS,
)

WORKFLOW_CASES = [
    INCIDENT_TO_DEPLOY,
    FEATURE_PROPOSAL,
    SCHEMA_INFERENCE_PIPELINE,
    REQUIREMENTS_TO_FILE,
    FULL_SDLC,
    TICKET_TO_PR,
    ADR_WORKFLOW,
    MEETING_TO_TICKETS,
    SPRINT_RETROSPECTIVE,
    WEEKLY_QUALITY_REPORT,
    CICD_WORKFLOW,
    DOGFOODING_PIPELINE,
    RELEASE_CANDIDATE,
    SIMPLEST_WORKFLOW,
]

# Every template must be discoverable via the library package's exports too.
WORKFLOW_IDS = [wf["name"] for wf in WORKFLOW_CASES]


def test_all_workflows_are_exposed_by_package() -> None:
    expected = {wf["name"] for wf in WORKFLOW_CASES}
    exported = {wf["name"] for wf in WORKFLOWS.values()}
    assert expected == exported


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_required_top_level_keys(workflow: dict[str, Any]) -> None:
    assert REQUIRED_WORKFLOW_KEYS.issubset(workflow.keys())


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_metadata_fields(workflow: dict[str, Any]) -> None:
    assert isinstance(workflow["name"], str) and workflow["name"]
    assert isinstance(workflow["description"], str) and workflow["description"]
    assert workflow["version"] == "1.0.0"
    assert workflow["author"] == "Modulo"
    assert isinstance(workflow["tags"], list) and "canonical" in workflow["tags"]
    assert isinstance(workflow["default_config"], dict) and workflow["default_config"]


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_step_ids_are_unique(workflow: dict[str, Any]) -> None:
    step_ids = [step["id"] for step in workflow["pipeline_steps"]]
    assert len(step_ids) == len(set(step_ids)), f"duplicate step ids in {workflow['name']}"


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_each_step_has_description(workflow: dict[str, Any]) -> None:
    for step in workflow["pipeline_steps"]:
        assert step.get("description"), f"Step '{step['id']}' in '{workflow['name']}' has no description"


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_agent_refs_are_known(workflow: dict[str, Any]) -> None:
    for step in workflow["pipeline_steps"]:
        agent = step.get("agent")
        if agent is not None:
            assert agent in KNOWN_AGENTS, (
                f"Step '{step['id']}' in '{workflow['name']}' references unknown agent '{agent}'"
            )


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_connector_bindings_are_well_formed(workflow: dict[str, Any]) -> None:
    for step in workflow["pipeline_steps"]:
        binding = step.get("connector_binding")
        if binding is None:
            continue
        assert binding["type"] in VALID_CONNECTOR_TYPES, (
            f"Step '{step['id']}' in '{workflow['name']}' has invalid connector type '{binding['type']}'"
        )
        assert isinstance(binding["required"], bool), (
            f"Step '{step['id']}' in '{workflow['name']}' has non-boolean connector binding 'required'"
        )


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_dependency_chain_is_valid(workflow: dict[str, Any]) -> None:
    step_ids = {step["id"] for step in workflow["pipeline_steps"]}
    for step in workflow["pipeline_steps"]:
        for dep in step.get("depends_on", []):
            assert dep in step_ids, f"Step '{step['id']}' in '{workflow['name']}' depends on unknown step '{dep}'"
            assert dep != step["id"], f"Step '{step['id']}' in '{workflow['name']}' depends on itself"


@pytest.mark.parametrize("workflow", WORKFLOW_CASES, ids=WORKFLOW_IDS)
def test_json_roundtrip(workflow: dict[str, Any]) -> None:
    serialized = json.dumps(workflow)
    deserialized = json.loads(serialized)
    assert deserialized["name"] == workflow["name"]
    assert len(deserialized["pipeline_steps"]) == len(workflow["pipeline_steps"])
