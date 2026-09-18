"""Structural validation tests for canonical library workflow primitives.

Every workflow primitive exported by ``modulo.core.library.workflows`` must be
a well-formed pipeline template: expected metadata, a non-empty sequence of
steps with unique ids, valid ``depends_on`` edges, and connector bindings whose
type belongs to the known connector-binding vocabulary.  No DB — pure data
checks.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from modulo.core.library.agents import __all__ as agent_exports
from modulo.core.library.agents import definitions as agent_defs
from modulo.core.library.workflows import __all__ as workflow_exports
from modulo.core.library.workflows import definitions as workflow_defs

REQUIRED_KEYS: set[str] = {
    "name",
    "description",
    "version",
    "author",
    "tags",
    "pipeline_steps",
    "default_config",
}

VALID_CONNECTOR_BINDING_TYPES: set[str] = {
    "source_control",
    "ci_runner",
    "ci_cd",
    "incident_management",
    "issue_tracking",
    "filesystem",
    "monitoring",
    "messaging",
    "devops",
    "security",
    "code_quality",
    "containers",
    "documentation",
    "observability",
    "infrastructure",
    "automation",
    "integration",
}


def _workflow(name: str) -> dict[str, Any]:
    return getattr(workflow_defs, name)


def test_all_workflows_exported() -> None:
    """Every exported workflow constant is a dict definition."""
    assert len(workflow_exports) == 15
    for name in workflow_exports:
        assert isinstance(_workflow(name), dict), name


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_has_required_keys(name: str) -> None:
    workflow = _workflow(name)
    assert REQUIRED_KEYS.issubset(workflow), f"missing {REQUIRED_KEYS - set(workflow)}"


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_metadata_fields(name: str) -> None:
    workflow = _workflow(name)
    assert workflow["version"] == "1.0.0"
    assert workflow["author"] == "Modulo"
    assert workflow["description"]
    assert "canonical" in workflow["tags"]
    assert workflow["default_config"]


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_step_ids_are_unique(name: str) -> None:
    steps = _workflow(name)["pipeline_steps"]
    assert steps, f"{name} has no pipeline steps"
    ids = [s["id"] for s in steps]
    assert len(ids) == len(set(ids)), f"{name}: duplicate step ids {ids}"


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_each_step_has_description(name: str) -> None:
    for step in _workflow(name)["pipeline_steps"]:
        assert step.get("description"), f"{name}.{step['id']} missing description"


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_dependency_edges_are_valid(name: str) -> None:
    """Every depends_on reference must name a step in the same workflow."""
    steps = _workflow(name)["pipeline_steps"]
    ids = {s["id"] for s in steps}
    for step in steps:
        for dep in step.get("depends_on", []):
            assert dep in ids, f"{name}.{step['id']} depends on unknown {dep!r}"


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_agent_refs_are_known(name: str) -> None:
    """Every agent reference must map to a defined agent primitive."""
    defined = {getattr(agent_defs, n)["name"].lower().replace(" ", "-") for n in agent_exports}
    for step in _workflow(name)["pipeline_steps"]:
        agent = step.get("agent")
        if agent is not None:
            assert agent in defined, f"{name}: unknown agent ref {agent!r}"


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_connector_bindings_are_valid(name: str) -> None:
    """Connector bindings must carry a known type and a boolean required flag."""
    for step in _workflow(name)["pipeline_steps"]:
        binding = step.get("connector_binding")
        if binding is None:
            continue
        assert binding["type"] in VALID_CONNECTOR_BINDING_TYPES, (
            f"{name}.{step['id']}: invalid binding type {binding['type']!r}"
        )
        assert isinstance(binding.get("required"), bool), f"{name}.{step['id']}: required must be a bool"


@pytest.mark.parametrize("name", workflow_exports, ids=lambda n: n)
def test_json_roundtrip(name: str) -> None:
    workflow = _workflow(name)
    assert json.loads(json.dumps(workflow)) == workflow


def test_workflow_names_are_unique() -> None:
    names = [_workflow(n)["name"] for n in workflow_exports]
    assert len(names) == len(set(names))


def test_integration_tool_groups_match_binding_vocabulary() -> None:
    """Integration tool_groups use the same vocabulary as workflow bindings."""
    from modulo.core.library.integrations import __all__ as integration_exports
    from modulo.core.library.integrations import definitions as integration_defs

    for name in integration_exports:
        tool_group = getattr(integration_defs, name)["tool_group"]
        assert tool_group in VALID_CONNECTOR_BINDING_TYPES, f"{name}: unknown tool_group {tool_group!r}"
