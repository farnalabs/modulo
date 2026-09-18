"""FAR-609: every HITL gate defaults to ``human_only: true``.

The shipped library seed templates and the built-in registry workflow carry
explicit gate configs; they must default to the fail-safe human-only posture
(opting out is an explicit edit), matching the resolver default in
``db.crud.hitl_gate_config.human_only_effective``.
"""

from __future__ import annotations

from modulo.core.library_service._seed_data import (
    _INCIDENT_TEMPLATE_EDGES,
    _PR_TEMPLATE_EDGES,
    _RELEASE_TEMPLATE_EDGES,
)
from modulo.core.registry import list_registry_primitives


def _seed_gate_configs() -> list[dict]:
    configs = []
    for edge_list in (_PR_TEMPLATE_EDGES, _RELEASE_TEMPLATE_EDGES, _INCIDENT_TEMPLATE_EDGES):
        for edge in edge_list:
            config = edge.get("hitl_gate_config")
            if config is not None:
                configs.append(config)
    return configs


def test_every_seed_gate_defaults_human_only() -> None:
    """All shipped seed HITL gates ship human_only: true (FAR-609)."""
    configs = _seed_gate_configs()
    assert configs, "expected at least one seeded hitl_gate_config"
    for config in configs:
        assert config["human_only"] is True, f"seed gate not human-only: {config.get('label')}"


def test_registry_workflows_default_human_only() -> None:
    """Registry workflow primitives with gate configs ship human_only: true."""
    entries = list_registry_primitives()
    found = False
    for entry in entries:
        if entry.primitive_type != "workflow" or not isinstance(entry.content_json, dict):
            continue
        for edge in entry.content_json.get("edges", []):
            if isinstance(edge, dict) and isinstance(edge.get("hitl_gate_config"), dict):
                found = True
                assert edge["hitl_gate_config"]["human_only"] is True
    assert found, "expected at least one registry workflow gate config"
