"""Per-node run output split helpers (FAR-124).

``run.outputs_json[node_id]`` stores the PURE node return and
``node_telemetry_json[node_id]`` stores exhaustive runtime telemetry. The
write-side splitter remains available for envelope inputs produced before the
split, while read-side accessors expose only the stored post-split values.

- ``split_node_output`` -- the WRITE-side splitter used by the production
  writers when a pre-split envelope must be converted. Never raises: unknown or
  malformed envelopes produce a warning and a best-effort split.
- ``node_return`` / ``node_telemetry`` -- the READ-side accessors for pure
  returns and stored telemetry.
- ``extend_node_type_map_from_edges`` -- stamps ``"gate"`` entries for the HITL
  gate nodes synthesized at graph-compile time (absent from
  ``graph_json.nodes``) so gate envelopes can be resolved from a type map.

FAR-132 backfill marker
-----------------------

The scheduled straggler sweep still uses ``stored_row_is_legacy`` to identify
rows that need backfill. Read accessors do not interpret those rows.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_NODE_TYPE",
    "DISPATCH_PROVENANCE_FIELDS",
    "NODE_TYPE_GATE",
    "SPLITTABLE_NODE_TYPES",
    "extend_node_type_map_from_edges",
    "has_telemetry_entry",
    "is_split_envelope",
    "node_return",
    "node_stderr_artifact",
    "node_stdout_artifact",
    "node_telemetry",
    "resolve_node_contract_output",
    "split_node_output",
    "stored_row_is_legacy",
]

#: Node-type value used for HITL gate envelopes in extended type maps.
NODE_TYPE_GATE = "gate"

#: Node type assumed when a graph node declares none (matches the compile-time
#: default in ``build_graph_from_json`` / ``_split_agent``).
DEFAULT_NODE_TYPE = "agent"

#: Node types with a dedicated WRITE-side envelope splitter in
#: ``split_node_output``.
SPLITTABLE_NODE_TYPES = frozenset(
    {
        "sandbox_agent",
        "agent",
        "connector",
        "dispatch",
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
        "stderr_artifact",
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

#: FAR-1141: provenance fields stamped on every dispatch result by
#: ``node_runner._stamp_dispatch_provenance``. Declared HERE (a leaf module
#: node_runner already imports) so the stamper and the splitter's failed-artifact
#: path cannot drift: the failure branch drops the envelope's ``output``, so it
#: lifts these keys out of it explicitly — without a shared list the stamped
#: provenance would silently vanish from failure inspection (the FAR-1141
#: ``substrate_status`` hand-duplication was exactly that drift in slow motion).
#:
#: FAR-1693 adds ``provenance`` — the unified taxonomy class (``reported``) —
#: alongside the ``witnessed_via`` channel stamp. Both must survive the failed
#: branch; the conformance test ties this literal to
#: ``modulo.core.provenance.PROVENANCE_KEY`` so the two cannot drift.
DISPATCH_PROVENANCE_FIELDS = (
    "witnessed_via",
    "execution_identity",
    "declared_external_cost",
    "substrate_status",
    "provenance",
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


def is_split_envelope(value: Any) -> bool:
    """True when *value* is an already-split node envelope (FAR-130).

    The write-side contract (``node_runner._build_sandbox_node_envelope``)
    emits all THREE channels together, so all three are required:

    - ``output`` -- the PURE node return,
    - ``telemetry`` -- the exhaustive runtime telemetry (a dict), and
    - ``artifacts`` -- the LangGraph state accumulator entry the reducer
      concatenates into ``state["artifacts"]``.

    Requiring ``artifacts`` is the COLLISION GUARD. A legacy envelope carries
    ``artifacts`` but no ``telemetry``; a bare ``{output, telemetry}`` pair is
    NOT the production shape and is deliberately not detected as split -- it
    falls through to the per-type splitter rather than being silently treated
    as an already-split value. This predicate is the ONE canonical detection:
    ``split_node_output``'s idempotent branch and
    ``agent_signal._resolve_child_node_output`` both call it, so the two can
    never drift apart again.

    RESIDUAL AMBIGUITY (FAR-130 review note): an AGENT-AUTHORED pure return
    coerced to exactly ``{output, telemetry, artifacts}`` would be mis-detected
    as a split envelope. This is confined to the LEGACY RE-SPLIT leg --
    ``split_node_output`` called with ``stored_telemetry=None`` (finalize with
    no stored telemetry, or the trigger signal resolver) -- because once a
    telemetry entry exists the idempotent ``stored_telemetry is not None``
    branch fires first and never shape-sniffs.
    """
    return isinstance(value, dict) and "output" in value and isinstance(value.get("telemetry"), dict) and "artifacts" in value


def resolve_node_contract_output(
    envelope: dict[str, Any],
    node_type: str | None,
) -> tuple[bool, Any]:
    """Resolve a node envelope's contract output for eval validation (FAR-311).

    Returns ``(found, contract_output)``. Only known splittable node types are
    resolved; unknown types and missing/non-dict outputs fail closed.
    """
    resolved = node_type or DEFAULT_NODE_TYPE
    if resolved not in SPLITTABLE_NODE_TYPES:
        return False, None
    contract_output, _ = split_node_output(envelope, resolved, None)
    if isinstance(contract_output, dict):
        return True, contract_output
    return False, None


# ---------------------------------------------------------------------------
# READ-side accessors (used by ALL readers)
# ---------------------------------------------------------------------------


def node_return(outputs_json: Any, _telemetry_json: Any, node_id: str) -> Any:
    """Return the pure output mapping for *node_id*, or ``None`` when absent."""
    if not isinstance(outputs_json, dict):
        return None
    return outputs_json.get(node_id)


def has_telemetry_entry(telemetry_json: Any, node_id: str) -> bool:
    """True when *telemetry_json* carries an entry for *node_id*."""
    return isinstance(telemetry_json, dict) and node_id in telemetry_json


def stored_row_is_legacy(*, outputs_present: bool, telemetry_present: bool) -> bool:
    """True for a stored row with outputs but no telemetry side.

    The scheduled straggler sweep still uses this row-level predicate even
    though read accessors no longer interpret legacy envelopes.
    """
    return outputs_present and not telemetry_present


def node_telemetry(telemetry_json: Any, _outputs_json: Any, node_id: str) -> Any:
    """Return the stored telemetry mapping for *node_id*, or ``None``."""
    if not isinstance(telemetry_json, dict):
        return None
    return telemetry_json.get(node_id)


def node_stdout_artifact(telemetry_json: Any, outputs_json: Any, node_id: str) -> dict[str, Any] | None:
    """Return the stored full-stdout transcript pointer, or ``None``.

    Transcript pointers are stored on the node's telemetry side. The
    ``outputs_json`` argument remains for call-site compatibility but is not
    interpreted.
    """
    telemetry = node_telemetry(telemetry_json, outputs_json, node_id)
    if isinstance(telemetry, dict):
        pointer = telemetry.get("stdout_artifact")
        if isinstance(pointer, dict):
            return pointer
    return None


def node_stderr_artifact(telemetry_json: Any, outputs_json: Any, node_id: str) -> dict[str, Any] | None:
    """Return the stored full-stderr transcript pointer, or ``None``.

    Transcript pointers are stored on the node's telemetry side. The
    ``outputs_json`` argument remains for call-site compatibility but is not
    interpreted.
    """
    telemetry = node_telemetry(telemetry_json, outputs_json, node_id)
    if isinstance(telemetry, dict):
        pointer = telemetry.get("stderr_artifact")
        if isinstance(pointer, dict):
            return pointer
    return None


# ---------------------------------------------------------------------------
# Type-map extension for HITL gates
# ---------------------------------------------------------------------------


def extend_node_type_map_from_edges(
    node_type_map: dict[str, str] | None,
    graph_json: Any,
) -> dict[str, str]:
    """Return *node_type_map* plus ``"gate"`` entries for edge-synthesized gates.

    HITL gate nodes are inserted at graph-compile time
    (``hitl_review_<source>_<target>``) and are NOT present in
    ``graph_json.nodes``. They ARE encoded on the edges via
    ``hitl_review_config``, so this walks ``graph_json.edges`` and stamps each
    gate id with ``NODE_TYPE_GATE``. Never mutates the input map.
    """
    result = dict(node_type_map or {})
    if not isinstance(graph_json, dict):
        return result
    edges = graph_json.get("edges")
    if not isinstance(edges, list):
        return result
    for edge in edges:
        if not isinstance(edge, dict) or "hitl_review_config" not in edge:
            continue
        source = edge.get("source") or edge.get("source_node_id")
        target = edge.get("target") or edge.get("target_node_id")
        if source and target:
            result[f"hitl_review_{source}_{target}"] = NODE_TYPE_GATE
    return result


# ---------------------------------------------------------------------------
# Per-node-type splitters (WRITE side -- used by the production writers)
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
    telemetry = ``{status, error}``.

    FAR-1141: a FAILED dispatch envelope also carries the stamped ``output``
    (``witnessed_via`` / ``execution_identity`` / ``declared_external_cost`` /
    ``substrate_status``), and this branch drops ``output`` — so the provenance
    fields are lifted out of it first. Otherwise a dispatch node that failed
    after firing an external job would reach failure inspection reading like a
    node Modulo executed itself.
    """
    a0 = _first_artifact(envelope)
    if a0 is not None and a0.get("status") == "failed":
        return _split_failed_connector(envelope, a0)
    return _split_completed_connector(envelope, a0)


def _split_failed_connector(
    envelope: dict[str, Any],
    a0: dict[str, Any],
) -> tuple[None, dict[str, Any]]:
    """Split a FAILED connector envelope: return None, keep status/error."""
    telemetry: dict[str, Any] = {"status": "failed"}
    if "error" in a0:
        telemetry["error"] = a0["error"]
    _surface_artifact_fields(a0, telemetry)
    _lift_dispatch_provenance(envelope.get("output"), telemetry)
    _add_lossless(envelope, telemetry, {"artifacts", "output"})
    return None, telemetry


def _lift_dispatch_provenance(failed_output: Any, telemetry: dict[str, Any]) -> None:
    """Lift dispatch provenance fields out of a failed envelope's ``output``."""
    if not isinstance(failed_output, dict):
        return
    for key in DISPATCH_PROVENANCE_FIELDS:
        if key in failed_output and key not in telemetry:
            telemetry[key] = failed_output[key]


def _split_completed_connector(
    envelope: dict[str, Any],
    a0: dict[str, Any] | None,
) -> tuple[Any, dict[str, Any]]:
    """Split a completed connector envelope: return ``artifacts[0].output``."""
    telemetry: dict[str, Any] = {}
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
    key OMITTED: the helper returns ``(None, {skipped: true})`` and the writer
    must NOT create the node's ``outputs_json`` entry (the telemetry entry is
    the sole record).
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
    if resolved_type in ("connector", "dispatch"):
        # FAR-1141: a dispatch node returns the SAME artifacts/output envelope
        # a connector node does, so it shares the connector splitter.
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
    """Split a pre-split per-node envelope into ``(return_value, telemetry)``.

    This is the WRITE-side splitter: production writers call it when a stored
    or incoming envelope must be converted, so the pure return and the
    exhaustive telemetry land in their separate columns.

    Dispatch rules (first match wins):

    1. ``stored_telemetry is not None`` -- the node is ALREADY pure: idempotent
       no-op returning ``(outer_dict, stored_telemetry)`` unchanged.
    2. Already-split envelope (FAR-130) -- detected by the canonical
       :func:`is_split_envelope` (``output`` + dict-valued ``telemetry`` +
       ``artifacts``): idempotent no-op returning
       ``(outer_dict["output"], outer_dict["telemetry"])``. This is the
       READ-side twin of the write-side split: ``node_runner``'s sandbox
       envelope is emitted already split (with an ``artifacts`` accumulator
       alongside), so re-splitting it must NEVER nest the telemetry under the
       return. Requiring ``artifacts`` is the documented collision guard -- see
       :func:`is_split_envelope` for the residual agent-authored-return case.
    3. Not a dict -- malformed: warn and return ``(None, {})``.
    4. Recovery marker (no ``artifacts`` and a ``recovered`` / ``skipped`` key).
    5. Known ``node_type`` (``sandbox_agent``, ``agent``, ``connector``,
       ``dispatch``, ``manual``, ``gate``).
    6. Gate envelope detected by shape (interrupted status / ``human_data`` /
       ``autonomy`` / ``result`` / ``condition_skipped`` / ``auto_approved``).
    7. Unknown: warn with ``run_id`` / ``node_id`` / ``reason`` and return the
       best-effort ``(envelope.get("output"), {unknown fields})``.

    NEVER raises. Unknown envelope fields always land in telemetry (lossless).
    """
    if stored_telemetry is not None:
        return outer_dict, stored_telemetry
    if is_split_envelope(outer_dict):
        # Already-split (new FAR-130 sandbox envelope): ``output`` is the pure
        # return and ``telemetry`` the exhaustive side. The pure return is
        # returned verbatim — never nest telemetry under it. Any OTHER
        # top-level key (e.g. the ``otel_span_id`` / ``otel_trace_id`` stamps
        # ``_record_chain_end_output`` adds before persistence) is folded into
        # telemetry losslessly, exactly like the legacy path, so those stamps
        # still surface in the stored ``node_telemetry_json`` entry. The three
        # split channels themselves are never re-folded.
        merged_telemetry = dict(outer_dict["telemetry"])
        _add_lossless(outer_dict, merged_telemetry, {"output", "telemetry", "artifacts"})
        return outer_dict.get("output"), merged_telemetry
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
