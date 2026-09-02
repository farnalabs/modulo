"""Per-node run output split helpers (FAR-124, P0).

Today ``run.outputs_json[node_id]`` is a MIXED envelope: sandbox_agent nodes
bury the agent's structured return at ``artifacts[0].output.output_json`` with
runtime telemetry flattened around it; non-sandbox nodes persist
``{artifacts, output}``; connector/stub/HITL/manual/connector-failure nodes
each have their own shape; recovery markers persist
``{input, output, recovered|skipped}``.

Future phases (P1/P2) write the PURE agent return to ``outputs_json[node_id]``
and exhaustive runtime telemetry to a NEW ``node_telemetry_json[node_id]``
column. This module is P0: it builds the helpers for that split WITHOUT
changing any persisted shape, and routes every reader through them with
byte-identical output for today's legacy rows, so P1's write-flip cannot break
readers.

- ``split_node_output`` -- the WRITE-side splitter. Not called by production
  writers until P1; fully unit-tested now. Never raises: unknown or malformed
  envelopes produce a warning and a best-effort split.
- ``node_return`` / ``node_telemetry`` -- the READ-side accessors. For legacy
  rows ``node_return`` returns ``outputs_json[node_id]`` verbatim and
  ``node_telemetry`` returns exactly what today's cost readers extract via
  ``finalize._node_output_dict``.
- ``extend_node_type_map_from_edges`` -- stamps ``"gate"`` entries for the HITL
  gate nodes synthesized at graph-compile time (absent from
  ``graph_json.nodes``) so gate envelopes can be resolved from a type map.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_NODE_TYPE",
    "NODE_TYPE_GATE",
    "SPLITTABLE_NODE_TYPES",
    "extend_node_type_map_from_edges",
    "node_return",
    "node_telemetry",
    "resolve_node_contract_output",
    "split_node_output",
]

#: Node-type value used for HITL gate envelopes in extended type maps.
NODE_TYPE_GATE = "gate"

#: Node type assumed when a graph node declares none (matches the compile-time
#: default in ``build_graph_from_json`` / ``_split_agent``).
DEFAULT_NODE_TYPE = "agent"

#: Node types with a dedicated WRITE-side splitter in ``split_node_output``.
#: Only these types can be resolved to a contract output from a live envelope;
#: anything else keeps the legacy inner-``output`` read.
SPLITTABLE_NODE_TYPES = frozenset(
    {
        "sandbox_agent",
        "agent",
        "connector",
        "manual",
        NODE_TYPE_GATE,
    }
)

#: The exhaustive telemetry field vocabulary -- every field that may appear in
#: ``node_telemetry_json[node_id]``. Envelope fields NOT in this vocabulary are
#: still preserved (lossless): they are folded into telemetry verbatim.
TELEMETRY_FIELDS = frozenset(
    {
        "status",
        "summary",
        "exit_code",
        "wall_clock_time_ms",
        "cost_estimate_usd",
        "model_cost_usd",
        "model_cost_raw_usd",
        "model_cost_clamped",
        "model_cost_out_of_band_high",
        "model_cost_display_usd",
        # FAR-491 agent-reported token usage (display-only — never an input to
        # the system's built-in money math; operator formulas may reference
        # them).
        "model_tokens_input",
        "model_tokens_output",
        "model_tokens_total",
        "model_tokens_cache_read",
        "model_tokens_cache_write",
        "agent_stdout",
        "agent_stderr",
        "stdout_length",
        "stderr_length",
        "stall_reason",
        "sandbox_id",
        "sandbox_log_tail",
        "error_type",
        "error_message",
        "human_data",
        "human_output",
        "pin_failed",
        "recovered",
        "skipped",
        "recovery_input",
    }
)

#: ``artifacts[0]`` keys surfaced into telemetry (lossless for the artifact
#: wrapper; the value may be duplicated with the return where the spec pins it,
#: e.g. a gate's ``human_data``).
_ARTIFACT_TELEMETRY_KEYS = (
    "status",
    "result",
    "human_data",
    "human_output",
    "error",
    "autonomy",
    "condition",
    "condition_result",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _first_artifact(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """The first artifact dict of a legacy envelope, or ``None``."""
    artifacts = envelope.get("artifacts")
    if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
        return artifacts[0]
    return None


def _surface_artifact_fields(a0: dict[str, Any] | None, telemetry: dict[str, Any]) -> None:
    """Copy known telemetry fields from the first artifact into *telemetry*."""
    if a0 is None:
        return
    for key in _ARTIFACT_TELEMETRY_KEYS:
        if key in a0 and key not in telemetry:
            telemetry[key] = a0[key]


def _add_lossless(
    envelope: dict[str, Any],
    telemetry: dict[str, Any],
    excluded: set[str],
) -> None:
    """Fold every not-yet-captured top-level envelope key into *telemetry*.

    *excluded* names the envelope keys whose content is already represented
    (consumed as the return value or decomposed into telemetry); everything
    else lands verbatim so no information is dropped.
    """
    for key, value in envelope.items():
        if key in excluded:
            continue
        if key not in telemetry:
            telemetry[key] = value


def _legacy_inner_output(outputs_json: Any, node_id: str) -> dict[str, Any] | None:
    """The inner ``output`` dict of a completed legacy node.

    Mirrors ``finalize._node_output_dict`` EXACTLY (kept in lock-step): the
    node value's ``output`` key when it is a dict, otherwise the whole node
    value. ``None`` when the node is absent or the value is not a dict.
    """
    if not isinstance(outputs_json, dict):
        return None
    node_output = outputs_json.get(node_id)
    if not isinstance(node_output, dict):
        return None
    inner = node_output.get("output")
    return inner if isinstance(inner, dict) else node_output


def resolve_node_contract_output(
    envelope: dict[str, Any],
    node_type: str | None,
) -> tuple[bool, Any]:
    """Resolve a node envelope's CONTRACT output for eval validation (FAR-311).

    Returns ``(found, contract_output)``. The contract output is the pure
    return users see as the node return — NOT the telemetry-style outer
    ``output`` envelope, which for a sandbox_agent carries status/summary/cost
    but never the agent's real fields (``pr_url`` / ``changed_files`` live in
    ``artifacts[0].output.output_json``). Only SPLITTABLE_NODE_TYPES are
    resolved this way; unknown node types report ``found=False`` so callers
    keep their legacy read. ``found=False`` also covers a splittable type
    whose contract output is missing or non-dict (callers fail closed).
    """
    resolved = node_type or DEFAULT_NODE_TYPE
    if resolved not in SPLITTABLE_NODE_TYPES:
        return False, None
    contract_output, _ = split_node_output(envelope, resolved, None)
    if isinstance(contract_output, dict):
        return True, contract_output
    return False, None


# ---------------------------------------------------------------------------
# READ-side accessors (used by ALL readers; legacy-safe)
# ---------------------------------------------------------------------------


def node_return(outputs_json: Any, _telemetry_json: Any, node_id: str) -> Any:
    """The PURE agent return for *node_id* -- legacy-safe.

    When a telemetry entry exists for the node (a P1+ row) the value in
    ``outputs_json[node_id]`` is already the pure return; when it is absent (a
    legacy row) the value is the mixed legacy envelope. In BOTH cases the
    returned value is ``outputs_json[node_id]`` verbatim -- for legacy rows
    this is byte-identical to today's direct column reads. ``None`` when the
    node has no entry (callers keep their own not-found handling).
    """
    if not isinstance(outputs_json, dict):
        return None
    return outputs_json.get(node_id)


def node_telemetry(telemetry_json: Any, outputs_json: Any, node_id: str) -> Any:
    """The exhaustive telemetry for *node_id* -- legacy-safe.

    When a telemetry entry exists for the node it is returned verbatim. For
    legacy rows (no telemetry entry) the inner ``output`` envelope is extracted
    from the mixed value (mirroring ``finalize._node_output_dict`` exactly), so
    the value matches what today's cost readers extract.
    """
    if isinstance(telemetry_json, dict) and node_id in telemetry_json:
        return telemetry_json[node_id]
    return _legacy_inner_output(outputs_json, node_id)


# ---------------------------------------------------------------------------
# Type-map extension for HITL gates
# ---------------------------------------------------------------------------


def extend_node_type_map_from_edges(
    node_type_map: dict[str, str] | None,
    graph_json: Any,
) -> dict[str, str]:
    """Return *node_type_map* plus ``"gate"`` entries for edge-synthesized gates.

    HITL gate nodes are inserted at graph-compile time
    (``hitl_gate_<source>_<target>``) and are NOT present in
    ``graph_json.nodes``. They ARE encoded on the edges via
    ``hitl_gate_config``, so this walks ``graph_json.edges`` and stamps each
    gate id with ``NODE_TYPE_GATE``. Never mutates the input map.
    """
    result = dict(node_type_map or {})
    if not isinstance(graph_json, dict):
        return result
    edges = graph_json.get("edges")
    if not isinstance(edges, list):
        return result
    for edge in edges:
        if not isinstance(edge, dict) or "hitl_gate_config" not in edge:
            continue
        source = edge.get("source") or edge.get("source_node_id")
        target = edge.get("target") or edge.get("target_node_id")
        if source and target:
            result[f"hitl_gate_{source}_{target}"] = NODE_TYPE_GATE
    return result


# ---------------------------------------------------------------------------
# Per-node-type splitters (WRITE side -- production writers use them from P1)
# ---------------------------------------------------------------------------


def _split_sandbox_agent(envelope: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """sandbox_agent: return = ``artifacts[0].output.output_json``.

    Telemetry = ``artifacts[0].output`` minus ``output_json``, unioned with the
    outer ``output`` envelope keys, plus any unknown top-level fields. A
    missing / non-dict ``output_json`` yields return ``None``.
    """
    outer_output = envelope.get("output")
    telemetry: dict[str, Any] = {}
    if isinstance(outer_output, dict):
        telemetry.update(outer_output)
    a0 = _first_artifact(envelope)
    return_value: Any = None
    if a0 is not None:
        inner = a0.get("output")
        if isinstance(inner, dict):
            inner_telemetry = dict(inner)
            return_value = inner_telemetry.pop("output_json", None)
            if not isinstance(return_value, dict):
                return_value = None
            telemetry.update(inner_telemetry)
    _surface_artifact_fields(a0, telemetry)
    excluded = {"artifacts"}
    if isinstance(outer_output, dict) or a0 is None:
        # The outer ``output`` was consumed into telemetry, or there is no
        # artifact to read a return from (the ``{status, summary}`` skip shape)
        # -- either way its content is already represented.
        excluded.add("output")
    _add_lossless(envelope, telemetry, excluded)
    return return_value, telemetry


def _split_agent(envelope: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Regular agent: return = the outer ``output`` key.

    A stub / agent-without-backend envelope has NO ``output`` key, so the
    return is ``None``. Telemetry = ``{status, summary?}`` from the envelope.
    """
    return_value = envelope.get("output")
    a0 = _first_artifact(envelope)
    telemetry: dict[str, Any] = {}
    if a0 is not None:
        for key in ("status", "summary"):
            if key in a0:
                telemetry[key] = a0[key]
    for key in ("status", "summary"):
        if key in envelope and key not in telemetry:
            telemetry[key] = envelope[key]
    _add_lossless(envelope, telemetry, {"artifacts", "output"})
    return return_value, telemetry


def _split_connector(envelope: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Connector completed: return = ``artifacts[0].output`` (the result).

    connector failure (``artifacts[0].status == "failed"``): return = ``None``,
    telemetry = ``{status: failed, error}``.
    """
    a0 = _first_artifact(envelope)
    telemetry: dict[str, Any] = {}
    if a0 is not None and a0.get("status") == "failed":
        telemetry["status"] = "failed"
        if "error" in a0:
            telemetry["error"] = a0["error"]
        _surface_artifact_fields(a0, telemetry)
        _add_lossless(envelope, telemetry, {"artifacts", "output"})
        return None, telemetry
    return_value = a0.get("output") if a0 is not None else envelope.get("output")
    _surface_artifact_fields(a0, telemetry)
    excluded = {"artifacts"}
    if a0 is not None and envelope.get("output") is return_value:
        excluded.add("output")
    _add_lossless(envelope, telemetry, excluded)
    return return_value, telemetry


def _split_gate(envelope: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """HITL gate: return = ``human_data`` if present else ``None``.

    Telemetry = ``{status, result?, human_data?}`` plus any unknown fields. A
    ``deliver_manual`` resume returns the full human decision dict as the
    return.
    """
    a0 = _first_artifact(envelope)
    return_value = a0.get("human_data") if a0 is not None else None
    telemetry: dict[str, Any] = {}
    if a0 is not None:
        for key in ("status", "result", "human_data"):
            if key in a0:
                telemetry[key] = a0[key]
    _surface_artifact_fields(a0, telemetry)
    _add_lossless(envelope, telemetry, {"artifacts"})
    return return_value, telemetry


def _split_manual(envelope: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """manual-node resume: return = ``manual_output``.

    Telemetry = ``{status, recovered?}`` plus any unknown fields.
    """
    a0 = _first_artifact(envelope)
    return_value = envelope.get("manual_output")
    if return_value is None and a0 is not None:
        return_value = a0.get("human_output") or a0.get("output")
    telemetry: dict[str, Any] = {}
    if a0 is not None:
        for key in ("status", "human_output"):
            if key in a0:
                telemetry[key] = a0[key]
    if "recovered" in envelope:
        telemetry["recovered"] = envelope["recovered"]
    _add_lossless(envelope, telemetry, {"artifacts", "manual_output"})
    return return_value, telemetry


def _split_recovery(envelope: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Recovery marker: ``{input, output, recovered|skipped}``.

    ``recovered`` -> return = ``input_data``, telemetry =
    ``{recovered: true, recovery_input: input_data}``. ``skipped`` -> return
    key OMITTED: the helper returns ``(None, {skipped: true})`` and the P1
    writer must NOT create the node's ``outputs_json`` entry (the telemetry
    entry is the sole record).
    """
    if envelope.get("recovered") is True:
        input_data = envelope.get("input")
        return input_data, {"recovered": True, "recovery_input": input_data}
    return None, {"skipped": True}


def _looks_like_gate(envelope: dict[str, Any]) -> bool:
    """Shape-based gate detection for envelopes without a resolved node type.

    Gate envelopes carry ``human_data`` / ``autonomy`` / ``result`` or an
    ``interrupted`` / ``condition_skipped`` / ``auto_approved`` artifact
    status -- none of which any non-gate writer produces.
    """
    a0 = _first_artifact(envelope)
    if a0 is None:
        return False
    if "human_data" in a0 or "autonomy" in a0 or "result" in a0:
        return True
    return a0.get("status") in {"interrupted", "condition_skipped", "auto_approved"}


def _is_recovery_marker(envelope: dict[str, Any]) -> bool:
    """True when *envelope* is a recovery marker.

    A recovery marker carries a ``recovered`` / ``skipped`` key but was NOT
    written with a structured ``artifacts`` list (in which case it is treated
    as a normal envelope instead).
    """
    has_marker = "recovered" in envelope or "skipped" in envelope
    return has_marker and not isinstance(envelope.get("artifacts"), list)


def _split_by_known_type(envelope: dict[str, Any], resolved_type: str) -> tuple[Any, dict[str, Any]] | None:
    """Dispatch a splittable node type to its dedicated per-type splitter.

    Returns the ``(return_value, telemetry)`` tuple, or ``None`` when
    *resolved_type* is not a splittable type so the caller can fall through to
    shape-based detection / unknown handling.
    """
    if resolved_type not in SPLITTABLE_NODE_TYPES:
        return None
    if resolved_type == "sandbox_agent":
        return _split_sandbox_agent(envelope)
    if resolved_type == "agent":
        return _split_agent(envelope)
    if resolved_type == "connector":
        return _split_connector(envelope)
    if resolved_type == "manual":
        return _split_manual(envelope)
    return _split_gate(envelope)


def _split_unknown(
    envelope: dict[str, Any],
    resolved_type: str,
    run_id: str | None,
    node_id: str | None,
) -> tuple[Any, dict[str, Any]]:
    """Best-effort split for a recognized-but-unhandled envelope shape.

    Warns with ``run_id`` / ``node_id`` / ``reason`` and returns the
    best-effort ``(envelope.get("output"), {unknown fields})``. All envelope
    fields land in telemetry (lossless).
    """
    _log.warning(
        "node_output_split.unknown",
        extra={
            "run_id": run_id,
            "node_id": node_id,
            "reason": f"unknown_node_type:{resolved_type!r}",
        },
    )
    return_value = envelope.get("output")
    telemetry = {key: value for key, value in envelope.items() if key != "output"}
    return return_value, telemetry


def split_node_output(
    outer_dict: Any,
    node_type: str | None,
    stored_telemetry: Any,
    *,
    run_id: str | None = None,
    node_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Split a legacy per-node envelope into ``(return_value, telemetry)``.

    This is the WRITE-side splitter: P1 writers call it before persisting a
    node's output so the pure return and the exhaustive telemetry land in their
    separate columns. It is deliberately NOT called by any production writer in
    P0 -- it is fully unit-tested now so the P1 flip is a straight swap.

    Dispatch rules (first match wins):

    1. ``stored_telemetry is not None`` -- the node is ALREADY pure: idempotent
       no-op returning ``(outer_dict, stored_telemetry)`` unchanged.
    2. Not a dict -- malformed: warn and return ``(None, {})``.
    3. Recovery marker (no ``artifacts`` and a ``recovered`` / ``skipped`` key).
    4. Known ``node_type`` (``sandbox_agent``, ``agent``, ``connector``,
       ``manual``, ``gate``).
    5. Gate envelope detected by shape (interrupted status / ``human_data`` /
       ``autonomy`` / ``result`` / ``condition_skipped`` / ``auto_approved``).
    6. Unknown: warn with ``run_id`` / ``node_id`` / ``reason`` and return the
       best-effort ``(envelope.get("output"), {unknown fields})``.

    NEVER raises. Unknown envelope fields always land in telemetry (lossless).
    """
    if stored_telemetry is not None:
        return outer_dict, stored_telemetry
    if not isinstance(outer_dict, dict):
        _log.warning(
            "node_output_split.malformed",
            extra={"run_id": run_id, "node_id": node_id, "reason": "not_a_dict"},
        )
        return None, {}
    if _is_recovery_marker(outer_dict):
        return _split_recovery(outer_dict)
    resolved_type = node_type or ""
    known = _split_by_known_type(outer_dict, resolved_type)
    if known is not None:
        return known
    if _looks_like_gate(outer_dict):
        return _split_gate(outer_dict)
    return _split_unknown(outer_dict, resolved_type, run_id, node_id)
