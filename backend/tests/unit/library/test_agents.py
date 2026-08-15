"""Structural validation tests for canonical library agent primitives.

Every agent primitive exported by ``modulo.core.library.agents`` must be a
well-formed definition: it carries the expected metadata, exposes valid
input/output JSON Schemas, and its prompt template only references
placeholders that exist on the input schema.  No DB — pure data checks.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from modulo.core.library.agents import __all__ as agent_exports
from modulo.core.library.agents import definitions as agent_defs

REQUIRED_KEYS: set[str] = {
    "name",
    "description",
    "node_type",
    "role",
    "prompt_template",
    "input_schema",
    "output_schema",
    "tags",
    "version",
    "author",
}

VALID_ROLES: set[str] = {
    "aggregator",
    "analyzer",
    "estimator",
    "generator",
    "implementer",
    "improver",
    "inferrer",
    "planner",
    "proposer",
    "reporter",
    "reviewer",
    "summarizer",
    "triage",
    "writer",
}

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _agent(name: str) -> dict[str, Any]:
    return getattr(agent_defs, name)


def test_all_agents_exported() -> None:
    """Every exported agent constant is a dict definition."""
    assert len(agent_exports) == 23
    for name in agent_exports:
        assert isinstance(_agent(name), dict), name


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_has_required_keys(name: str) -> None:
    agent = _agent(name)
    assert REQUIRED_KEYS.issubset(agent), f"missing {REQUIRED_KEYS - set(agent)}"


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_node_type_is_agent(name: str) -> None:
    assert _agent(name)["node_type"] == "agent"


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_role_is_known(name: str) -> None:
    role = _agent(name)["role"]
    assert role in VALID_ROLES, f"unexpected role {role!r}"


def test_agent_slugs_are_unique() -> None:
    """Name-derived slugs must be unique so library lookups stay unambiguous."""
    slugs = [_agent(n)["name"].lower().replace(" ", "-") for n in agent_exports]
    assert len(slugs) == len(set(slugs))


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_metadata_fields(name: str) -> None:
    agent = _agent(name)
    assert agent["version"] == "1.0.0"
    assert agent["author"] == "Modulo"
    assert agent["description"]
    assert "canonical" in agent["tags"]
    assert "library" in agent["tags"]


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_input_schema_is_valid_json_schema(name: str) -> None:
    schema = _agent(name)["input_schema"]
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    for field in schema.get("required", []):
        assert field in schema["properties"], f"required {field!r} not in properties"


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_output_schema_is_valid_json_schema(name: str) -> None:
    schema = _agent(name)["output_schema"]
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    for field in schema.get("required", []):
        assert field in schema["properties"], f"required {field!r} not in properties"


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_prompt_placeholders_match_input_schema(name: str) -> None:
    """Every {placeholder} in the prompt must be a property of input_schema."""
    agent = _agent(name)
    placeholders = set(_PLACEHOLDER_RE.findall(agent["prompt_template"]))
    properties = set(agent["input_schema"]["properties"])
    assert placeholders <= properties, f"unknown placeholders: {placeholders - properties}"


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_json_roundtrip(name: str) -> None:
    agent = _agent(name)
    assert json.loads(json.dumps(agent)) == agent


def test_workflow_agent_refs_all_exist() -> None:
    """Every agent referenced by a workflow step must be a defined primitive."""
    from modulo.core.library.workflows import __all__ as workflow_exports
    from modulo.core.library.workflows import definitions as workflow_defs

    defined = {_agent(n)["name"].lower().replace(" ", "-") for n in agent_exports}
    for name in workflow_exports:
        for step in getattr(workflow_defs, name)["pipeline_steps"]:
            agent = step.get("agent")
            if agent is not None:
                assert agent in defined, f"{name}: unknown agent ref {agent!r}"
