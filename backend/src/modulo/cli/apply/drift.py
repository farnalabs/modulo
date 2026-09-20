"""Drift mode + reporting for ``modulo apply`` (FAR-681, slice 3).

``modulo apply --diff`` reads the live org state and reports drift against
the declarative config WITHOUT writing anything. It reuses the SAME plan
engine (``plan.build_plan`` + canonical hashes) — the report shape is the
plan report explicitly labelled as drift (``"mode": "drift"``), with a
per-entity breakdown (``"drift_detail"``): node/edge graph detail for
drifted pipelines and a managed-field breakdown for drifted schemas /
model backends / triggers.

Exit-code semantics (CI gate on config drift):
- exit 0 when the org matches the config (all unchanged)
- exit 1 when drift is detected — a ``created`` (declared but absent),
  an ``updated`` (managed-field hash differs) or a ``blocked`` entity
  (drift cannot be evaluated, so the gate must fail loudly)

Runtime state (``next_fire_at``, ``streak_epoch``, spend-limit runtime
fields, ...) is excluded by definition: the slice-2 canonicalisation
restricts the current view to the DESIRED managed keys, so nothing outside
the managed field set can produce drift.
"""

from __future__ import annotations

from typing import Any

from modulo.cli.apply.plan import (
    _CURRENT_VIEWS,
    KIND_BACKEND,
    KIND_PIPELINE,
    KIND_SCHEMA,
    KIND_TRIGGER,
    _trigger_current_view,
)


def _diff_graph(
    desired_graph: dict[str, Any],
    current_graph: dict[str, Any],
) -> dict[str, Any]:
    """Node/edge-level add/remove/modify breakdown between two graphs.

    Nodes/edges are matched by id; ``modified`` lists ids whose payload
    differs (the same whole-graph canonical comparison the plan hash uses,
    broken out per element). Empty graphs are treated as ``{"nodes": [],
    "edges": []}`` for symmetry with the plan engine.
    """

    def _pick(graph: dict[str, Any], key: str) -> dict[str, Any]:
        return {element["id"]: element for element in (graph.get(key) or [])}

    detail: dict[str, Any] = {}
    for key in ("nodes", "edges"):
        desired_elements = _pick(desired_graph, key)
        current_elements = _pick(current_graph, key)
        detail[key] = {
            "added": sorted(i for i in desired_elements if i not in current_elements),
            "removed": sorted(i for i in current_elements if i not in desired_elements),
            "modified": sorted(
                i for i in desired_elements if i in current_elements and desired_elements[i] != current_elements[i]
            ),
        }
    return detail


def _diff_fields(desired: dict[str, Any], current: dict[str, Any]) -> dict[str, list[str]]:
    """Top-level add/remove/modify breakdown between two managed-field views.

    Mirrors the graph breakdown's list-of-ids shape so the renderer drives
    both through the same ``+N/-N/~N`` formula. Keys match by name;
    ``modified`` lists keys present in both views whose values differ.
    Managed views are symmetric by construction (the current side is
    restricted to the desired key set), so ``added``/``removed`` are normally
    empty — the signal is which managed fields are ``modified``.
    """
    return {
        "added": sorted(key for key in desired if key not in current),
        "removed": sorted(key for key in current if key not in desired),
        "modified": sorted(key for key in desired if key in current and desired[key] != current[key]),
    }


def _managed_current_view(
    kind: str,
    name: str,
    desired_view: dict[str, Any],
    current_entities: dict[str, Any],
) -> dict[str, Any] | None:
    """The canonical current view for a non-pipeline entity (plan parity).

    Mirrors ``plan_entity``'s current-view construction so the field diff is
    computed over EXACTLY the values that decided drift — a discrepancy here
    would report a field as ``modified`` while the hash says unchanged (or
    vice versa). Returns None when the entity cannot be compared (absent
    locally), in which case the caller emits no entry.
    """
    current = (current_entities.get(kind) or {}).get(name)
    if current is None:
        return None
    if kind == KIND_TRIGGER:
        return _trigger_current_view(current, desired_view)
    view_builder = _CURRENT_VIEWS.get(kind)
    if view_builder is None:
        return None
    return view_builder(current)


def build_drift_detail(
    current_entities: dict[str, Any],
    desired: dict[str, list[tuple[str, dict[str, Any]]]],
    report: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Per-entity breakdown for the drift report.

    Only UPDATED entities get an entry (the plan already hashed whole
    entities; drift adds the breakdown):

    - a drifted pipeline whose whole-graph hash differs gets a node/edge
      breakdown (``{"nodes": {added/removed/modified}, "edges": {...}}``)
      keyed by pipeline name — a graph-less or top-level-only updated
      pipeline gets no entry (legacy shape, unchanged);
    - a drifted schema / model backend / trigger gets a managed-field
      breakdown (``{"fields": {added/removed/modified}}``) keyed
      ``"<kind>:<name>"`` so a schema or trigger named like a pipeline can
      never collide with the bare-name pipeline key.
    """
    updated = {
        kind: {e["name"] for e in (report.get("updated") or []) if e.get("kind") == kind}
        for kind in (KIND_SCHEMA, KIND_BACKEND, KIND_PIPELINE, KIND_TRIGGER)
    }
    detail: dict[str, dict[str, Any]] = {}

    # Pipeline graph breakdown (legacy shape — keyed by pipeline name).
    desired_pipelines = dict(desired.get(KIND_PIPELINE) or [])
    for name in updated[KIND_PIPELINE]:
        view = desired_pipelines.get(name)
        if not view or "graph" not in view:
            continue
        current = (current_entities.get(KIND_PIPELINE) or {}).get(name) or {}
        current_graph: dict[str, Any] = current.get("graph") or {"nodes": [], "edges": []}
        breakdown = _diff_graph(view["graph"], current_graph)
        if any(side for field in breakdown.values() for side in field.values()):
            detail[name] = breakdown

    # Non-pipeline field breakdown (keyed "<kind>:<name>").
    for kind in (KIND_SCHEMA, KIND_BACKEND, KIND_TRIGGER):
        desired_views = dict(desired.get(kind) or [])
        for name in updated[kind]:
            view = desired_views.get(name)
            if not view:
                continue
            current_view = _managed_current_view(kind, name, view, current_entities)
            if current_view is None:
                continue
            fields = _diff_fields(view, current_view)
            if any(fields.values()):
                detail[f"{kind}:{name}"] = {"fields": fields}
    return detail


def has_drift(report: dict[str, Any]) -> bool:
    """True when drift mode must exit 1.

    ``created`` and ``updated`` are drift by definition. A ``blocked`` entity
    cannot be compared — an unevaluated entity would let a drift gate pass
    while the org is unverified, so it fails the gate too. ``failed`` is
    always empty in drift mode (the mode never writes).
    """
    return bool(report.get("created") or report.get("updated") or report.get("blocked"))


__all__ = ["build_drift_detail", "has_drift"]
