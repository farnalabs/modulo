"""Semantic-diff + impact propagation for pipeline snapshots (FAR-402 P6).

Extends the existing structural snapshot diff with PORT-SIGNATURE deltas and a
deterministic impact-propagation oracle. Purely operates on the graph dictionary
shape stored in ``PipelineSnapshot.graph_json`` (``{"nodes": [...], "edges":
[...]}``) so it is unit-testable without a DB.

Port model (F1 / P2, additive over flat state): a node carries optional
``inputs`` / ``outputs`` port lists; edges optionally carry ``source_port`` /
``target_port``. When ports are absent (legacy, pre-P2 graphs) these helpers
return empty signatures and impact propagation falls back to plain node
reachability — no breaking behaviour for existing graphs.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

# Direction labels used across the diff + impact contract.
_DIR_INPUT = "input"
_DIR_OUTPUT = "output"

# Maps a direction label to the node's JSON key for that port set.
_DIRECTION_NODE_KEY = {_DIR_INPUT: "inputs", _DIR_OUTPUT: "outputs"}

# Change-type labels for edge-port repoints (the edge now reads/writes a
# different port of its endpoint node). These are emitted by
# ``normalise_edge_port_delta`` and handled by the impact/breaking oracle.
_CHANGE_EDGE_SOURCE = "edge_source_port_repoint"
_CHANGE_EDGE_TARGET = "edge_target_port_repoint"


def _port_schema_ref(port: Any) -> str | None:
    """Extract a canonical schema-ref string from a port entry.

    Accepts both shapes: ``{"name", "schema_ref"}`` / ``{"name", "schema_id"}``
    / a bare string (the port name itself, no ref).
    """
    if isinstance(port, str):
        return None
    if isinstance(port, Mapping):
        if port.get("schema_ref") is not None:
            return str(port["schema_ref"])
        if port.get("schema_id") is not None:
            return str(port["schema_id"])
        if port.get("ref") is not None:
            return str(port["ref"])
    return None


def _ports_list(value: Any) -> list[Any]:
    """Normalise a node's ``inputs`` / ``outputs`` value to a list of entries.

    Accepts a list of port dicts, a dict mapping ``name -> schema_ref``, or
    ``None`` (returns ``[]``).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return [{"name": k, "schema_ref": v} for k, v in value.items() if isinstance(v, str)]
    return []


def _port_name(port: Any) -> str:
    if isinstance(port, str):
        return port
    if isinstance(port, Mapping):
        raw = port.get("name")
        if raw is not None:
            return str(raw)
    return ""


def node_port_signature(node: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    """Canonical port signature for a node: ``{input: {name: ref}, output: {...}}``."""
    signature: dict[str, dict[str, str | None]] = {_DIR_INPUT: {}, _DIR_OUTPUT: {}}
    for direction in (_DIR_INPUT, _DIR_OUTPUT):
        node_key = _DIRECTION_NODE_KEY[direction]
        for port in _ports_list(node.get(node_key)):
            name = _port_name(port)
            if not name:
                continue
            signature[direction][name] = _port_schema_ref(port)
    return signature


def diff_node_ports(na: dict[str, Any], nb: dict[str, Any]) -> list[dict[str, Any]]:
    """Port-signature deltas between two versions of the SAME node.

    Returns a list of ``{"node_id", "direction", "port", "change", "old",
    "new"}`` where ``change`` is ``added`` | ``removed`` | ``modified``.
    """
    node_id = str(na.get("id"))
    changes: list[dict[str, Any]] = []
    for direction in (_DIR_INPUT, _DIR_OUTPUT):
        old_ports = node_port_signature(na)[direction]
        new_ports = node_port_signature(nb)[direction]
        old_names = set(old_ports)
        new_names = set(new_ports)
        for name in new_names - old_names:
            changes.append(
                {
                    "node_id": node_id,
                    "direction": direction,
                    "port": name,
                    "change": "added",
                    "old": None,
                    "new": new_ports[name],
                }
            )
        for name in old_names - new_names:
            changes.append(
                {
                    "node_id": node_id,
                    "direction": direction,
                    "port": name,
                    "change": "removed",
                    "old": old_ports[name],
                    "new": None,
                }
            )
        for name in old_names & new_names:
            old_ref = old_ports[name]
            new_ref = new_ports[name]
            if old_ref != new_ref:
                changes.append(
                    {
                        "node_id": node_id,
                        "direction": direction,
                        "port": name,
                        "change": "modified",
                        "old": old_ref,
                        "new": new_ref,
                    }
                )
    return changes


def diff_edge_ports(ea: dict[str, Any], eb: dict[str, Any]) -> dict[str, Any]:
    """Port-field deltas between two versions of the SAME edge.

    Returns ``{"source_port": {old,new}, "target_port": {old,new}, ...}`` with
    only the fields that actually changed.
    """
    changes: dict[str, Any] = {}
    for field in ("source_port", "target_port"):
        old_val = ea.get(field)
        new_val = eb.get(field)
        if old_val != new_val:
            changes[field] = {"old": old_val, "new": new_val}
    return changes


def normalise_edge_port_delta(edge: dict[str, Any], delta: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an edge-port delta (from ``diff_edge_ports``) into normalised
    port-change entries so it propagates through the impact/breaking oracle.

    ``diff_edge_ports`` returns ``{"source_port": {old,new}, ...}`` with no
    ``node_id`` — the oracle's ``_normalise_changed_ports`` drops such entries,
    which previously produced a false "safe" signal for edge repoints. We
    attribute each field to the endpoint node it actually affects:

    * ``source_port`` change -> the edge now reads a different OUTPUT port of its
      source node (impact propagates downstream from the source).
    * ``target_port`` change -> the edge now writes to a different INPUT port of
      its target node (impact propagates downstream from the target).
    """
    src = edge.get("source") or edge.get("source_node_id")
    tgt = edge.get("target") or edge.get("target_node_id")
    changes: list[dict[str, Any]] = []
    if "source_port" in delta:
        changes.append(
            {
                "node_id": str(src),
                "direction": _DIR_OUTPUT,
                "port": delta["source_port"]["new"],
                "change": _CHANGE_EDGE_SOURCE,
                "old": delta["source_port"]["old"],
                "new": delta["source_port"]["new"],
                "edge": {"source": str(src), "target": str(tgt)},
            }
        )
    if "target_port" in delta:
        changes.append(
            {
                "node_id": str(tgt),
                "direction": _DIR_INPUT,
                "port": delta["target_port"]["new"],
                "change": _CHANGE_EDGE_TARGET,
                "old": delta["target_port"]["old"],
                "new": delta["target_port"]["new"],
                "edge": {"source": str(src), "target": str(tgt)},
            }
        )
    return changes


def _edge_source(edge: dict[str, Any]) -> str | None:
    raw = edge.get("source") or edge.get("source_node_id")
    return str(raw) if raw is not None else None


def _edge_target(edge: dict[str, Any]) -> str | None:
    raw = edge.get("target") or edge.get("target_node_id")
    return str(raw) if raw is not None else None


def _normalise_changed_ports(
    changed_ports: Iterable[Any],
) -> list[tuple[str, str, str | None, str | None]]:
    """Normalise changed-port entries to ``(node_id, direction, port, change)``.

    Accepts ``{"node_id", "direction"?, "port"?, "change"?}`` dicts or positional
    tuples whose first element is the node id. Unset values become ``None`` so a
    bare node id still scopes impact to the whole node + its downstream.
    """
    result: list[tuple[str, str, str | None, str | None]] = []
    for item in changed_ports:
        if isinstance(item, Mapping):
            node_id = item.get("node_id")
            if node_id is None:
                continue
            result.append(
                (
                    str(node_id),
                    str(item.get("direction") or ""),
                    str(item["port"]) if item.get("port") is not None else None,
                    str(item["change"]) if item.get("change") is not None else None,
                )
            )
        else:
            if not item:
                continue
            result.append((str(item[0]), "", None, None))
    return result


def compute_port_change_impact(graph: dict[str, Any], changed_ports: Iterable[Any]) -> set[str]:
    """Deterministic impact oracle: which downstream nodes a port change affects.

    BFS from the node owning each changed port along outgoing edges, returning
    the set of node ids whose execution/result could be affected (the changed
    node itself plus every transitive downstream node). A changed port whose
    node is not in the graph is ignored. Given a graph + the changed ports, the
    impacted set is fully determined — this is the unit-testable "which
    downstream nodes break" oracle.
    """
    node_ids = {str(n.get("id")) for n in graph.get("nodes", []) if n.get("id")}
    adjacency: dict[str, set[str]] = {}
    for edge in graph.get("edges", []):
        src = _edge_source(edge)
        tgt = _edge_target(edge)
        if src is None or tgt is None:
            continue
        adjacency.setdefault(src, set()).add(tgt)

    impacted: set[str] = set()
    for node_id, _direction, _port, _change in _normalise_changed_ports(changed_ports):
        if node_id not in node_ids or node_id in impacted:
            continue
        visited: set[str] = set()
        queue: deque[str] = deque([node_id])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for nxt in adjacency.get(current, ()):
                if nxt not in visited:
                    queue.append(nxt)
        impacted |= visited
    return impacted


def _check_edge_repoint_breaking(
    entry: Mapping[Any, Any],
    graph_new: dict[str, Any],
) -> list[dict[str, Any]]:
    """Breaking check for an edge-port repoint (``normalise_edge_port_delta``).

    The edge now reads a different port of its endpoint node. It is breaking
    when the new port the edge points at is undeclared (the data read is
    dropped — ``block``) or when the new port's schema-ref differs from the old
    one (the data read may alter — ``warning``).
    """
    node_id = str(entry.get("node_id"))
    direction = entry.get("direction")
    if direction not in (_DIR_INPUT, _DIR_OUTPUT):
        return []
    new_port = entry.get("port")
    old_port = entry.get("old")
    edge = entry.get("edge") or {}
    src = str(edge.get("source")) if edge.get("source") is not None else None
    tgt = str(edge.get("target")) if edge.get("target") is not None else None

    new_nodes = {str(n.get("id")): n for n in graph_new.get("nodes", []) if n.get("id")}
    node = new_nodes.get(node_id)
    if node is None:
        return []

    declared = node_port_signature(node)[direction]
    findings: list[dict[str, Any]] = []
    if new_port not in declared:
        findings.append(
            {
                "severity": "block",
                "node_id": node_id,
                "direction": direction,
                "port": new_port,
                "edge": {"source": src, "target": tgt},
                "reason": (
                    f"edge {src} -> {tgt} reads {direction} port '{new_port}' of node "
                    f"{node_id}, which the new graph does not declare — the data read is dropped"
                ),
            }
        )
    elif old_port is not None and old_port in declared and declared[old_port] != declared[new_port]:
        findings.append(
            {
                "severity": "warning",
                "node_id": node_id,
                "direction": direction,
                "port": new_port,
                "edge": {"source": src, "target": tgt},
                "reason": (
                    f"edge {src} -> {tgt} changed the {direction} port it reads from "
                    f"'{old_port}' to '{new_port}'; the schema-ref differs — the data read may alter"
                ),
            }
        )
    return findings


def check_port_change_breaking(
    graph_new: dict[str, Any],
    changed_ports: Iterable[Any],
) -> list[dict[str, Any]]:
    """Save-time breaking-change check for a port-signature change.

    Flags edges whose port contract a port change breaks:

    * ``block`` — an edge reads a port that the new graph no longer declares
      (the downstream edge's data would be dropped). Includes port-less
      (legacy) edges when the node's default output/input port is removed.
    * ``warning`` — an edge reads a port whose schema-ref changed (the data
      read may alter, but nothing is dropped).

    Returns an ordered list of findings; an empty list means the change is safe.
    """
    new_nodes = {str(n.get("id")): n for n in graph_new.get("nodes", []) if n.get("id")}
    signatures = {nid: node_port_signature(node) for nid, node in new_nodes.items()}
    new_edges = list(graph_new.get("edges", []))
    findings: list[dict[str, Any]] = []

    def _consuming_edges(node_id: str, direction: str) -> list[dict[str, Any]]:
        if direction == _DIR_OUTPUT:
            return [e for e in new_edges if _edge_source(e) == node_id]
        return [e for e in new_edges if _edge_target(e) == node_id]

    for item in changed_ports:
        # Edge-port repoints carry their own semantics (the edge now reads a
        # different port of its endpoint node) and are handled separately so the
        # node-signature oracle below does not silently ignore them.
        if isinstance(item, Mapping) and item.get("change") in (
            _CHANGE_EDGE_SOURCE,
            _CHANGE_EDGE_TARGET,
        ):
            findings.extend(_check_edge_repoint_breaking(item, graph_new))
            continue
        for node_id, direction, port, change in _normalise_changed_ports([item]):
            if node_id not in signatures:
                continue
            # The node's port set for this direction is now empty — the default
            # port a legacy (port-less) edge reads has disappeared.
            now_empty = not signatures[node_id][direction]

            for edge in _consuming_edges(node_id, direction):
                ref = edge.get("source_port") if direction == _DIR_OUTPUT else edge.get("target_port")
                ref = str(ref) if ref is not None else None
                src = _edge_source(edge)
                tgt = _edge_target(edge)

                if ref is None:
                    # Port-less (legacy) edge reads the node's default port.
                    if change == "removed" and now_empty:
                        findings.append(
                            {
                                "severity": "block",
                                "node_id": node_id,
                                "direction": direction,
                                "port": port,
                                "edge": {"source": src, "target": tgt},
                                "reason": (
                                    f"edge {src} -> {tgt} reads the default {direction} of node "
                                    f"{node_id}, which no longer declares any {direction} port"
                                ),
                            }
                        )
                    continue

                if ref != port:
                    continue

                if change == "removed":
                    findings.append(
                        {
                            "severity": "block",
                            "node_id": node_id,
                            "direction": direction,
                            "port": port,
                            "edge": {"source": src, "target": tgt},
                            "reason": (
                                f"edge {src} -> {tgt} reads {direction} port '{ref}' of node "
                                f"{node_id}, which the new node no longer declares"
                            ),
                        }
                    )
                elif change == "modified":
                    findings.append(
                        {
                            "severity": "warning",
                            "node_id": node_id,
                            "direction": direction,
                            "port": port,
                            "edge": {"source": src, "target": tgt},
                            "reason": (
                                f"edge {src} -> {tgt} reads {direction} port '{ref}' of node "
                                f"{node_id}, whose schema-ref changed — the data read may alter"
                            ),
                        }
                    )
    return findings
