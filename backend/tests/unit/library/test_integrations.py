"""Structural validation tests for canonical library integration primitives.

Every integration primitive exported by ``modulo.core.library.integrations``
must be a well-formed definition: expected metadata, a default config, and
credential fields each describing a type, a description, and whether they are
required.  No DB — pure data checks.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from modulo.core.library.integrations import __all__ as integration_exports
from modulo.core.library.integrations import definitions as integration_defs

REQUIRED_KEYS: set[str] = {
    "name",
    "description",
    "version",
    "author",
    "tags",
    "connector_type",
    "default_config",
    "credential_fields",
    "tool_group",
}

VALID_FIELD_TYPES: set[str] = {"string", "boolean", "number", "integer"}


def _integration(name: str) -> dict[str, Any]:
    return getattr(integration_defs, name)


def test_all_integrations_exported() -> None:
    """Every exported integration constant is a dict definition."""
    assert len(integration_exports) == 24
    for name in integration_exports:
        assert isinstance(_integration(name), dict), name


@pytest.mark.parametrize("name", integration_exports, ids=lambda n: n)
def test_has_required_keys(name: str) -> None:
    integration = _integration(name)
    assert REQUIRED_KEYS.issubset(integration), f"missing {REQUIRED_KEYS - set(integration)}"


@pytest.mark.parametrize("name", integration_exports, ids=lambda n: n)
def test_metadata_fields(name: str) -> None:
    integration = _integration(name)
    assert integration["version"] == "1.0.0"
    assert integration["author"] == "Modulo"
    assert integration["description"]
    assert "canonical" in integration["tags"]
    assert integration["connector_type"]
    assert integration["tool_group"]


@pytest.mark.parametrize("name", integration_exports, ids=lambda n: n)
def test_default_config_is_dict(name: str) -> None:
    config = _integration(name)["default_config"]
    assert isinstance(config, dict)
    assert config, "default_config must not be empty"


@pytest.mark.parametrize("name", integration_exports, ids=lambda n: n)
def test_credential_fields_are_well_formed(name: str) -> None:
    """Every credential field must carry type, description, and required."""
    for field, spec in _integration(name)["credential_fields"].items():
        assert isinstance(spec, dict), f"{name}.{field} not a dict"
        assert spec["type"] in VALID_FIELD_TYPES, f"{name}.{field} bad type {spec.get('type')!r}"
        assert spec.get("description"), f"{name}.{field} missing description"
        assert isinstance(spec["required"], bool), f"{name}.{field} required not bool"


@pytest.mark.parametrize("name", integration_exports, ids=lambda n: n)
def test_credential_fields_non_empty(name: str) -> None:
    assert _integration(name)["credential_fields"], f"{name} has no credential fields"


@pytest.mark.parametrize("name", integration_exports, ids=lambda n: n)
def test_json_roundtrip(name: str) -> None:
    integration = _integration(name)
    assert json.loads(json.dumps(integration)) == integration


def test_integration_names_are_unique() -> None:
    names = [_integration(n)["name"] for n in integration_exports]
    assert len(names) == len(set(names))


def test_connector_types_are_known() -> None:
    """connector_type must resolve to a registered ConnectorType, 'custom',
    or a workflow connector-binding type used by the pipeline library."""
    from modulo.connectors.base import ConnectorType
    from modulo.core.library.workflows import __all__ as workflow_exports
    from modulo.core.library.workflows import definitions as workflow_defs

    binding_types = {
        step["connector_binding"]["type"]
        for name in workflow_exports
        for step in getattr(workflow_defs, name)["pipeline_steps"]
        if step.get("connector_binding")
    }
    known = {t.value for t in ConnectorType} | binding_types | {"custom"}
    for name in integration_exports:
        connector_type = _integration(name)["connector_type"]
        assert connector_type in known, f"{name}: unknown connector_type {connector_type!r}"
