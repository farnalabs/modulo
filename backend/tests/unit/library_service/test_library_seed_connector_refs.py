"""Seeded agent connector_type_refs must use the object form, never bare strings."""

from __future__ import annotations

from typing import Any

from modulo.core.library_service import _MODULO_PRIMITIVES


def _seeded_agent_entries() -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for prim in _MODULO_PRIMITIVES:
        content = prim.content_json or {}
        if prim.primitive_type == "agent":
            entries.append((prim.slug, content))
        for nested in content.get("agents", []) if prim.primitive_type == "pipeline_template" else []:
            entries.append((f"{prim.slug}/{nested.get('name')}", nested))
    return entries


def test_seeded_agents_were_found() -> None:
    assert len(_seeded_agent_entries()) > 5


def test_no_seeded_agent_has_bare_string_connector_ref() -> None:
    bare = [
        (label, ref)
        for label, entry in _seeded_agent_entries()
        for ref in entry.get("connector_type_refs", [])
        if not isinstance(ref, dict)
    ]
    assert not bare, f"bare-string connector_type_refs: {bare}"


def test_seeded_agent_refs_have_type_and_capabilities() -> None:
    bad = [
        (label, ref)
        for label, entry in _seeded_agent_entries()
        for ref in entry.get("connector_type_refs", [])
        if not (isinstance(ref.get("connector_type"), str) and isinstance(ref.get("capabilities"), list))
    ]
    assert not bad, f"malformed refs: {bad}"
