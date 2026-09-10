"""Drift mode + reporting for ``modulo apply`` (FAR-681, slice 3).

``modulo apply --diff`` reads the live org state and reports drift against
the declarative config WITHOUT writing anything. It reuses the SAME plan
engine (``plan.build_plan`` + canonical hashes) — the report shape is the
plan report explicitly labelled as drift (``"mode": "drift"``), with a
node-level graph breakdown for drifted pipelines (``"drift_detail"``).

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

from modulo.cli.apply.plan import KIND_PIPELINE


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


def build_drift_detail(
    current_entities: dict[str, Any],
    desired: dict[str, list[tuple[str, dict[str, Any]]]],
    report: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Per-pipeline node-level breakdown for the drift report.

    Only UPDATED pipelines whose whole-graph hash differs get an entry (the
    plan already hashed whole graphs; drift adds the breakdown). A graph-less
    updated pipeline never manages the graph, so it gets no entry — drift is
    top-level fields only.
    """
    updated_names = {e["name"] for e in (report.get("updated") or []) if e.get("kind") == KIND_PIPELINE}
    desired_views = dict(desired.get(KIND_PIPELINE) or [])
    detail: dict[str, dict[str, Any]] = {}
    for name in updated_names:
        view = desired_views.get(name)
        if not view or "graph" not in view:
            continue
        current = (current_entities.get(KIND_PIPELINE) or {}).get(name) or {}
        current_graph: dict[str, Any] = current.get("graph") or {"nodes": [], "edges": []}
        breakdown = _diff_graph(view["graph"], current_graph)
        if any(side for field in breakdown.values() for side in field.values()):
            detail[name] = breakdown
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
