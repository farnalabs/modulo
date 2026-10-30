"""Shared reader for agent ``connector_type_refs`` entries."""

from __future__ import annotations

from typing import Any


def connector_ref_type_id(ref: Any) -> str:
    """Extract the connector type id from a single connector_type_refs entry.

    Entries are either plain strings (Native library built-ins like
    ``["github"]``) or dicts keyed by ``connector_type_id``, ``connector_type``
    or ``type`` (checked in that order).  Anything else yields ``""``.
    """
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        value = ref.get("connector_type_id") or ref.get("connector_type") or ref.get("type", "")
        return str(value) if value is not None else ""
    return ""
