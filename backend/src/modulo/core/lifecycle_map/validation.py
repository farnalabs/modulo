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


def _require_bounded_str(value: Any, *, field: str, index: int, max_length: int) -> str:
    """Require a non-empty string no longer than *max_length* characters.

    Stage ``id``/``name`` flow into the ``String(255)`` junction columns
    (``lifecycle_map_stages.stage_id`` / ``stage_name``), so unbounded values
    would raise ``DataError`` at flush time and surface as a 503 instead of a
    friendly 422 validation error.
    """
    text = _require_non_empty_str(value, field=field, index=index)
    if len(text) > max_length:
        raise LifecycleMapContentError(
            f"lifecycle-map stage #{index}: {field!r} must be at most {max_length} characters (got {len(text)})"
        )
    return text


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

    stage["id"] = _require_bounded_str(stage.get("id"), field="id", index=index, max_length=255)
    stage["name"] = _require_bounded_str(stage.get("name"), field="name", index=index, max_length=200)

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


def _find_transition_cycle(edges: list[dict[str, Any]]) -> list[str] | None:
    """Return a stage-id cycle path in *edges*, or ``None`` when acyclic.

    The lifecycle map is a DAG (PRD §8.31.4), so a transition cycle is an
    invalid graph structure. Uses depth-first search with three-state colouring
    to detect back edges; the returned path starts at the cycle entry node and
    closes with the repeated node (e.g. ``["s1", "s2", "s1"]``). A self-loop
    (``source == target``) is reported as a two-element path ``[n, n]``.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source"], []).append(edge["target"])
        adjacency.setdefault(edge["target"], [])

    unvisited, in_progress, done = 0, 1, 2
    colour: dict[str, int] = {}
    path: list[str] = []

    def _visit(node: str) -> list[str] | None:
        colour[node] = in_progress
        path.append(node)
        for next_stage in adjacency.get(node, ()):
            if colour.get(next_stage, unvisited) == in_progress:
                try:
                    start = path.index(next_stage)
                except ValueError:
                    start = 0
                return [*path[start:], next_stage]
            if colour.get(next_stage, unvisited) == unvisited:
                cycle = _visit(next_stage)
                if cycle is not None:
                    return cycle
        path.pop()
        colour[node] = done
        return None

    for stage_id in adjacency:
        if colour.get(stage_id, unvisited) == unvisited:
            cycle = _visit(stage_id)
            if cycle is not None:
                return cycle
    return None


def _validate_graph_structure(stages: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Reject invalid stage/transition graph structure with specific messages.

    Runs after the per-stage/per-edge shape checks so the graph invariants are
    validated on canonical keys (``type``/``source``/``target``). A lifecycle
    map is a DAG of uniquely-identified stages (PRD §8.31.4), so this enforces:

    * unique stage ids (a duplicate breaks stage identity and junction rows)
    * unique edge ids (a duplicate breaks editor selection and round-trips)
    * every edge endpoint names a defined stage (no dangling references)
    * acyclic transitions (back-edge detection, existing ``_find_transition_cycle``)

    Each violation raises :class:`LifecycleMapContentError` naming the
    offending ids so the routes surface a specific 422 rather than a generic
    "invalid content" message.
    """
    seen_stage_ids: dict[str, int] = {}
    for index, stage in enumerate(stages):
        stage_id = stage["id"]
        if stage_id in seen_stage_ids:
            raise LifecycleMapContentError(
                f"lifecycle-map stage #{index}: duplicate stage id {stage_id!r} "
                f"(already used by stage #{seen_stage_ids[stage_id]})"
            )
        seen_stage_ids[stage_id] = index

    seen_edge_ids: dict[str, int] = {}
    for index, edge in enumerate(edges):
        edge_id = edge["id"]
        if edge_id in seen_edge_ids:
            raise LifecycleMapContentError(
                f"lifecycle-map edge/transition #{index}: duplicate edge id {edge_id!r} "
                f"(already used by edge/transition #{seen_edge_ids[edge_id]})"
            )
        seen_edge_ids[edge_id] = index

    stage_ids = frozenset(seen_stage_ids)
    for index, edge in enumerate(edges):
        source = edge["source"]
        target = edge["target"]
        if source not in stage_ids:
            raise LifecycleMapContentError(
                f"lifecycle-map edge/transition #{index} (id {edge['id']!r}): source stage {source!r} "
                "is not defined in stages"
            )
        if target not in stage_ids:
            raise LifecycleMapContentError(
                f"lifecycle-map edge/transition #{index} (id {edge['id']!r}): target stage {target!r} "
                "is not defined in stages"
            )

    cycle = _find_transition_cycle(edges)
    if cycle is not None:
        raise LifecycleMapContentError("lifecycle-map content: stage transitions form a cycle: " + " -> ".join(cycle))


def _edge_endpoint(edge: dict[str, Any], kind: str) -> str | None:
    """Resolve an edge's source/target, honouring the editor alias keys."""
    keys = _SOURCE_KEYS if kind == "source" else _TARGET_KEYS
    for key in keys:
        value = edge.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _can_reach(adjacency: dict[str, set[str]], start: str, goal: str) -> bool:
    """True when *goal* is reachable from *start* in *adjacency* (BFS).

    A self-loop is reachable trivially, so ``_can_reach(g, n, n)`` returns True.
    """
    if start == goal:
        return True
    stack = [start]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for nxt in adjacency.get(node, ()):
            if nxt == goal:
                return True
            stack.append(nxt)
    return False


def _clean_stages(
    raw_stages: list[Any],
    changes: list[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Drop non-dict/id-less/duplicate stages, returning the survivors + ids."""
    cleaned: list[dict[str, Any]] = []
    stage_ids: set[str] = set()
    for index, stage in enumerate(raw_stages):
        if not isinstance(stage, dict):
            changes.append(f"dropped stage #{index} (not an object)")
            continue
        stage_id = stage.get("id")
        if not isinstance(stage_id, str) or not stage_id.strip():
            changes.append(f"dropped stage #{index} (missing or non-string id)")
            continue
        if stage_id in stage_ids:
            changes.append(f"dropped duplicate stage id {stage_id!r} (#{index})")
            continue
        stage_ids.add(stage_id)
        cleaned.append(stage)
    return cleaned, stage_ids


def _clean_edges(
    raw_edges: list[Any],
    stage_ids: set[str],
    changes: list[str],
) -> list[dict[str, Any]]:
    """Drop non-dict/id-less/duplicate/dangling edges, returning the survivors."""
    seen_edge_ids: set[str] = set()
    valid_edges: list[dict[str, Any]] = []
    for index, edge in enumerate(raw_edges):
        if not isinstance(edge, dict):
            changes.append(f"dropped edge #{index} (not an object)")
            continue
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id.strip():
            changes.append(f"dropped edge #{index} (missing or non-string id)")
            continue
        if edge_id in seen_edge_ids:
            changes.append(f"dropped duplicate edge id {edge_id!r} (#{index})")
            continue
        seen_edge_ids.add(edge_id)
        source = _edge_endpoint(edge, "source")
        target = _edge_endpoint(edge, "target")
        if source is None or target is None:
            changes.append(f"dropped edge {edge_id!r} (missing source/target)")
            continue
        if source not in stage_ids:
            changes.append(f"dropped dangling edge {edge_id!r} (source {source!r} not defined)")
            continue
        if target not in stage_ids:
            changes.append(f"dropped dangling edge {edge_id!r} (target {target!r} not defined)")
            continue
        valid_edges.append(edge)
    return valid_edges


def _break_transition_cycles(valid_edges: list[dict[str, Any]], changes: list[str]) -> list[dict[str, Any]]:
    """Greedily drop edges that close a transition cycle (kept graph acyclic)."""
    adjacency: dict[str, set[str]] = {}
    acyclic_edges: list[dict[str, Any]] = []
    for edge in valid_edges:
        source = _edge_endpoint(edge, "source") or ""
        target = _edge_endpoint(edge, "target") or ""
        if _can_reach(adjacency, target, source):
            changes.append(f"dropped cycle-closing edge {edge.get('id')!r} ({source} -> {target})")
            continue
        acyclic_edges.append(edge)
        adjacency.setdefault(source, set()).add(target)
    return acyclic_edges


def clean_legacy_content(content: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Repair legacy ``content_json`` so it passes :func:`normalize_content` again.

    FAR-175 tightened graph validation to reject dangling edges and duplicate
    stage/edge ids with a 422. Maps whose ``content_json`` was written before
    that validation now fail EVERY content edit -- including graduation --
    because ``save_map_version`` / ``graduate_stage`` re-validate the stored
    graph before writing. This pure helper repairs such legacy content so the
    one-off backfill script (``backend/scripts/
    backfill_lifecycle_map_legacy_content.py``) can unblock those maps:

    * drops duplicate stage ids (keeps the first occurrence) and
      non-dict / id-less stages
    * drops duplicate edge ids (keeps the first occurrence) and
      non-dict / id-less edges
    * drops edges whose source/target names an undefined stage (dangling)
    * greedily drops edges that close a transition cycle (edges are processed
      in order, keeping the graph acyclic; a self-loop is dropped)
    * drops the ``transitions`` alias so the result is canonical

    Non-repairable shapes (``stages``/``edges`` present but not arrays) are
    returned unchanged with a change note so the caller can flag them for
    manual repair. Every other key (``notes``, metadata) is preserved.

    Returns ``(cleaned_content, changes)`` where each entry in *changes* is a
    human-readable description of one dropped/fixed element. Callers MUST
    verify the result with :func:`normalize_content` before persisting.
    """
    changes: list[str] = []
    if content is None:
        return {}, []
    if not isinstance(content, dict):
        return {}, ["content_json is not an object; left for manual repair"]
    result: dict[str, Any] = dict(content)

    if "stages" in result and not isinstance(result["stages"], list):
        changes.append(
            f"content_json.stages must be an array (got {type(result['stages']).__name__}); left for manual repair"
        )
        return result, changes

    if "stages" in result:
        cleaned_stages, stage_ids = _clean_stages(result["stages"], changes)
        result["stages"] = cleaned_stages
    else:
        stage_ids = set()

    if "edges" not in result and "transitions" not in result:
        return result, changes

    edges_raw = result.get("edges")
    if edges_raw is None:
        edges_raw = result.get("transitions")
    if not isinstance(edges_raw, list):
        changes.append(
            f"content_json.edges/transitions must be an array (got {type(edges_raw).__name__}); left for manual repair"
        )
        return result, changes

    valid_edges = _clean_edges(edges_raw, stage_ids, changes)
    acyclic_edges = _break_transition_cycles(valid_edges, changes)

    result["edges"] = acyclic_edges
    result.pop("transitions", None)
    return result, changes


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

    if "stages" in content or "edges" in content or "transitions" in content:
        _validate_graph_structure(result.get("stages", []), result.get("edges", []))

    if "notes" in content:
        notes = content["notes"]
        if notes is not None and not isinstance(notes, str):
            raise LifecycleMapContentError("content_json.notes must be a string")
        result["notes"] = notes if isinstance(notes, str) else ""

    return result
