"""Port-addressed typed state (FAR-416 / FAR-402 F1).

Ports are ADDITIVE metadata over the existing flat ``run_context``/``artifact``
dict. The port->state-key adapter maps a port name to the SAME flat-state key
the node already uses (identity mapping for P2). Because no flat key is renamed,
existing conditional-edge JMESPath that reads ``state.foo`` resolves identically.

This module is the single source of truth for the F1 port model:

* port datatypes + default synthesis (lazy backfill, zero-break)
* the port->state-key identity resolver (used at compile time)
* a deterministic compile-cache structural hash covering port topology
* compile-time fan-in safety + port-type validation helpers
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Stable default port names. A legacy (port-less) node is backfilled with a
# single output port "out" and a single input port "in" — identical to the
# flat-state keys the node already uses today.
DEFAULT_OUTPUT_PORT = "out"
DEFAULT_INPUT_PORT = "in"

# Node type that legitimately accepts many incoming edges (fan-in collectors).
JOIN_NODE_TYPE = "join"

# Stable error codes surfaced by the GraphValidator port rules.
PORT_FAN_IN_ERROR = "PORT_FAN_IN_VIOLATION"
PORT_TYPE_MISMATCH_ERROR = "PORT_TYPE_MISMATCH"


def default_input_ports() -> list[dict[str, Any]]:
    return [{"port": DEFAULT_INPUT_PORT}]


def default_output_ports() -> list[dict[str, Any]]:
    return [{"port": DEFAULT_OUTPUT_PORT}]


def is_port_declared(node: dict[str, Any]) -> bool:
    """True when the node author explicitly supplied port metadata.

    This is the discriminator between a first-class port-modeled node and a
    legacy port-less node. Legacy nodes fall back to raw flat-state keys and are
    NOT subject to strict port rules (zero-break migration guarantee).

    Nodes whose inputs/outputs are None (the API serializer emits the keys with
    null values on round-trip) are legacy port-less nodes — key presence alone
    must not flip a legacy graph into strict port mode (zero-break guarantee).
    """
    return bool(node.get("inputs") or node.get("outputs"))


def node_type_of(node: dict[str, Any]) -> str:
    return str(node.get("node_type") or "agent")


def _validate_port_names(ports: list[dict[str, Any]], direction: str) -> None:
    seen: set[str] = set()
    for p in ports:
        if not isinstance(p, dict):
            raise ValueError(f"{direction} port entry must be an object")
        name = p.get("port")
        if not name or not isinstance(name, str):
            raise ValueError(f"{direction} port requires a non-empty 'port' name")
        if name in seen:
            raise ValueError(f"duplicate {direction} port name '{name}'")
        seen.add(name)


def synthesize_node_ports(node: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *node* with inputs/outputs populated by default ports.

    Lazy backfill: adds ONLY missing required ports; never clobbers a node that
    already declares custom ports. Port identity mapping (port name == flat
    state key) is validated; a port whose name collides with another port on the
    same node is rejected with ``ValueError``.
    """
    node = dict(node)
    inputs = node.get("inputs")
    outputs = node.get("outputs")
    if inputs is None:
        inputs = default_input_ports()
    if outputs is None:
        outputs = default_output_ports()
    _validate_port_names(inputs, "input")
    _validate_port_names(outputs, "output")
    node["inputs"] = inputs
    node["outputs"] = outputs
    return node


def port_to_state_key(port_name: str) -> str:
    """Port->state-key adapter (P2 identity mapping).

    For P2 the port name IS the flat-state key. The runtime reads/writes the
    flat dict unchanged; ports are purely compile-time typed metadata.
    """
    return port_name


def resolve_port_state_key(node: dict[str, Any], port_name: str) -> str:
    """Resolve the flat-state key a given output/input port maps to."""
    return port_to_state_key(port_name)


def _edge_source(edge: dict[str, Any]) -> str:
    return str(edge.get("source") or edge.get("source_node_id") or "")


def _edge_target(edge: dict[str, Any]) -> str:
    return str(edge.get("target") or edge.get("target_node_id") or "")


def _edge_source_port(edge: dict[str, Any]) -> str:
    return str(edge.get("source_port") or DEFAULT_OUTPUT_PORT)


def _edge_target_port(edge: dict[str, Any]) -> str:
    return str(edge.get("target_port") or DEFAULT_INPUT_PORT)


def _edge_declares_ports(edge: dict[str, Any]) -> bool:
    """True when an edge carries NON-DEFAULT explicit port metadata.

    Key presence cannot discriminate: every persisted edge carries
    ``source_port``/``target_port`` (DB NOT NULL defaults ``'out'``/``'in'``,
    migration 0141) and the API's validator-edge serializer always emits
    them, so a legacy graph's edges would all "declare" ports and flip strict
    fan-in enforcement on — the serializer-defaults sibling of the FAR-480
    serializer-nulls bug. Value-based default-equivalent semantics are used,
    matching the normalization ``compute_port_topology_hash`` applies
    (an explicitly-set default value is backfill-equivalent, not a declaration).
    """
    source = edge.get("source_port") or DEFAULT_OUTPUT_PORT
    target = edge.get("target_port") or DEFAULT_INPUT_PORT
    return source != DEFAULT_OUTPUT_PORT or target != DEFAULT_INPUT_PORT


def compute_port_topology_hash(graph_json: dict[str, Any]) -> str:
    """Deterministic structural hash covering port topology + node types.

    Sorted so it does not thrash across semantically-identical graphs. Used to
    extend the compile cache key so a port-topology change forces a recompile.
    """
    nodes = graph_json.get("nodes", []) or []
    node_sig: list[tuple[str, str, list[str], list[str]]] = []
    for n in nodes:
        nid = str(n.get("id", ""))
        node_type = node_type_of(n)
        inputs = sorted(str(p.get("port")) for p in (n.get("inputs") or []))
        outputs = sorted(str(p.get("port")) for p in (n.get("outputs") or []))
        node_sig.append((nid, node_type, inputs, outputs))
    node_sig.sort()

    edges = graph_json.get("edges", []) or []
    edge_sig: list[tuple[str, str, str, str, str]] = []
    for e in edges:
        src = _edge_source(e)
        tgt = _edge_target(e)
        etype = str(e.get("type") or e.get("edge_type") or "")
        sp = _edge_source_port(e)
        tp = _edge_target_port(e)
        edge_sig.append((src, tgt, etype, sp, tp))
    edge_sig.sort()

    payload = json.dumps({"nodes": node_sig, "edges": edge_sig}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate_port_topology(graph_json: dict[str, Any], result: Any) -> None:
    """Compile-time port rules. Mutates *result* with typed issues.

    Rules:
      * Fan-in safety: a non-join target port accepts AT MOST ONE incoming
        edge. Enforced only when the target node declares ports OR the incoming
        edge carries non-default port metadata (an explicitly-set default
        value is backfill-equivalent — see :func:`_edge_declares_ports`) —
        fully-legacy graphs keep their backward-compatible last-write-wins
        fan-in behaviour (zero-break).
      * Port-type validation: when BOTH the source output port and the target
        input port declare a ``schema_ref``, the refs must match. This is the
        typed ``PortMismatchError``. When either is absent (the common P2 case)
        the check is lenient and skipped.
    """
    nodes = graph_json.get("nodes", []) or []
    edges = graph_json.get("edges", []) or []

    node_type_by_id = {str(n.get("id")): node_type_of(n) for n in nodes}
    declares_ports_by_id = {str(n.get("id")): is_port_declared(n) for n in nodes}

    # --- Fan-in safety ----------------------------------------------------
    incoming: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in edges:
        tgt = _edge_target(e)
        tp = _edge_target_port(e)
        incoming.setdefault((tgt, tp), []).append(e)

    for (tgt, tp), es in incoming.items():
        if len(es) <= 1:
            continue
        if node_type_by_id.get(tgt) == JOIN_NODE_TYPE:
            continue  # join nodes legitimately collect many incoming edges
        # Enforce only when the topology is explicitly port-addressed.
        target_declares = declares_ports_by_id.get(tgt, False)
        any_edge_declares = any(_edge_declares_ports(e) for e in es)
        if not (target_declares or any_edge_declares):
            continue  # legacy fan-in: keep backward-compatible behaviour
        srcs = sorted(_edge_source(e) for e in es)
        result.error(
            PORT_FAN_IN_ERROR,
            f"Target port '{tp}' on node '{tgt}' accepts at most one incoming edge "
            f"but receives {len(es)} (from {srcs}). Use a 'join' node to fan in.",
            node_id=tgt,
        )

    # --- Port-type validation (lenient when schema_refs absent) -------------
    out_refs: dict[tuple[str, str], Any] = {}
    in_refs: dict[tuple[str, str], Any] = {}
    for n in nodes:
        nid = str(n.get("id"))
        for p in n.get("outputs") or []:
            # The port->state-key adapter maps each declared output port to the
            # flat-state key it writes. P2 is identity; later phases may remap.
            state_key = resolve_port_state_key(n, str(p["port"]))
            if p.get("schema_ref") is not None:
                out_refs[(nid, state_key)] = p["schema_ref"]
        for p in n.get("inputs") or []:
            state_key = resolve_port_state_key(n, str(p["port"]))
            if p.get("schema_ref") is not None:
                in_refs[(nid, state_key)] = p["schema_ref"]

    for e in edges:
        etype = str(e.get("type") or e.get("edge_type") or "")
        if etype in ("reject", "kickback", "loop"):
            continue
        src = _edge_source(e)
        tgt = _edge_target(e)
        sp = _edge_source_port(e)
        tp = _edge_target_port(e)
        sr = out_refs.get((src, sp))
        tr = in_refs.get((tgt, tp))
        if sr is None or tr is None:
            continue  # lenient: absent schema_ref is not a mismatch
        if sr != tr:
            result.error(
                PORT_TYPE_MISMATCH_ERROR,
                f"Edge {src}:{sp} -> {tgt}:{tp}: output schema_ref '{sr}' is incompatible with input schema_ref '{tr}'",
                node_id=src,
            )
