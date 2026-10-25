"""Structural validation tests for canonical library agent primitives.

Every agent primitive exported by ``modulo.core.library.agents`` must be a
well-formed definition: it carries the expected metadata, exposes valid
input/output JSON Schemas, and its prompt template only references
placeholders that exist on the input schema.  No DB — pure data checks.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from modulo.core.library.agents import __all__ as agent_exports
from modulo.core.library.agents import definitions as agent_defs

from .helpers import AGENTS, kebab

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
    return cast(dict[str, Any], getattr(agent_defs, name))


def test_all_agents_exported() -> None:
    """``__all__`` mirrors the agent definitions module exactly."""
    assert AGENTS, "library catalog is empty"
    assert set(agent_exports) == set(AGENTS), f"unexported: {set(AGENTS) - set(agent_exports)}"
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
    slugs = [kebab(_agent(n)["name"]) for n in agent_exports]
    assert len(slugs) == len(set(slugs)), f"duplicate slugs: {sorted({s for s in slugs if slugs.count(s) > 1})}"


@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_metadata_fields(name: str) -> None:
    agent = _agent(name)
    assert agent["version"] == "1.0.0"
    assert agent["author"] == "Modulo"
    assert agent["description"]
    assert "canonical" in agent["tags"]
    assert "library" in agent["tags"]


@pytest.mark.parametrize("schema_field", ["input_schema", "output_schema"])
@pytest.mark.parametrize("name", agent_exports, ids=lambda n: n)
def test_schemas_are_valid_json_schema(name: str, schema_field: str) -> None:
    """Both schemas are valid JSON Schemas whose required fields are declared."""
    schema = _agent(name)[schema_field]
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object", f"{name}.{schema_field} is not an object schema"
    undeclared = set(schema.get("required", [])) - set(schema["properties"])
    assert not undeclared, f"{name}.{schema_field}: required {undeclared} not in properties"


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
