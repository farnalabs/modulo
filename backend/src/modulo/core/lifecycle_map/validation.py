"""Validation and canonicalisation of lifecycle-map ``content_json``.

The canonical stored shape uses ``type`` / ``source`` / ``target`` (matching
the PRD §8.31.9 primitive model). The visual editor POSTs the aliases
``stage_type`` / ``source_stage_id`` / ``target_stage_id`` (and the store's
canvas payload uses ``source`` / ``target``), so every save path normalises the
payload to the canonical shape before it touches ``lifecycle_maps.content_json``.

``normalize_content`` is a pure function — no DB, no I/O — so it can be unit
tested in isolation and reused by the routes and the service layer.
"""

from __future__ import annotations

import uuid
from typing import Any

STAGE_TYPES = frozenset({"modulo", "external", "manual", "placeholder"})

_STAGE_TYPE_KEYS = ("type", "stage_type")
_SOURCE_KEYS = ("source", "source_stage_id", "from_stage_id")
_TARGET_KEYS = ("target", "target_stage_id", "to_stage_id")
_EDGE_ALIAS_KEYS = ("source_stage_id", "target_stage_id", "from_stage_id", "to_stage_id")

_UUID_RE = uuid.UUID


class LifecycleMapContentError(ValueError):
    """Raised when a lifecycle-map content payload fails shape validation."""


class LifecycleMapPipelineConflictError(LifecycleMapContentError):
    """Raised when a save would register a pipeline as a stage of two active maps."""


def _require_non_empty_str(value: Any, *, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleMapContentError(f"lifecycle-map stage/edge #{index}: {field!r} must be a non-empty string")
    return value


def _normalise_pipeline_id(value: Any, *, index: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LifecycleMapContentError(f"lifecycle-map stage #{index}: 'pipeline_id' must be a string or null")
    raw = value.strip()
    try:
        _UUID_RE(raw)
    except ValueError:
        raise LifecycleMapContentError(
            f"lifecycle-map stage #{index}: 'pipeline_id' {raw!r} is not a valid UUID"
        ) from None
    return raw


def _normalise_stage(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LifecycleMapContentError(f"lifecycle-map stage #{index} must be an object")
    stage: dict[str, Any] = dict(raw)

    stage["id"] = _require_non_empty_str(stage.get("id"), field="id", index=index)
    stage["name"] = _require_non_empty_str(stage.get("name"), field="name", index=index)

    stage_type: Any = None
    for key in _STAGE_TYPE_KEYS:
        if key in stage and stage.get(key) is not None:
            stage_type = stage.get(key)
            break
    if not isinstance(stage_type, str) or stage_type not in STAGE_TYPES:
        raise LifecycleMapContentError(
            f"lifecycle-map stage #{index}: 'type' must be one of {sorted(STAGE_TYPES)}, got {stage_type!r}"
        )
    stage["type"] = stage_type

    if "pipeline_id" in stage:
        stage["pipeline_id"] = _normalise_pipeline_id(stage.get("pipeline_id"), index=index)

    # Drop the editor alias key so content_json stays canonical.
    stage.pop("stage_type", None)
    return stage


def _normalise_edge(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LifecycleMapContentError(f"lifecycle-map edge/transition #{index} must be an object")
    edge: dict[str, Any] = dict(raw)

    edge["id"] = _require_non_empty_str(edge.get("id"), field="id", index=index)

    source: Any = None
    for key in _SOURCE_KEYS:
        if key in edge and edge.get(key) is not None:
            source = edge.get(key)
            break
    edge["source"] = _require_non_empty_str(source, field="source", index=index)

    target: Any = None
    for key in _TARGET_KEYS:
        if key in edge and edge.get(key) is not None:
            target = edge.get(key)
            break
    edge["target"] = _require_non_empty_str(target, field="target", index=index)

    for alias in _EDGE_ALIAS_KEYS:
        edge.pop(alias, None)
    return edge


def normalize_content(content: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and canonicalise a lifecycle-map ``content_json`` payload.

    Only the keys present in the payload are validated and normalised — an
    empty payload stays ``{}`` (a new map starts with no stages). Keys that are
    absent are not injected, so existing content written before validation
    (e.g. ``{"stages": [...]}``) round-trips unchanged apart from the alias
    normalisation.

    Returns a new dict. Raises :class:`LifecycleMapContentError` with a
    human-readable message naming the offending field when the shape is
    invalid.
    """
    if content is None:
        content = {}
    if not isinstance(content, dict):
        raise LifecycleMapContentError("content_json must be an object")

    result: dict[str, Any] = dict(content)

    if "stages" in content:
        stages_raw = content["stages"]
        if not isinstance(stages_raw, list):
            raise LifecycleMapContentError("content_json.stages must be an array")
        result["stages"] = [_normalise_stage(s, i) for i, s in enumerate(stages_raw)]

    if "edges" in content or "transitions" in content:
        edges_raw = content.get("edges")
        if edges_raw is None:
            edges_raw = content.get("transitions")
        if not isinstance(edges_raw, list):
            raise LifecycleMapContentError("content_json.edges/transitions must be an array")
        result["edges"] = [_normalise_edge(e, i) for i, e in enumerate(edges_raw)]
        result.pop("transitions", None)

    if "notes" in content:
        notes = content["notes"]
        if notes is not None and not isinstance(notes, str):
            raise LifecycleMapContentError("content_json.notes must be a string")
        result["notes"] = notes if isinstance(notes, str) else ""

    return result
