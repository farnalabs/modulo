"""Graph validator — pre-run and on-save validation.

Checks:
1. Topology: no cycles, valid edge references, reachability, max nesting depth 3
2. Schema compatibility: output schema of each edge source matches input schema of target
3. Connector capability: bound connector instances are active and have required operations
4. Model backend health: pinned model backends exist and are active
5. Environment capability: bound EnvironmentProfile declares all agent required capabilities
6. Pre-run input payload compatibility with entry node schema
7. Node category: ``node_category_id`` references exist and are compatible with node type
"""

import logging
import re
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, NamedTuple, TypeGuard

import jmespath
import jmespath.exceptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator._types import (
    ValidationResult,
    try_parse_uuid,
    try_parse_uuids,
)
from modulo.core.graph_validator.category_validator import validate_node_categories
from modulo.db.models.agent import Agent
from modulo.db.models.composite_template import CompositeTemplate
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.parameter_set import ParameterSet
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.schema import SchemaVersion

_log = logging.getLogger(__name__)
_SKIPPED_EDGE_TYPES = frozenset({"reject", "kickback", "loop"})
_JSON_TYPE_MAP: MappingProxyType[str, type | tuple[type, ...]] = MappingProxyType(
    {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
)

# Phase 1 cutover: pipelines with snapshots created before this date
# use degraded-mode validation (warnings instead of hard errors).
_PHASE_1_CUTOVER = datetime(2026, 7, 22, tzinfo=UTC)

_DEFERRED_SCHEMA_KEYWORDS = frozenset({"$ref", "oneOf", "anyOf", "allOf", "not", "if", "then", "else"})

# Maximum recursion depth for the field-level schema compatibility check.
_SCHEMA_MAX_DEPTH = 20

# Known-good E2B sandbox templates. "opencode" is the product default (has the
# opencode CLI pre-installed); "modulo-opencode" is the managed cache-warmed
# image built for Modulo's own pipelines.
_KNOWN_SANDBOX_TEMPLATES = frozenset({"opencode", "modulo-opencode"})

# Pipeline retry_policy events + budget bound (kept in sync with the API
# schema in api/routes/pipelines.py and the executor's _retry_after_policy).
# "eval_failed" re-dispatches a guardrail-blocked run (final_status
# "eval_failed" / error_code "eval.blocked"); the FAR-228 idempotency gate
# (guard A) makes that re-dispatch safe for delivery nodes.
_RETRY_POLICY_EVENTS = frozenset({"stall", "timeout", "failure", "eval_failed"})
_RETRY_POLICY_MAX_RETRIES = 5

# Run-level ``backoff_schedule`` bounds (FAR-525). Hosted in retry_compensation
# (DB-free, the schedule constants' home) so the write-site validator and the
# runtime resolver cannot drift. NOTE: retry_compensation is LAZY-imported
# inside check_retry_policy_schedule — an eager module-level import would
# re-enter the executor <-> graph_validator import cycle (see _check_retry).

# REST connector fan-out effective defaults. The connector defaults
# ``max_cardinality`` to ``_DEFAULT_MAX_FANOUT_CARDINALITY`` and
# ``per_item_timeout`` to its ``_DEFAULT_TIMEOUT`` when the ``fan_out`` config
# omits them. Note: the connector does NOT read a top-level ``timeout`` config
# key — the timeout is a constructor parameter defaulted to ``_DEFAULT_TIMEOUT``
# that the production composition root never overrides, so the connector ALWAYS
# executes 30.0s per item in production. The send-budget reconcile applies the
# SAME defaults, imported in _check_node_send_budget directly from the connector
# module as the single source of truth, so it cannot diverge from what the
# connector actually executes.


def _is_pre_existing(snapshot: PipelineSnapshot) -> bool:
    """Check if a snapshot was created before the Phase 1 cutover date."""
    created_at = getattr(snapshot, "created_at", None)
    if created_at is None:
        return False
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at < _PHASE_1_CUTOVER
    return False


def _string_or_default(value: object, default: str = "?") -> str:
    return default if value is None else str(value)


def _is_valid_number(value: object) -> TypeGuard[int | float]:
    """True for int/float values that are not bool (bool is a subclass of int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_non_negative_int(value: object) -> bool:
    """True for a genuine (non-bool) int that is >= 0."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_valid_retry_budget(value: object) -> bool:
    """True for a genuine (non-bool) int within the retry_policy budget bound."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _RETRY_POLICY_MAX_RETRIES


def _as_positive_number(value: Any) -> float | None:
    """Coerce *value* to a positive float, or None when it is not one.

    Used by the node send-budget reconcile (FAR-410/FAR-411) on the
    ``config_json.fan_out`` advisory keys (``max_cardinality`` /
    ``per_item_timeout``) — a malformed value is treated as absent (skipped)
    rather than a hard error.
    """
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if number > 0 else None


def _check_list_type_mismatch(path: str, out_type: object, in_type: list[object]) -> list[str]:
    """Input is a nullable type list: output must be a subset of the input types."""
    if isinstance(out_type, list):
        return [f"{path}: output type '{ot}' not in input types {in_type}" for ot in out_type if ot not in in_type]
    if out_type not in in_type:
        return [f"{path}: output type '{out_type}' not in input types {in_type}"]
    return []


def _check_nullable_type_mismatch(path: str, out_type: list[object], in_type: object) -> list[str]:
    """Output is a nullable type list: each entry must be the input type (or null)."""
    errors = [f"{path}: type mismatch '{ot}' -> '{in_type}'" for ot in out_type if ot not in ("null", in_type)]
    if "null" in out_type and not (isinstance(in_type, list) and "null" in in_type):
        errors.append(f"{path}: output allows null but input does not")
    return errors


def _check_additional_properties(
    in_field: dict[str, Any],
    out_field: dict[str, Any],
    path: str,
    errors: list[str],
) -> None:
    """When the input forbids extra properties, flag any output-only properties."""
    in_addl = in_field.get("additionalProperties", True)
    if in_addl is False:
        out_props_raw = out_field.get("properties", {})
        in_props_raw = in_field.get("properties", {})
        out_props = set(out_props_raw.keys()) if isinstance(out_props_raw, dict) else set()
        in_props = set(in_props_raw.keys()) if isinstance(in_props_raw, dict) else set()
        extra = out_props - in_props
        if extra:
            errors.append(f"{path}: extra properties {extra} not allowed (additionalProperties: false)")


def _check_scalar_type_mismatch(path: str, out_type: Any, in_type: Any) -> list[str]:
    """Flag a primitive type mismatch (integer is promotable to number)."""
    if out_type and in_type and out_type != in_type:
        promotable = {"integer": ["number"]}
        if in_type not in promotable.get(out_type, []):
            return [f"{path}: type mismatch '{out_type}' -> '{in_type}'"]
    return []


def _check_enum_subset(
    path: str,
    out_enum: Any,
    in_enum: Any,
) -> list[str]:
    """Output enum values must be a subset of the input enum values."""
    if out_enum is not None and in_enum is not None and not set(out_enum).issubset(set(in_enum)):
        return [f"{path}: output enum values {out_enum} not subset of input enum {in_enum}"]
    return []


def _resolve_parameter_schema(
    node_id: str,
    raw_schema_id: object,
    schemas: dict[uuid.UUID, ParameterSchema],
    result: ValidationResult,
) -> uuid.UUID | None:
    """Parse + resolve a node's parameter_schema_id; emits an error and returns None when invalid."""
    schema_id = try_parse_uuid(raw_schema_id)
    if schema_id is None:
        result.error(
            "PARAMETER_SCHEMA_INVALID_ID",
            f"Node '{node_id}': parameter_schema_id is not a valid UUID",
            node_id=node_id,
        )
        return None
    if schemas.get(schema_id) is None:
        result.error(
            "PARAMETER_SCHEMA_NOT_FOUND",
            f"Node '{node_id}': ParameterSchema '{schema_id}' not found",
            node_id=node_id,
        )
        return None
    return schema_id


def _resolve_parameter_set(
    node_id: str,
    raw_set_id: object,
    sets: dict[uuid.UUID, ParameterSet],
    result: ValidationResult,
) -> uuid.UUID | None:
    """Parse + resolve a node's parameter_set_id; emits an error and returns None when invalid."""
    set_id = try_parse_uuid(raw_set_id)
    if set_id is None:
        result.error(
            "PARAMETER_SET_INVALID_ID",
            f"Node '{node_id}': parameter_set_id is not a valid UUID",
            node_id=node_id,
        )
        return None
    if sets.get(set_id) is None:
        result.error(
            "PARAMETER_SET_NOT_FOUND",
            f"Node '{node_id}': ParameterSet '{set_id}' not found or belongs to a different org",
            node_id=node_id,
        )
        return None
    return set_id


def _check_parameter_set_schema_match(
    node_id: str,
    schema_id: uuid.UUID,
    set_id: uuid.UUID,
    ps: ParameterSet,
    result: ValidationResult,
) -> None:
    """A ParameterSet pinned alongside a schema must belong to that exact schema."""
    if schema_id != ps.parameter_schema_id:
        result.error(
            "PARAMETER_SET_SCHEMA_MISMATCH",
            f"Node '{node_id}': ParameterSet '{set_id}' belongs to schema "
            f"'{ps.parameter_schema_id}', not '{schema_id}'",
            node_id=node_id,
        )


def _schema_parameters_missing_from_set(schema: ParameterSchema, ps: ParameterSet) -> set[str]:
    """Parameters the schema defines that the set's values do not cover."""
    schema_param_names: set[str] = set()
    for param in schema.parameters or []:
        if isinstance(param, dict) and "name" in param:
            schema_param_names.add(param["name"])
    set_param_names: set[str] = set(ps.values.keys()) if isinstance(ps.values, dict) else set()
    return schema_param_names - set_param_names


def _check_parameter_set_drift(
    node_id: str,
    schema: ParameterSchema,
    set_id: uuid.UUID,
    ps: ParameterSet,
    result: ValidationResult,
) -> None:
    """Warn when a ParameterSet predates its schema (version drift) or misses schema params."""
    if schema.version > ps.schema_version:
        result.warning(
            "PARAMETER_SCHEMA_DRIFT",
            f"Node '{node_id}': ParameterSchema '{schema.id}' has been updated to "
            f"version {schema.version} but ParameterSet '{set_id}' was created against "
            f"version {ps.schema_version}. Consider updating the set.",
            node_id=node_id,
        )
    missing_from_set = _schema_parameters_missing_from_set(schema, ps)
    if missing_from_set:
        result.warning(
            "PARAMETER_SCHEMA_DRIFT_COMPOSITE",
            f"Node '{node_id}': ParameterSchema '{schema.id}' defines parameters "
            f"{missing_from_set} that are not present in ParameterSet '{set_id}'. "
            f"Default values will be used.",
            node_id=node_id,
        )


def _check_parameter_node(
    node: dict[str, Any],
    schemas: dict[uuid.UUID, ParameterSchema],
    sets: dict[uuid.UUID, ParameterSet],
    result: ValidationResult,
) -> None:
    """Validate ONE agent node's parameter_schema_id / parameter_set_id references."""
    node_id = _string_or_default(node.get("id"))
    raw_schema_id = node.get("parameter_schema_id")
    schema_id: uuid.UUID | None = None
    if raw_schema_id is not None:
        schema_id = _resolve_parameter_schema(node_id, raw_schema_id, schemas, result)
        if schema_id is None:
            return

    raw_set_id = node.get("parameter_set_id")
    if raw_set_id is None:
        return
    set_id = _resolve_parameter_set(node_id, raw_set_id, sets, result)
    if set_id is None:
        return
    ps = sets[set_id]

    if raw_schema_id is not None and schema_id is not None:
        _check_parameter_set_schema_match(node_id, schema_id, set_id, ps, result)
        schema = schemas[schema_id]
        _check_parameter_set_drift(node_id, schema, set_id, ps, result)


class _CompositeContext(NamedTuple):
    """Shared (template, node_id) context for composite sub-pipeline validation."""

    template: CompositeTemplate
    node_id: str


def _check_composite_node(
    node: dict[str, Any],
    node_ref_map: dict[str, uuid.UUID],
    found: dict[uuid.UUID, CompositeTemplate],
    validator: "GraphValidator",
    result: ValidationResult,
) -> None:
    """Validate ONE composite node: template existence, ports, output validation."""
    node_id = _string_or_default(node.get("id"))
    if node.get("composite_ref") is None:
        return
    ref = node_ref_map.get(node_id)
    if ref is None:
        return
    template = found.get(ref)
    if template is None:
        result.error(
            "COMPOSITE_TEMPLATE_NOT_FOUND",
            f"Node '{node_id}': CompositeTemplate '{ref}' not found",
            node_id=node_id,
        )
        return

    validator._check_composite_subgraph(template, node_id, result)

    parameter_ports: list[dict[str, Any]] = template.parameter_ports_json or []
    parameter_values: dict[str, Any] = node.get("composite_parameter_values") or {}
    for port in parameter_ports:
        if port.get("required") and port.get("name") not in parameter_values:
            result.error(
                "COMPOSITE_MISSING_PARAMETER",
                f"Node '{node_id}': required parameter '{port.get('name')}' has no value",
                node_id=node_id,
            )

    output_validation: dict[str, Any] = node.get("output_validation", {})
    if output_validation:
        validator._check_output_validation(node_id, output_validation, result)


def _check_composite_sub_nodes(
    ctx: _CompositeContext,
    sub_nodes: list[Any],
    result: ValidationResult,
) -> set[str]:
    """Validate a composite template's sub-nodes; returns the sub-node id set."""
    template, node_id = ctx.template, ctx.node_id
    sub_ids: set[str] = set()
    for sub in sub_nodes:
        if not isinstance(sub, dict):
            result.error(
                "COMPOSITE_SUBGRAPH_INVALID_NODE",
                f"Node '{node_id}': CompositeTemplate '{template.id}' contains a non-dict sub-node",
                node_id=node_id,
            )
            continue
        sid = _string_or_default(sub.get("id"))
        if sid in sub_ids:
            result.error(
                "COMPOSITE_SUBGRAPH_DUPLICATE_NODE_ID",
                f"Node '{node_id}': CompositeTemplate '{template.id}' has duplicate sub-node id '{sid}'",
                node_id=node_id,
            )
        sub_ids.add(sid)
        node_type = sub.get("node_type", "agent")
        if node_type not in ("agent", "manual", "composite", "sandbox_agent"):
            result.error(
                "COMPOSITE_SUBGRAPH_INVALID_TYPE",
                f"Node '{node_id}': CompositeTemplate '{template.id}' sub-node '{sid}' has "
                f"invalid node_type '{node_type}'",
                node_id=node_id,
            )
        if node_type == "sandbox_agent":
            _check_composite_sandbox_sub_node(ctx, sid, sub, result)
    return sub_ids


def _check_composite_sandbox_sub_node(
    ctx: _CompositeContext,
    sid: str,
    sub: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Validate a sandbox_agent composite sub-node's command + template config."""
    template, node_id = ctx.template, ctx.node_id
    cmd = sub.get("agent_command", "")
    if not cmd or not str(cmd).strip():
        result.error(
            "COMPOSITE_SUBGRAPH_SANDBOX_MISSING_COMMAND",
            f"Node '{node_id}': CompositeTemplate '{template.id}' sub-node '{sid}' is missing required agent_command",
            node_id=node_id,
        )
    if not sub.get("template_id"):
        result.error(
            "COMPOSITE_SUBGRAPH_SANDBOX_MISSING_TEMPLATE",
            f"Node '{node_id}': CompositeTemplate '{template.id}' sub-node '{sid}' has no template_id",
            node_id=node_id,
        )


def _check_composite_sub_edges(
    ctx: _CompositeContext,
    sub_ids: set[str],
    sub_edges: list[Any],
    result: ValidationResult,
) -> None:
    """Validate a composite template's sub-edge references + gate support."""
    template, node_id = ctx.template, ctx.node_id
    for edge in sub_edges:
        if not isinstance(edge, dict):
            result.error(
                "COMPOSITE_SUBGRAPH_INVALID_EDGE",
                f"Node '{node_id}': CompositeTemplate '{template.id}' contains a non-dict sub-edge",
                node_id=node_id,
            )
            continue
        if edge.get("hitl_gate_config"):
            result.error(
                "COMPOSITE_SUBGRAPH_GATE_UNSUPPORTED",
                f"Node '{node_id}': CompositeTemplate '{template.id}' sub-edge carries a HITL gate "
                f"config which is not supported in composite sub-pipelines (no HITL write path exists)",
                node_id=node_id,
            )
        src = str(edge.get("source"))
        tgt = str(edge.get("target"))
        if src not in sub_ids:
            result.error(
                "COMPOSITE_SUBGRAPH_EDGE_BAD_SOURCE",
                f"Node '{node_id}': CompositeTemplate '{template.id}' sub-edge references unknown source '{src}'",
                node_id=node_id,
            )
        if tgt not in sub_ids:
            result.error(
                "COMPOSITE_SUBGRAPH_EDGE_BAD_TARGET",
                f"Node '{node_id}': CompositeTemplate '{template.id}' sub-edge references unknown target '{tgt}'",
                node_id=node_id,
            )


def _check_agent_capabilities(
    agent: Agent,
    profile: EnvironmentProfile,
    profile_caps: set[str],
    result: ValidationResult,
) -> None:
    """Flag an agent whose required capabilities are not declared by the profile."""
    required: list[str] = agent.required_environment_capabilities or []
    if not required:
        return
    missing = [c for c in required if c not in profile_caps]
    if missing:
        result.error(
            "ENV_MISSING_CAPABILITIES",
            f"Agent '{agent.name}' requires capabilities {missing} not declared by EnvironmentProfile '{profile.name}'",
        )


def _check_sandbox_command(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 1: the mode-scoped command must be non-empty.

    FAR-296: routed through the SHARED ``_validate_sandbox_mode_config`` helper
    (the same one the node runner, Pydantic model, MCP tool, and config linter
    use) so save-time and run-time validation agree. llm mode requires
    agent_command / agent_commands + agent_prompt; script mode requires
    script_command; both commands present is an error. The ValueError message
    (which already carries the node id) is surfaced as the issue detail.
    """
    from modulo.core.pipeline_engine.sandbox_mode import _validate_sandbox_mode_config

    try:
        _validate_sandbox_mode_config(node)
    except ValueError as exc:
        result.error("SANDBOX_MISSING_COMMAND", str(exc), node_id=nid)


def _check_sandbox_jinja(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check: the agent_command must be Jinja-renderable (FAR-226).

    Renders through the same ``SandboxedEnvironment`` the node runner uses, so
    a broken template (e.g. an invalid backslash) is caught at SAVE time as a
    clear config error instead of surfacing as an opaque instant-fail for every
    run of the pipeline. Only llm mode is checked (script mode is verbatim).
    """
    from modulo.core.pipeline_engine.sandbox_mode import validate_sandbox_agent_command_jinja

    err = validate_sandbox_agent_command_jinja(node)
    if err:
        result.error("SANDBOX_BAD_JINJA_TEMPLATE", err, node_id=nid)


def _check_sandbox_template(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 2: template_id must be set to a known-good sandbox template."""
    template_id = node.get("template_id")
    if not template_id:
        result.warning(
            "SANDBOX_MISSING_TEMPLATE",
            f"Sandbox agent node '{nid}' has no template_id (expected 'opencode' or 'modulo-opencode')",
            node_id=nid,
        )
    elif template_id not in _KNOWN_SANDBOX_TEMPLATES:
        result.warning(
            "SANDBOX_UNKNOWN_TEMPLATE",
            f"Sandbox agent node '{nid}' template_id '{template_id}' is not a known-good sandbox "
            "template (expected 'opencode' or 'modulo-opencode')",
            node_id=nid,
        )


def _check_sandbox_timeout(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 3: timeout_seconds bounds (60-604800)."""
    timeout = node.get("timeout_seconds")
    if timeout is None:
        return
    try:
        t = int(timeout) if not isinstance(timeout, int) else timeout
        if t < 60 or t > 604800:
            result.warning(
                "SANDBOX_TIMEOUT_BOUNDS",
                f"Sandbox agent node '{nid}' timeout_seconds={t} is outside recommended range 60-604800s",
                node_id=nid,
            )
    except (ValueError, TypeError):
        result.warning(
            "SANDBOX_TIMEOUT_INVALID",
            f"Sandbox agent node '{nid}' timeout_seconds is not a valid integer",
            node_id=nid,
        )


def _check_sandbox_timeout_e2b_cap(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check: timeout_seconds must stay under the E2B 1-hour cap (FAR-511).

    The e2b SDK upgrade shipped in the Aug 30 deploy (Python 3.14) began
    enforcing E2B's 1-hour sandbox timeout cap: a sandbox_agent node with
    ``timeout_seconds`` above the cap now fails provisioning with
    ``400: Timeout cannot be greater than 1 hours`` (previously accepted).
    Reject anything above 3300 at save time so there is provisioning headroom;
    do NOT clamp — the author must pick a value explicitly.
    """
    timeout = node.get("timeout_seconds")
    if timeout is None:
        return
    try:
        t = int(timeout) if not isinstance(timeout, int) else timeout
    except (ValueError, TypeError):
        return
    if t > 3300:
        result.error(
            "SANDBOX_TIMEOUT_EXCEEDS_E2B_CAP",
            f"Sandbox agent node '{nid}' timeout_seconds {t} exceeds the E2B sandbox "
            "cap (1 hour); use <= 3300 to leave provisioning headroom",
            node_id=nid,
        )


def _check_sandbox_stall_timeout(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 7: stall_timeout_seconds must be positive and not exceed timeout_seconds."""
    stall_timeout = node.get("stall_timeout_seconds")
    if stall_timeout is None:
        return
    timeout = node.get("timeout_seconds")
    try:
        st = float(stall_timeout) if not isinstance(stall_timeout, (int, float)) else stall_timeout
        if st <= 0:
            result.warning(
                "SANDBOX_STALL_TIMEOUT_INVALID",
                f"Sandbox agent node '{nid}' stall_timeout_seconds={st} is not a positive number",
                node_id=nid,
            )
        elif timeout is not None:
            timeout_seconds = _as_int_or_none(timeout)
            if timeout_seconds is not None and st > timeout_seconds:
                result.warning(
                    "SANDBOX_STALL_TIMEOUT_GT_TIMEOUT",
                    f"Sandbox agent node '{nid}' stall_timeout_seconds={st} exceeds "
                    f"timeout_seconds={timeout_seconds} — a stall timeout larger than the total "
                    "timeout is pointless",
                    node_id=nid,
                )
    except (ValueError, TypeError):
        result.warning(
            "SANDBOX_STALL_TIMEOUT_INVALID",
            f"Sandbox agent node '{nid}' stall_timeout_seconds is not a valid number",
            node_id=nid,
        )


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value) if not isinstance(value, int) else value
    except (ValueError, TypeError):
        return None


def _check_sandbox_stall_detectors(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 8: FAR-306 opt-in stall-detector field validation.

    - ``stdout_percentage_delta`` must be a number in (0, 1] if set.
    - ``watch_globs`` must be an array of strings if set.
    - ``watch_log_path`` must be a string if set.
    - ``enable_heartbeat`` must be a boolean if set.
    """
    _check_sandbox_stdout_delta(node, nid, result)
    _check_sandbox_watch_globs(node, nid, result)
    _check_sandbox_watch_log_path(node, nid, result)
    _check_sandbox_enable_heartbeat(node, nid, result)


def _check_sandbox_stdout_delta(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """stdout_percentage_delta must be a number in (0, 1] if set."""
    delta = node.get("stdout_percentage_delta")
    if delta is None:
        return
    try:
        d = float(delta) if not isinstance(delta, (int, float)) else delta
        if not (0.0 < d <= 1.0):
            result.warning(
                "SANDBOX_STDOUT_DELTA_INVALID",
                f"Sandbox agent node '{nid}' stdout_percentage_delta={d} is outside (0, 1]",
                node_id=nid,
            )
    except (ValueError, TypeError):
        result.warning(
            "SANDBOX_STDOUT_DELTA_INVALID",
            f"Sandbox agent node '{nid}' stdout_percentage_delta is not a valid number",
            node_id=nid,
        )


def _check_sandbox_watch_globs(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """watch_globs must be an array of strings if set."""
    globs = node.get("watch_globs")
    if globs is not None and not isinstance(globs, list):
        result.warning(
            "SANDBOX_WATCH_GLOBS_INVALID",
            f"Sandbox agent node '{nid}' watch_globs must be an array of strings",
            node_id=nid,
        )
    elif isinstance(globs, list):
        for g in globs:
            if not isinstance(g, str):
                result.warning(
                    "SANDBOX_WATCH_GLOBS_INVALID",
                    f"Sandbox agent node '{nid}' watch_globs entries must be strings",
                    node_id=nid,
                )
                break


def _check_sandbox_watch_log_path(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """watch_log_path must be an absolute string path if set."""
    log_path = node.get("watch_log_path")
    if log_path is not None and not isinstance(log_path, str):
        result.warning(
            "SANDBOX_WATCH_LOG_PATH_INVALID",
            f"Sandbox agent node '{nid}' watch_log_path must be a string",
            node_id=nid,
        )
    elif isinstance(log_path, str) and log_path and not log_path.startswith("/"):
        result.warning(
            "SANDBOX_WATCH_LOG_PATH_RELATIVE",
            f"Sandbox agent node '{nid}' watch_log_path '{log_path}' is not an absolute path",
            node_id=nid,
        )


def _check_sandbox_enable_heartbeat(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """enable_heartbeat must be a boolean if set."""
    heartbeat = node.get("enable_heartbeat")
    if heartbeat is not None and not isinstance(heartbeat, bool):
        result.warning(
            "SANDBOX_ENABLE_HEARTBEAT_INVALID",
            f"Sandbox agent node '{nid}' enable_heartbeat must be a boolean",
            node_id=nid,
        )


def _check_sandbox_context_files(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 4: context_files paths must be absolute."""
    context_files = node.get("context_files")
    if isinstance(context_files, dict):
        for source_path in context_files:
            if not source_path.startswith("/"):
                result.warning(
                    "SANDBOX_CONTEXT_PATH_RELATIVE",
                    f"Sandbox agent node '{nid}' context_files source '{source_path}' "
                    f"is not an absolute path (should start with /)",
                    node_id=nid,
                )


def _check_sandbox_env_vars(
    node: dict[str, Any],
    nid: str,
    reserved_prefixes: tuple[str, ...],
    result: ValidationResult,
) -> None:
    """Sandbox check 5: env_vars must not use reserved prefixes."""
    env_vars = node.get("env_vars")
    if isinstance(env_vars, dict):
        for key in env_vars:
            for prefix in reserved_prefixes:
                if key.startswith(prefix):
                    result.warning(
                        "SANDBOX_RESERVED_ENV_VAR",
                        f"Sandbox agent node '{nid}' env var '{key}' uses reserved "
                        f"prefix '{prefix}'. System-reserved env vars are set automatically.",
                        node_id=nid,
                    )


def _check_sandbox_output_schema(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 6: output_schema_json basic structure."""
    schema_json = node.get("output_schema_json")
    if isinstance(schema_json, dict) and "type" not in schema_json and "$ref" not in schema_json:
        result.warning(
            "SANDBOX_SCHEMA_INCOMPLETE",
            f"Sandbox agent node '{nid}' output_schema_json lacks 'type' or '$ref'",
            node_id=nid,
        )


def _check_sandbox_egress(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 9: egress_policy + egress_allowlist (FAR-296, fail-closed).

    egress_policy must be None / "default" / "deny_all" / "selected". The
    host:port ``egress_allowlist`` is cross-checked via the shared
    sandbox_mode validator: ``selected`` REQUIRES a non-empty allowlist,
    any other policy must NOT carry one, and every entry must have a
    non-empty ``host`` + an int ``port`` in [1, 65535].
    """
    from modulo.core.pipeline_engine.sandbox_mode import (
        _SANDBOX_EGRESS_POLICIES,
        _validate_sandbox_egress_allowlist_config,
    )

    egress_policy = node.get("egress_policy")
    if egress_policy is not None and (
        not isinstance(egress_policy, str) or egress_policy not in _SANDBOX_EGRESS_POLICIES
    ):
        result.error(
            "SANDBOX_EGRESS_POLICY_INVALID",
            f"Sandbox agent node '{nid}' egress_policy {egress_policy!r} is invalid — "
            "expected None, 'default', 'deny_all' or 'selected'",
            node_id=nid,
        )
    try:
        _validate_sandbox_egress_allowlist_config(egress_policy, node.get("egress_allowlist"), nid)
    except ValueError as exc:
        result.error(
            "SANDBOX_EGRESS_ALLOWLIST_INVALID",
            f"Sandbox agent node '{nid}' egress allowlist is invalid: {exc}",
            node_id=nid,
        )
    if egress_policy == "selected":
        # FAR-296 Phase 3b-3 limitation: ``selected`` currently DENIES ALL egress
        # (allow_internet_access=False); the allowlist is metadata-only until a
        # template-side enforcement point exists. Warn (not error) so an operator
        # expecting specific hosts to be reachable sees the limitation at
        # save-time instead of discovering a deny_all-equivalent sandbox at run.
        result.warning(
            "SANDBOX_EGRESS_SELECTED_METADATA_ONLY",
            f"Sandbox agent node '{nid}' egress_policy='selected' currently denies "
            "ALL egress — the egress_allowlist is carried as sandbox metadata and "
            "is NOT honored at runtime yet (it awaits a template-side enforcement "
            "point). Functionally equivalent to 'deny_all' until then.",
            node_id=nid,
        )


def _check_sandbox_resource_limits(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 10: resource_limits keys must be a known subset (FAR-296, fail-closed).

    Unknown keys are a hard ERROR — never silently dropped, because a typo
    would otherwise disable a limit the operator intended to enforce.
    """
    resource_limits = node.get("resource_limits")
    if resource_limits is None:
        return
    if not isinstance(resource_limits, dict):
        result.error(
            "SANDBOX_RESOURCE_LIMITS_INVALID",
            f"Sandbox agent node '{nid}' resource_limits must be an object",
            node_id=nid,
        )
        return
    from modulo.core.pipeline_engine.sandbox_mode import _SANDBOX_RESOURCE_LIMIT_KEYS

    unknown = set(resource_limits) - _SANDBOX_RESOURCE_LIMIT_KEYS
    if unknown:
        result.error(
            "SANDBOX_RESOURCE_LIMITS_UNKNOWN_KEY",
            f"Sandbox agent node '{nid}' resource_limits contains unknown keys "
            f"{sorted(unknown)} — allowed keys are {sorted(_SANDBOX_RESOURCE_LIMIT_KEYS)}",
            node_id=nid,
        )


def _check_sandbox_read_only(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 11: read_only must be a genuine boolean (FAR-212 PR B).

    ``read_only`` is a validated + enforced ``PipelineGraphNode`` field — when
    True the workspace is chmodded read-only at runtime and ``sandbox.write_files``
    derives False. A non-boolean value (e.g. a smuggled string ``"yes"`` from a
    raw workflow import) would otherwise reach the fail-closed derivation as an
    unvalidated key and resolve unknown (block) — acceptable — but the operator
    intent was a real read-only seal. Fail closed at save time with a clear
    error instead.
    """
    from modulo.core.pipeline_engine.sandbox_mode import _validate_sandbox_read_only_config

    try:
        _validate_sandbox_read_only_config(node)
    except ValueError as exc:
        result.error("SANDBOX_READ_ONLY_INVALID", str(exc), node_id=nid)


def _check_sandbox_git_credentials(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 12: git_credentials scope must be recognised (FAR-212 PR B).

    ``git_credentials`` is a validated + enforced ``PipelineGraphNode`` field —
    ``scoped`` provisions a helper that limits the token to the allowlisted
    github.com host, ``none`` provisions no git credentials, ``unscoped``/absent
    leave the default full-access credential. An unrecognised value (e.g. a
    smuggled scope from a raw workflow import) would otherwise reach the
    fail-closed derivation as an unvalidated key and resolve unknown (block) —
    acceptable — but the operator intent was a specific scope. Fail closed at
    save time with a clear error instead.
    """
    from modulo.core.pipeline_engine.sandbox_mode import _validate_sandbox_git_credentials_config

    try:
        _validate_sandbox_git_credentials_config(node)
    except ValueError as exc:
        result.error("SANDBOX_GIT_CREDENTIALS_INVALID", str(exc), node_id=nid)


def _check_sandbox_policy_fields_only_on_sandbox_nodes(graph_json: dict[str, Any], result: ValidationResult) -> None:
    """Sandbox check 13: read_only / git_credentials only exist on sandbox_agent nodes.

    The enforcement surface (read-only workspace, git-credential scope) only
    exists for sandbox agents. A raw workflow import could smuggle these fields
    onto an agent/manual/composite node, where they would be a silent no-op —
    a declared control nothing enforces. Fail closed at save time.
    """
    for node in graph_json.get("nodes", []):
        if not isinstance(node, dict) or node.get("node_type") == "sandbox_agent":
            continue
        nid = _string_or_default(node.get("id"))
        if node.get("read_only") is not None and node.get("read_only") is not False:
            result.error(
                "SANDBOX_POLICY_FIELD_ON_NON_SANDBOX",
                f"Node '{nid}' (node_type={node.get('node_type')!r}) sets read_only — "
                "only sandbox_agent nodes can set read_only / git_credentials",
                node_id=nid,
            )
        if node.get("git_credentials") is not None:
            result.error(
                "SANDBOX_POLICY_FIELD_ON_NON_SANDBOX",
                f"Node '{nid}' (node_type={node.get('node_type')!r}) sets git_credentials "
                "— only sandbox_agent nodes can set read_only / git_credentials",
                node_id=nid,
            )


def _check_sandbox_wallclock_budget(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 14: wallclock_budget_seconds must be a positive int (FAR-296 Phase 4a).

    Routed through the SHARED ``_validate_sandbox_wallclock_budget_config`` helper
    (the same one the Pydantic model and node runner use) so save-time and run-time
    validation agree. A budget that cannot be compared to the wall clock is a hard
    ERROR (fail-closed): a malformed budget would silently no-op the spend cap.
    """
    from modulo.core.pipeline_engine.sandbox_mode import _validate_sandbox_wallclock_budget_config

    try:
        _validate_sandbox_wallclock_budget_config(
            node.get("wallclock_budget_seconds"),
            node.get("timeout_seconds"),
            nid,
        )
    except ValueError as exc:
        result.error(
            "SANDBOX_WALLCLOCK_BUDGET_INVALID",
            f"Sandbox agent node '{nid}' wallclock_budget_seconds is invalid: {exc}",
            node_id=nid,
        )


def _check_sandbox_loop_intercept(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """Sandbox check 8: loop_intercept config shape (FAR-211).

    The ``loop_intercept`` config enables the agent-loop interior tool-call
    interception bridge (ADR 003 amendment). A malformed config is a hard
    ERROR — a declared control must never silently no-op because its shape was
    invalid. Absent config (the default) passes.
    """
    raw = node.get("loop_intercept")
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict):
        result.error(
            "SANDBOX_LOOP_INTERCEPT_MALFORMED",
            f"Sandbox agent node '{nid}' loop_intercept must be an object (or omitted)",
            node_id=nid,
        )
        return
    try:
        from modulo.core.guardrails.loop_intercept import validate_loop_intercept_config_errors
    except Exception:
        # Lazy import failure must not crash validation — surface as an error.
        result.error(
            "SANDBOX_LOOP_INTERCEPT_MALFORMED",
            f"Sandbox agent node '{nid}' loop_intercept could not be validated",
            node_id=nid,
        )
        return
    errors = validate_loop_intercept_config_errors(raw)
    for err in errors:
        result.error(
            "SANDBOX_LOOP_INTERCEPT_MALFORMED",
            f"Sandbox agent node '{nid}' loop_intercept: {err}",
            node_id=nid,
        )
    # The empty-patterns warning is independent of other errors: an empty list is
    # VALID shape (so it contributes no error), yet it means the bridge would
    # intercept nothing. Warn whenever the list is empty so a declared-but-inert
    # control never passes silently. (Previously gated on ``errors``, which is
    # empty exactly when the list is empty — the warning was unreachable.)
    if isinstance(raw.get("intercepted_tool_patterns"), list) and not raw["intercepted_tool_patterns"]:
        result.warning(
            "SANDBOX_LOOP_INTERCEPT_EMPTY_PATTERNS",
            f"Sandbox agent node '{nid}' loop_intercept.intercepted_tool_patterns is empty — "
            "no tool calls will be intercepted",
            node_id=nid,
        )


def _check_edge_references(
    edges: list[dict[str, Any]],
    node_ids: set[str],
    result: ValidationResult,
) -> None:
    """Flag edges whose source/target are not graph nodes."""
    for edge in edges:
        src = _string_or_default(edge.get("source"))
        tgt = _string_or_default(edge.get("target"))
        if src not in node_ids:
            result.error("TOPOLOGY_UNKNOWN_SOURCE", f"Edge source '{src}' is not a node")
        if tgt not in node_ids:
            result.error("TOPOLOGY_UNKNOWN_TARGET", f"Edge target '{tgt}' is not a node")


def _check_loop_default_target(
    edge: dict[str, Any],
    source: str,
    target: str,
    node_ids: set[str],
    result: ValidationResult,
) -> None:
    """Loop constraint 1: default_target must exist and reference a node."""
    default_raw = edge.get("default_target")
    if default_raw is None:
        result.error(
            "LOOP_MISSING_DEFAULT_TARGET",
            f"Loop edge from '{source}' to '{target}' has no default_target",
            node_id=source,
        )
        return
    default_target = str(default_raw)
    if default_target not in node_ids:
        result.error(
            "LOOP_DEFAULT_TARGET_NOT_FOUND",
            f"Loop edge from '{source}' default_target '{default_target}' is not a node",
            node_id=source,
        )


def _check_loop_max_iterations(edge: dict[str, Any], source: str, result: ValidationResult) -> None:
    """Loop constraint 2: max_iterations must be a positive integer if set."""
    max_it = edge.get("max_iterations")
    if max_it is not None and not _is_non_negative_int(max_it):
        result.error(
            "LOOP_INVALID_MAX_ITERATIONS",
            f"Loop edge from '{source}' max_iterations must be a non-negative integer (got {max_it!r})",
            node_id=source,
        )


def _check_loop_expression(edge: dict[str, Any], source: str, result: ValidationResult) -> None:
    """Loop constraint 3: condition_expression must be valid JMESPath if set."""
    expr: object = edge.get("condition_expression")
    if isinstance(expr, str) and expr.strip():
        try:
            jmespath.compile(expr.strip())
        except jmespath.exceptions.JMESPathError as exc:
            result.error(
                "LOOP_INVALID_EXPRESSION",
                f"Loop edge from '{source}': invalid JMESPath expression: {exc}",
                node_id=source,
            )


def _check_llm_routing_prompt(node: dict[str, Any], nid: str, result: ValidationResult) -> None:
    """LLM routing 1: routing_prompt must be non-empty."""
    routing_prompt: object = node.get("routing_prompt")
    if not isinstance(routing_prompt, str) or not routing_prompt.strip():
        result.error(
            "LLM_ROUTING_MISSING_PROMPT",
            f"LLM routing node '{nid}' requires a non-empty routing_prompt",
            node_id=nid,
        )


def _edge_source(edge: dict[str, Any]) -> str:
    """Normalised source node id for an edge (falls back to ``source_node_id``)."""
    return _string_or_default(edge.get("source"), _string_or_default(edge.get("source_node_id"), ""))


def _check_llm_routing_labels(
    _node: dict[str, Any],
    edges: list[dict[str, Any]],
    nid: str,
    result: ValidationResult,
) -> None:
    """LLM routing 2: outgoing non-reject edges must have unique routing_labels."""
    labels: list[str] = []
    for edge in edges:
        if _edge_source(edge) != nid:
            continue
        if edge.get("type", edge.get("edge_type", "")) == "reject":
            continue
        label: object = edge.get("routing_label")
        if not label or not str(label).strip():
            result.error(
                "LLM_ROUTING_MISSING_LABEL",
                f"Edge from LLM routing node '{nid}' is missing a routing_label",
                node_id=nid,
            )
            continue
        label_str = str(label)
        if label_str in labels:
            result.error(
                "LLM_ROUTING_DUPLICATE_LABEL",
                f"Edge from LLM routing node '{nid}' has duplicate routing_label '{label_str}'",
                node_id=nid,
            )
        labels.append(label_str)


def _check_llm_routing_default(
    node: dict[str, Any],
    nid: str,
    node_ids: set[str],
    result: ValidationResult,
) -> None:
    """LLM routing 3: default_target must exist and reference a valid node."""
    default_raw = node.get("default_target")
    if default_raw is None:
        result.error(
            "LLM_ROUTING_MISSING_DEFAULT",
            f"LLM routing node '{nid}' requires a default_target",
            node_id=nid,
        )
        return
    default_target = str(default_raw)
    if default_target not in node_ids:
        result.error(
            "LLM_ROUTING_DEFAULT_NOT_FOUND",
            f"LLM routing node '{nid}' default_target '{default_target}' is not a node",
            node_id=nid,
        )

    # ------------------------------------------------------------------


class GraphValidator:
    """Validates a PipelineSnapshot's graph before save or execution."""

    MAX_NESTING_DEPTH = 3

    async def validate(
        self,
        snapshot: PipelineSnapshot,
        session: AsyncSession,
    ) -> ValidationResult:
        return await self.validate_definition(
            snapshot.graph_json,
            session,
            connector_bindings=snapshot.connector_bindings_json,
            model_backend_pins=snapshot.model_backend_pins_json,
            environment_profile_id=snapshot.environment_profile_id,
        )

    async def validate_definition(
        self,
        graph_json: dict[str, Any],
        session: AsyncSession,
        *,
        connector_bindings: list[dict[str, Any]] | None = None,
        model_backend_pins: list[dict[str, Any]] | None = None,
        environment_profile_id: uuid.UUID | None = None,
        guardrail_definitions: list[Any] | None = None,
    ) -> ValidationResult:
        """Validate a live graph definition or an immutable snapshot.

        *guardrail_definitions*, when provided, are the pipeline's
        ``eval_type='guardrail'`` ``EvalDefinition`` rows — used for the
        per-node guardrail cap check (FAR-223 item 7). Returns warnings and
        errors. Errors block execution; warnings are advisory.
        """
        result = ValidationResult()

        self._check_topology(graph_json, result)
        if not result.is_valid:
            return result

        self._check_edges(graph_json, result)
        self._check_ports(graph_json, result)
        self._check_sandbox_agent_config(graph_json, result)
        self._check_node_idempotent(graph_json, result)
        self._check_failure_and_retry(graph_json, result)
        await self._check_node_send_budget_bindings(graph_json, connector_bindings or [], session, result)
        self._check_parallel_run_context_writes(graph_json, result)
        self._check_schema_compatibility(graph_json, result)
        await self._check_connector_bindings(connector_bindings or [], session, result)
        await self._check_model_backends(model_backend_pins or [], session, result)
        await self._check_environment_capabilities(
            environment_profile_id,
            graph_json,
            session,
            result,
        )

        await self._check_node_categories(graph_json, session, result)
        await self._check_composite_nodes(graph_json, session, result)
        await self._check_parameter_references(graph_json, session, result)
        self._check_guardrail_caps(guardrail_definitions or [], result)
        self._check_guardrail_correction_bindings(guardrail_definitions or [], result)

        return result

    async def validate_for_run(
        self,
        snapshot: PipelineSnapshot,
        input_payload: dict[str, Any],
        session: AsyncSession,
    ) -> ValidationResult:
        """Pre-run validation — all save-time checks plus input schema checking.

        Returns errors only (no warnings). Any error blocks run start,
        unless the snapshot pre-dates the Phase 1 cutover (grace period).
        """
        result = ValidationResult()

        pre_existing = _is_pre_existing(snapshot)

        # Topology: hard errors block immediately.
        self._check_topology(snapshot.graph_json, result)
        if not result.is_valid:
            return self._strip_warnings(result)

        # Schema compatibility (field-level).
        await self._check_schema_compatibility_deep(
            snapshot.graph_json,
            session,
            result,
        )
        if not result.is_valid and pre_existing:
            _log.warning(
                "Schema incompatibility in pipeline %s (snapshot %s). Run proceeds in degraded mode.",
                snapshot.pipeline_id,
                snapshot.id,
            )
            result.warning(
                "SCHEMA_DEGRADED",
                "Schema incompatibility found. Run proceeds in degraded mode.",
            )
        elif not result.is_valid:
            return self._strip_warnings(result)

        # Input payload compatibility with entry node schema.
        if input_payload is None:
            result.error("INPUT_NULL_PAYLOAD", "Input payload cannot be None")
            return self._strip_warnings(result)
        await self._check_input_schema_compatibility(
            snapshot.graph_json,
            input_payload,
            session,
            result,
        )
        if not result.is_valid and pre_existing:
            _log.warning(
                "Input schema incompatibility in pipeline %s (snapshot %s). Run proceeds in degraded mode.",
                snapshot.pipeline_id,
                snapshot.id,
            )
            result.warning(
                "SCHEMA_DEGRADED",
                "Input schema incompatibility found. Run proceeds in degraded mode.",
            )
        elif not result.is_valid:
            return self._strip_warnings(result)

        # Sandbox agent config check.
        self._check_sandbox_agent_config(snapshot.graph_json, result)

        # Node idempotency flag check (FAR-295).
        self._check_node_idempotent(snapshot.graph_json, result)

        # Failure & retry + compensation rules (FAR-402 P5 §4F).
        self._check_failure_and_retry(snapshot.graph_json, result)

        await self._check_node_send_budget_bindings(
            snapshot.graph_json, snapshot.connector_bindings_json, session, result
        )

        # Edge validation.
        self._check_edges(snapshot.graph_json, result)

        # Port-addressed typed state (FAR-416 / F1): fan-in safety +
        # port-type validation. Backward-compatible for legacy graphs.
        self._check_ports(snapshot.graph_json, result)

        # Parallel fan-out / run_context writes (warnings are stripped by
        # _strip_warnings below, but the check runs for consistency).
        self._check_parallel_run_context_writes(snapshot.graph_json, result)

        # Connector and backend checks.
        await self._check_connector_bindings(snapshot.connector_bindings_json, session, result)
        await self._check_model_backends(snapshot.model_backend_pins_json, session, result)

        # Environment capability check.
        await self._check_environment_capabilities(
            snapshot.environment_profile_id,
            snapshot.graph_json,
            session,
            result,
        )

        # Node category check.
        await self._check_node_categories(snapshot.graph_json, session, result)

        # Composite node validation.
        await self._check_composite_nodes(snapshot.graph_json, session, result)

        # Parameter schema / set validation.
        await self._check_parameter_references(snapshot.graph_json, session, result)

        return self._strip_warnings(result)

    def _strip_warnings(self, result: ValidationResult) -> ValidationResult:
        """Return a copy containing only error-severity issues."""
        out = ValidationResult()
        out.issues = [issue for issue in result.issues if issue.severity == "error"]
        return out

    # ------------------------------------------------------------------
    # Topology
    # ------------------------------------------------------------------

    @staticmethod
    def _find_entry_candidates(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
        return [str(n["id"]) for n in nodes if str(n["id"]) not in {str(e["target"]) for e in edges}]

    @staticmethod
    def _collect_node_ids(nodes: list[dict[str, Any]], result: ValidationResult) -> set[str] | None:
        """Collect unique node ids; emits TOPOLOGY_NODE_MISSING_ID and returns None on a missing id."""
        node_ids: set[str] = set()
        for n in nodes:
            nid = n.get("id")
            if nid is None:
                result.error("TOPOLOGY_NODE_MISSING_ID", "A node is missing its 'id' field")
                return None
            node_ids.add(str(nid))
        return node_ids

    @staticmethod
    def _build_flow_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Forwarding edges only: exclude kickback, reject, and loop from topology flow."""
        return [e for e in edges if e.get("type") not in _SKIPPED_EDGE_TYPES]

    @staticmethod
    def _build_adjacency(nodes: list[dict[str, Any]], flow_edges: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Adjacency map from forwarding edges."""
        adj: dict[str, list[str]] = {str(n["id"]): [] for n in nodes}
        for edge in flow_edges:
            src, tgt = str(edge["source"]), str(edge["target"])
            adj[src].append(tgt)
        return adj

    @staticmethod
    def _find_reachable(adj: dict[str, list[str]], primary_entry: str) -> set[str]:
        """BFS from the primary entry candidate over forwarding edges."""
        visited: set[str] = set()
        queue: deque[str] = deque([primary_entry])
        while queue:
            nid = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            queue.extend(adj.get(nid, []))
        return visited

    def _check_topology(self, graph_json: dict[str, Any], result: ValidationResult) -> None:
        nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
        edges: list[dict[str, Any]] = graph_json.get("edges", [])

        if not nodes:
            result.error("TOPOLOGY_NO_NODES", "Graph has no nodes")
            return

        node_ids = self._collect_node_ids(nodes, result)
        if node_ids is None:
            return

        _check_edge_references(edges, node_ids, result)

        if not result.is_valid:
            return

        # Validate conditional edge JMESPath expressions.
        self._check_condition_expressions(edges, result)

        # Validate loop-edge constraints.
        self._check_loop_edges(edges, node_ids, result)

        # Validate LLM routing node configuration.
        self._check_llm_routing(nodes, edges, node_ids, result)

        # Determine forwarding edges (exclude kickback + reject + loop from topology flow).
        flow_edges = self._build_flow_edges(edges)

        # Entry node: no incoming forwarding edges
        entry_candidates = self._find_entry_candidates(nodes, flow_edges)
        if not entry_candidates:
            result.error("TOPOLOGY_CYCLE", "Graph has a cycle or no entry node")
            return

        # Build adjacency from forwarding edges.
        adj = self._build_adjacency(nodes, flow_edges)

        # BFS from the first entry candidate over forwarding edges.
        # Nodes not visited (including other entry candidates) are unreachable.
        visited = self._find_reachable(adj, entry_candidates[0])

        for nid in sorted(node_ids - visited):
            result.warning(
                "TOPOLOGY_UNREACHABLE",
                f"Node '{nid}' is unreachable from entry node",
                node_id=nid,
            )

        # Nesting depth: longest path from entry node to any leaf.
        self._check_nesting_depth(adj, list(entry_candidates), result)

    def _check_nesting_depth(
        self,
        adj: dict[str, list[str]],
        entry_ids: list[str],
        result: ValidationResult,
    ) -> None:
        """Compute longest path from entries to leaf via iterative DFS. Error if > MAX_NESTING_DEPTH."""

        def _max_depth(node: str, visited: frozenset[str], _remaining: int = 1000) -> int:
            # Iterative stack-based DFS. The previous recursive version consumed
            # multiple Python frames per graph level, so the interpreter's
            # recursion limit tripped before the _remaining guard could cap deep
            # graphs — raising RecursionError instead of emitting
            # TOPOLOGY_NESTING_EXCEEDED. Each stack frame carries the path's
            # visited set (children outside it are followed), the remaining
            # descent budget, and the edge count so far; the longest path found
            # is returned, capped at _remaining like the recursive walk.
            best = 0
            # (node, visited-along-current-path, remaining-budget, edges-so-far)
            stack: list[tuple[str, frozenset[str], int, int]] = [(node, visited, _remaining, 0)]
            while stack:
                cur, path_visited, remaining, edges = stack.pop()
                best = max(best, edges)
                if remaining <= 0:
                    continue
                stack.extend(
                    (child, path_visited | {cur}, remaining - 1, edges + 1)
                    for child in adj.get(cur, [])
                    if child not in path_visited
                )
            return best

        depth = max((_max_depth(eid, frozenset()) for eid in entry_ids), default=0)
        if depth > self.MAX_NESTING_DEPTH:
            result.error(
                "TOPOLOGY_NESTING_EXCEEDED",
                f"Graph nesting depth {depth} exceeds maximum {self.MAX_NESTING_DEPTH}",
            )

    # ------------------------------------------------------------------
    # Conditional edge expressions
    # ------------------------------------------------------------------

    def _check_condition_expressions(
        self,
        edges: list[dict[str, Any]],
        result: ValidationResult,
    ) -> None:
        """Validate JMESPath condition expressions on conditional edges
        and eval-reference conditions on HITL gates.

        Each conditional edge must have a non-empty ``condition_expression``
        that compiles as valid JMESPath.

        Each HITL gate with an ``eval_condition`` must have valid fields.
        """
        for edge in edges:
            self._check_jmespath_conditional(edge, result)
            self._check_hitl_eval_condition(edge, result)

    @staticmethod
    def _check_jmespath_conditional(
        edge: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        if edge.get("type") != "conditional":
            return
        source = edge.get("source")
        if source is None:
            source = edge.get("source_node_id")
        src = _string_or_default(source)
        expr: object = edge.get("condition_expression")
        if not isinstance(expr, str) or not expr.strip():
            result.error(
                "CONDITION_MISSING_EXPRESSION",
                f"Edge from '{src}': conditional edge requires a condition_expression",
                node_id=src,
            )
            return
        try:
            jmespath.compile(expr.strip())
        except jmespath.exceptions.JMESPathError as exc:
            result.error(
                "CONDITION_INVALID_EXPRESSION",
                f"Edge from '{src}': invalid JMESPath expression: {exc}",
                node_id=src,
            )

    @staticmethod
    def _check_hitl_eval_condition(
        edge: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """Validate eval_condition on a HITL gate config, if present."""
        hitl_config = edge.get("hitl_gate_config")
        if not isinstance(hitl_config, dict):
            return
        eval_cond = hitl_config.get("eval_condition")
        if not isinstance(eval_cond, dict):
            return
        source = edge.get("source")
        if source is None:
            source = edge.get("source_node_id")
        src = _string_or_default(source)
        eval_name: str | None = eval_cond.get("eval_name")
        if not eval_name or not eval_name.strip():
            result.error(
                "HITL_EVAL_CONDITION_MISSING_NAME",
                f"Edge from '{src}': eval_condition requires a non-empty eval_name",
                node_id=src,
            )
            return
        threshold = eval_cond.get("threshold")
        if not _is_valid_number(threshold):
            result.error(
                "HITL_EVAL_CONDITION_INVALID_THRESHOLD",
                f"Edge from '{src}': eval_condition.threshold must be a number",
                node_id=src,
            )
            return
        if not (0.0 <= threshold <= 1.0):
            result.error(
                "HITL_EVAL_CONDITION_THRESHOLD_RANGE",
                f"Edge from '{src}': eval_condition.threshold must be between 0.0 and 1.0 (got {threshold})",
                node_id=src,
            )
            return
        valid_ops = {"lt", "gt", "lte", "gte", "eq", "neq"}
        operator: str | None = eval_cond.get("operator")
        if operator not in valid_ops:
            result.error(
                "HITL_EVAL_CONDITION_INVALID_OPERATOR",
                f"Edge from '{src}': eval_condition.operator must be one of {valid_ops} (got {operator!r})",
                node_id=src,
            )

    @staticmethod
    def _check_loop_edges(
        edges: list[dict[str, Any]],
        node_ids: set[str],
        result: ValidationResult,
    ) -> None:
        """Validate loop-edge constraints.

        Checks:
        1. Every loop edge must have a ``default_target`` that references
           an existing node ID.
        2. If ``max_iterations`` is set, it must be a positive integer.
        3. If ``condition_expression`` is set, it must compile as valid
           JMESPath.
        """
        for edge in edges:
            if edge.get("type") != "loop":
                continue
            source = _string_or_default(edge.get("source"))
            target = _string_or_default(edge.get("target"))
            _check_loop_default_target(edge, source, target, node_ids, result)
            _check_loop_max_iterations(edge, source, result)
            _check_loop_expression(edge, source, result)

    @staticmethod
    def _check_llm_routing(
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        node_ids: set[str],
        result: ValidationResult,
    ) -> None:
        """Validate LLM routing node configuration.

        For each node with ``routing_mode: "llm"``:
        1. Must have a non-empty ``routing_prompt``.
        2. Outgoing non-reject edges must have ``routing_label`` values,
           and those values must be unique.
        3. ``default_target`` field must exist on the node def and
           reference a valid node ID.
        """
        for node in nodes:
            if node.get("routing_mode") != "llm":
                continue
            nid = _string_or_default(node.get("id"))
            _check_llm_routing_prompt(node, nid, result)
            _check_llm_routing_labels(node, edges, nid, result)
            _check_llm_routing_default(node, nid, node_ids, result)

    # Schema compatibility
    # ------------------------------------------------------------------

    @staticmethod
    def _build_schema_pins_map(
        graph_json: dict[str, Any],
    ) -> dict[str, dict[str, tuple[uuid.UUID, str] | None]]:
        """Build a map of (node_id, direction) -> (schema_id, version) from graph_json.

        Returns dict[node_id][direction] = (schema_id, schema_version) or None.
        """
        pins_map: dict[str, dict[str, tuple[uuid.UUID, str] | None]] = {}

        for node in graph_json.get("nodes", []):
            node_id = str(node.get("id"))
            if not node_id:
                continue

            pins_map[node_id] = {"input": None, "output": None}

            input_pin = node.get("input_schema_pin")
            if input_pin:
                sid = (
                    uuid.UUID(input_pin["schema_id"])
                    if isinstance(input_pin["schema_id"], str)
                    else input_pin["schema_id"]
                )
                pins_map[node_id]["input"] = (sid, input_pin["schema_version"])

            output_pin = node.get("output_schema_pin")
            if output_pin:
                sid = (
                    uuid.UUID(output_pin["schema_id"])
                    if isinstance(output_pin["schema_id"], str)
                    else output_pin["schema_id"]
                )
                pins_map[node_id]["output"] = (sid, output_pin["schema_version"])

        return pins_map

    def _check_schema_compatibility(
        self,
        graph_json: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        schemas = self._build_schema_pins_map(graph_json)

        for edge in graph_json.get("edges", []):
            if edge.get("type") in _SKIPPED_EDGE_TYPES:
                continue
            src, tgt = str(edge["source"]), str(edge["target"])
            src_out = schemas.get(src, {}).get("output")
            tgt_in = schemas.get(tgt, {}).get("input")

            if src_out is None or tgt_in is None:
                continue

            if src_out[0] != tgt_in[0]:
                result.error(
                    "SCHEMA_INCOMPATIBLE",
                    f"Edge {src}→{tgt}: output schema '{src_out[0]}' != input schema '{tgt_in[0]}'",
                    node_id=src,
                )

    async def _check_schema_compatibility_deep(
        self,
        graph_json: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Field-level schema: output fields must exist in input with compatible types."""
        pins = self._build_schema_pins_map(graph_json)

        all_pins: dict[tuple[uuid.UUID, str], None] = {
            direction_pin: None
            for node_pins in pins.values()
            for direction_pin in node_pins.values()
            if direction_pin is not None
        }

        if not all_pins:
            return

        definitions = await self._resolve_schema_definitions(all_pins, session)

        for edge in graph_json.get("edges", []):
            if edge.get("type") in _SKIPPED_EDGE_TYPES:
                continue
            src, tgt = str(edge["source"]), str(edge["target"])
            src_out_pin = pins.get(src, {}).get("output")
            tgt_in_pin = pins.get(tgt, {}).get("input")

            if src_out_pin is None or tgt_in_pin is None:
                continue

            src_schema_id = src_out_pin[0]
            tgt_schema_id = tgt_in_pin[0]

            out_def = definitions.get(src_schema_id, {})
            in_def = definitions.get(tgt_schema_id, {})

            if not out_def or not in_def:
                continue

            has_deferred = any(k in out_def or k in in_def for k in _DEFERRED_SCHEMA_KEYWORDS)
            if has_deferred:
                result.warning(
                    "SCHEMA_CHECK_DEFERRED",
                    f"Edge {src}→{tgt}: schema contains deferred keywords ($ref, oneOf, etc.). "
                    f"Full structural validation deferred.",
                    node_id=src,
                )

            errors = self._check_schema_fields(out_def, in_def, path="")
            for error in errors:
                result.error(
                    "SCHEMA_FIELD_INCOMPATIBLE",
                    f"Edge {src}→{tgt}: {error}",
                    node_id=src,
                )

    def _check_schema_fields(
        self,
        out_field: dict[str, Any],
        in_field: dict[str, Any],
        path: str = "",
        depth: int = 0,
    ) -> list[str]:
        """Check if out_field is compatible with in_field (subtype check).

        Recursively checks type compatibility, required fields, additionalProperties,
        nested properties, array items, and enum subsets. Returns a list of error
        messages (empty means compatible).
        """
        if depth > _SCHEMA_MAX_DEPTH:
            return []

        errors: list[str] = []
        out_type = out_field.get("type")
        in_type = in_field.get("type")

        # Handle nullable (array of types)
        if isinstance(in_type, list):
            errors.extend(_check_list_type_mismatch(path, out_type, in_type))
            return errors

        if isinstance(out_type, list):
            errors.extend(_check_nullable_type_mismatch(path, out_type, in_type))
            return errors

        errors.extend(_check_scalar_type_mismatch(path, out_type, in_type))

        # Check additionalProperties
        _check_additional_properties(in_field, out_field, path, errors)

        # Check nested properties
        errors.extend(self._check_nested_properties(out_field, in_field, path, depth))

        # Check array items
        errors.extend(self._check_array_items(out_field, in_field, path, depth))

        # Check enum compatibility
        errors.extend(_check_enum_subset(path, out_field.get("enum"), in_field.get("enum")))

        return errors

    def _check_nested_properties(
        self,
        out_field: dict[str, Any],
        in_field: dict[str, Any],
        path: str,
        depth: int,
    ) -> list[str]:
        """Check required fields in the output and recurse into shared properties."""
        errors: list[str] = []
        out_properties = out_field.get("properties", {})
        in_properties = in_field.get("properties", {})

        if not (isinstance(out_properties, dict) and isinstance(in_properties, dict)):
            return errors

        in_required = in_field.get("required", [])
        errors.extend(
            f"{path}.{req_field}: required field missing in output"
            for req_field in in_required
            if req_field not in out_properties
        )

        for field_name in set(out_properties) & set(in_properties):
            errors.extend(
                self._check_schema_fields(
                    out_properties[field_name],
                    in_properties[field_name],
                    f"{path}.{field_name}",
                    depth + 1,
                )
            )
        return errors

    def _check_array_items(
        self,
        out_field: dict[str, Any],
        in_field: dict[str, Any],
        path: str,
        depth: int,
    ) -> list[str]:
        """Recurse into array item schemas when both sides declare items."""
        out_items = out_field.get("items", {})
        in_items = in_field.get("items", {})
        if not (isinstance(out_items, dict) and isinstance(in_items, dict)):
            return []
        return self._check_schema_fields(out_items, in_items, f"{path}.items", depth + 1)

    async def _resolve_schema_definitions(
        self,
        schema_pins: dict[tuple[uuid.UUID, str], None],
        session: AsyncSession,
    ) -> dict[uuid.UUID, dict[str, Any]]:
        """Resolve schema definitions for the given (schema_id, version) pins.

        Queries by exact version, not latest published.
        """
        if not schema_pins:
            return {}

        definitions: dict[uuid.UUID, dict[str, Any]] = {}

        for schema_id, version in schema_pins:
            stmt = select(SchemaVersion).where(
                SchemaVersion.schema_id == schema_id,
                SchemaVersion.version == version,
            )
            result = await session.execute(stmt)
            sv = result.scalar_one_or_none()

            if sv is None:
                _log.warning(
                    "Schema version not found: %s/%s. It may have been deleted.",
                    schema_id,
                    version,
                )
                continue

            definitions[schema_id] = dict(sv.definition_json) if sv.definition_json else {}

        return definitions

    async def _check_input_schema_compatibility(
        self,
        graph_json: dict[str, Any],
        input_payload: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Check pipeline input payload against the first node's input schema."""
        nodes = graph_json.get("nodes", [])
        if not nodes:
            return

        first_node = nodes[0]
        input_pin = first_node.get("input_schema_pin")

        if not input_pin:
            return

        schema_id = input_pin["schema_id"]
        if isinstance(schema_id, str):
            schema_id = uuid.UUID(schema_id)

        stmt = select(SchemaVersion).where(
            SchemaVersion.schema_id == schema_id,
            SchemaVersion.version == input_pin["schema_version"],
        )
        sv = await session.execute(stmt)
        schema_version = sv.scalar_one_or_none()

        if not schema_version:
            return

        errors = self._validate_payload(input_payload, schema_version.definition_json)
        for error in errors:
            result.error(
                "INPUT_SCHEMA_MISMATCH",
                f"Input payload does not match schema: {error}",
            )

    def _validate_payload(
        self,
        payload: dict[str, Any],
        schema_definition: dict[str, Any] | None,
    ) -> list[str]:
        """Validate a payload against a JSON schema definition.

        Returns a list of error messages (empty means valid).
        """
        errors: list[str] = []
        if not schema_definition:
            return errors

        properties = schema_definition.get("properties", {})
        if not isinstance(properties, dict):
            return errors

        required = schema_definition.get("required", [])

        errors.extend(f"Missing required field '{field_name}'" for field_name in required if field_name not in payload)

        for field_name, field_def in properties.items():
            if not isinstance(field_def, dict):
                continue
            if field_name not in payload:
                continue

            expected_type = field_def.get("type")
            if expected_type is None:
                continue

            val = payload[field_name]
            type_map_entry = _JSON_TYPE_MAP.get(expected_type, object)
            is_bool = isinstance(val, bool)
            matches = isinstance(val, type_map_entry) and not (is_bool and expected_type in ("integer", "number"))
            if not matches:
                actual_type = type(val).__name__
                errors.append(f"Field '{field_name}' expected type '{expected_type}', got '{actual_type}'")

        return errors

    # ------------------------------------------------------------------
    # Connector bindings
    # ------------------------------------------------------------------

    async def _check_connector_bindings(
        self,
        bindings: list[dict[str, Any]],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        if not bindings:
            return

        raw_ids = [b.get("connector_instance_id") for b in bindings]
        instance_ids, _ = try_parse_uuids(raw_ids)
        if not instance_ids:
            return

        rows = (
            (await session.execute(select(ConnectorInstance).where(ConnectorInstance.id.in_(instance_ids))))
            .scalars()
            .all()
        )
        found: dict[uuid.UUID, ConnectorInstance] = {r.id: r for r in rows}

        for binding in bindings:
            node_id: str | None = str(binding.get("node_id")) if binding.get("node_id") else None
            cid_obj = try_parse_uuid(binding.get("connector_instance_id"))
            if cid_obj is None:
                continue
            cid = cid_obj
            instance = found.get(cid)

            if instance is None:
                result.error("CONNECTOR_NOT_FOUND", f"Connector instance {cid} not found", node_id)
                continue

            if instance.status != "active":
                result.error(
                    "CONNECTOR_INACTIVE",
                    f"Connector {cid} ({instance.name!r}) has status {instance.status!r}",
                    node_id,
                )

            required_ops: list[str] = binding.get("required_operations", [])
            allowed_ops: list[str] = instance.allowed_operations or []
            missing = [op for op in required_ops if op not in allowed_ops]
            if missing:
                result.error(
                    "CONNECTOR_MISSING_OPERATIONS",
                    f"Connector {cid} missing operations: {missing}",
                    node_id,
                )

    # ------------------------------------------------------------------
    # Model backend health
    # ------------------------------------------------------------------

    async def _check_model_backends(
        self,
        pins: list[dict[str, Any]],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        if not pins:
            return

        raw_ids = [p.get("model_backend_id") for p in pins]
        backend_ids, _ = try_parse_uuids(raw_ids)
        if not backend_ids:
            return

        rows = (await session.execute(select(ModelBackend).where(ModelBackend.id.in_(backend_ids)))).scalars().all()
        found: dict[uuid.UUID, ModelBackend] = {r.id: r for r in rows}

        for pin in pins:
            node_id: str | None = str(pin.get("node_id")) if pin.get("node_id") else None
            bid_obj = try_parse_uuid(pin.get("model_backend_id"))
            if bid_obj is None:
                continue
            bid = bid_obj
            backend = found.get(bid)

            if backend is None:
                result.error("MODEL_BACKEND_NOT_FOUND", f"Model backend {bid} not found", node_id)
                continue

            if backend.status != "active":
                result.error(
                    "MODEL_BACKEND_INACTIVE",
                    f"Model backend {bid} ({backend.name!r}) has status {backend.status!r}",
                    node_id,
                )
                continue

            if backend.last_health_check_error:
                result.error(
                    "MODEL_BACKEND_UNHEALTHY",
                    f"Model backend '{backend.name}' (id={bid}) is unhealthy: {backend.last_health_check_error}",
                    node_id,
                )

    # ------------------------------------------------------------------
    # Environment capabilities
    # ------------------------------------------------------------------

    async def _check_environment_capabilities(
        self,
        environment_profile_id: uuid.UUID | None,
        graph_json: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Check that the bound EnvironmentProfile covers all agent capabilities.

        Hard-block if any agent requires a capability the profile does not declare.
        Skipped if no environment_profile_id is set on the snapshot.
        """
        if environment_profile_id is None:
            return

        profile = await session.get(EnvironmentProfile, environment_profile_id)
        if profile is None:
            result.error(
                "ENV_PROFILE_NOT_FOUND",
                f"EnvironmentProfile {environment_profile_id} not found",
            )
            return

        agent_ids: set[uuid.UUID] = set()
        for node in graph_json.get("nodes", []):
            raw = node.get("agent_id")
            if raw is not None:
                parsed = try_parse_uuid(raw)
                if parsed is not None:
                    agent_ids.add(parsed)

        if not agent_ids:
            return

        rows = (await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()

        profile_caps: set[str] = set(profile.capabilities_json or [])

        for agent in rows:
            _check_agent_capabilities(agent, profile, profile_caps, result)

    # ------------------------------------------------------------------
    # Node categories
    # ------------------------------------------------------------------

    async def _check_node_categories(
        self,
        graph_json: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Check that all ``node_category_id`` references are valid.

        Delegates to ``validate_node_categories`` for the actual check
        and merges results into the running result.
        """
        cat_result = await validate_node_categories(graph_json, session)
        result.issues.extend(cat_result.issues)

    # ------------------------------------------------------------------
    # Guardrail per-node cap (FAR-223 item 7)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_guardrail_caps(
        guardrail_rows: list[Any],
        result: ValidationResult,
    ) -> None:
        """Reject a graph where a single node binds more than the guardrail cap.

        The cap is org-configurable via guardrail config-as-code (mirrored onto
        every row's ``config_json``); ``guardrail_cap_violation`` shares the
        same resolution the ``create_run`` seam enforces at run start, so the
        authoring-time rejection and the dispatch-time fail-closed backstop
        agree. A violation is a hard error — the graph-save route turns it into
        a 422.
        """
        if not guardrail_rows:
            return
        from modulo.core.guardrails import guardrail_cap_violation, to_engine_definition

        definitions = [to_engine_definition(row) for row in guardrail_rows]
        violation = guardrail_cap_violation(definitions)
        if violation:
            result.error("GUARDRAIL_CAP_EXCEEDED", violation)

    @staticmethod
    def _check_guardrail_correction_bindings(
        guardrail_rows: list[Any],
        result: ValidationResult,
    ) -> None:
        """Reject a ``redact``-action guardrail that declares a ``correction`` block.

        FAR-210: a correction on a redaction guardrail is an exfiltration
        channel for the exact data redaction protects. The runtime
        ``RedactCorrectBlockedError`` (``modulo.core.guardrails.correction``)
        is the fail-closed backstop; this save-time check rejects the
        mis-bound config at authoring time so it can never be saved onto a
        redaction guardrail.
        """
        if not guardrail_rows:
            return
        from modulo.core.guardrails import GuardrailAction

        for row in guardrail_rows:
            config = getattr(row, "config_json", None)
            if not isinstance(config, dict):
                continue
            if config.get("action") != GuardrailAction.REDACT.value:
                continue
            if not isinstance(config.get("correction"), dict):
                continue
            result.error(
                "REDACT_CORRECT_BLOCKED",
                f"Guardrail {row.name!r} declares a 'correction' block on a 'redact'-action "
                "guardrail — a correction on a redaction guardrail is an exfiltration channel "
                "for the exact data redaction protects. Remove the correction block or change "
                "the guardrail action.",
            )

    # ------------------------------------------------------------------
    # Composite nodes
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Parameter schema / set references
    # ------------------------------------------------------------------

    async def _check_parameter_references(
        self,
        graph_json: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Validate parameter schema and set references on agent nodes.

        For each agent node that references a parameter_schema_id (embedded
        from the Agent model at snapshot creation):
        1. Verify the ParameterSchema exists.
        2. If a ParameterSet is referenced (parameter_set_id), verify it
           exists and its schema_version matches the current schema version.
        3. If schema version has drifted since the set was created, warn.
        """
        nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
        schema_ids: set[uuid.UUID] = set()
        set_ids: set[uuid.UUID] = set()

        for node in nodes:
            raw_schema_id = node.get("parameter_schema_id")
            if raw_schema_id is not None:
                parsed = try_parse_uuid(raw_schema_id)
                if parsed is not None:
                    schema_ids.add(parsed)
            raw_set_id = node.get("parameter_set_id")
            if raw_set_id is not None:
                parsed = try_parse_uuid(raw_set_id)
                if parsed is not None:
                    set_ids.add(parsed)

        if not schema_ids and not set_ids:
            return

        # Fetch all referenced schemas.
        schemas: dict[uuid.UUID, ParameterSchema] = {}
        if schema_ids:
            schema_rows = (
                (await session.execute(select(ParameterSchema).where(ParameterSchema.id.in_(schema_ids))))
                .scalars()
                .all()
            )
            schemas = {s.id: s for s in schema_rows}

        # Fetch all referenced sets.
        sets: dict[uuid.UUID, ParameterSet] = {}
        if set_ids:
            set_rows = (await session.execute(select(ParameterSet).where(ParameterSet.id.in_(set_ids)))).scalars().all()
            sets = {s.id: s for s in set_rows}

        for node in nodes:
            _check_parameter_node(node, schemas, sets, result)

    async def _check_composite_nodes(
        self,
        graph_json: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Validate composite node references.

        For each composite node in the graph:
        1. Verify the referenced ``CompositeTemplate`` exists.
        2. Check that required parameter ports have values (if template
           declares ``parameter_ports_json`` with ``required: true``).
        3. Check that the composite version exists — a version constraint
           may be stored on the binding; for now we verify the template
           record itself exists (version validation is deferred to
           execution time).
        4. Validate the template's sub-pipeline graph structure (unique
           sub-node ids, valid sub-edge references, supported sub-node types,
           sandbox sub-node config).
        """
        nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
        composite_nodes = [n for n in nodes if n.get("node_type") == "composite" or n.get("composite_ref") is not None]

        if not composite_nodes:
            return

        composite_refs: set[uuid.UUID] = set()
        node_ref_map: dict[str, uuid.UUID] = {}
        for node in composite_nodes:
            raw = node.get("composite_ref")
            nid = _string_or_default(node.get("id"))
            if raw is not None:
                parsed = try_parse_uuid(raw)
                if parsed is not None:
                    composite_refs.add(parsed)
                    node_ref_map[nid] = parsed
                else:
                    result.error(
                        "COMPOSITE_INVALID_REF",
                        f"Node '{nid}': composite_ref is not a valid UUID",
                        node_id=nid,
                    )

        if not composite_refs:
            return

        rows = (
            (await session.execute(select(CompositeTemplate).where(CompositeTemplate.id.in_(composite_refs))))
            .scalars()
            .all()
        )
        found: dict[uuid.UUID, CompositeTemplate] = {r.id: r for r in rows}

        for node in composite_nodes:
            _check_composite_node(node, node_ref_map, found, self, result)

    def _check_composite_subgraph(
        self,
        template: CompositeTemplate,
        node_id: str,
        result: ValidationResult,
    ) -> None:
        """Validate a composite template's sub-pipeline graph structure.

        Emits ``COMPOSITE_SUBGRAPH_*`` errors so malformed templates are caught
        at save time rather than when a run tries to expand them. Only runs when
        ``sub_pipeline_graph_json`` is a real dict (mocks and legacy rows that
        lack it are skipped).
        """
        graph = template.sub_pipeline_graph_json
        if not isinstance(graph, dict):
            return
        sub_nodes = graph.get("nodes")
        if not isinstance(sub_nodes, list) or not sub_nodes:
            result.error(
                "COMPOSITE_SUBGRAPH_EMPTY",
                f"Node '{node_id}': CompositeTemplate '{template.id}' has no sub-pipeline nodes",
                node_id=node_id,
            )
            return

        ctx = _CompositeContext(template=template, node_id=node_id)
        sub_ids = _check_composite_sub_nodes(ctx, sub_nodes, result)

        sub_edges = graph.get("edges")
        if not isinstance(sub_edges, list):
            return
        _check_composite_sub_edges(ctx, sub_ids, sub_edges, result)

    def _check_output_validation(
        self,
        node_id: str,
        output_validation: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """Validate output validation config on a composite node.

        Checks:
        1. max_validation_retries must be 0-5.
        2. Each eval definition must have a valid type.
        3. Regex patterns must compile.
        4. JSON Schema configs must have 'schema' or valid 'schema_ref'.
        5. failure_behaviour must be valid.
        """
        max_retries = output_validation.get("max_validation_retries", 0)
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, (int, float))
            or max_retries < 0
            or max_retries > 5
        ):
            result.error(
                "COMPOSITE_VALIDATION_RETRIES_RANGE",
                f"Node '{node_id}': max_validation_retries must be an integer between 0 and 5 (got {max_retries!r})",
                node_id=node_id,
            )

        valid_types = {"regex", "json_schema", "llm_judge", "human_set"}
        valid_behaviours = {"retry", "block", "warn"}

        eval_definitions: list[dict[str, Any]] = output_validation.get("eval_definitions", [])
        if not isinstance(eval_definitions, list):
            return
        for i, eval_def in enumerate(eval_definitions):
            eval_id = eval_def.get("id", f"#{i}")
            eval_name = eval_def.get("name", eval_id)

            eval_type = eval_def.get("type")
            if eval_type not in valid_types:
                result.error(
                    "COMPOSITE_VALIDATION_INVALID_TYPE",
                    f"Node '{node_id}', eval '{eval_name}': type must be one of {valid_types} (got {eval_type!r})",
                    node_id=node_id,
                )

            behaviour = eval_def.get("failure_behaviour", "retry")
            if behaviour not in valid_behaviours:
                result.error(
                    "COMPOSITE_VALIDATION_INVALID_BEHAVIOUR",
                    f"Node '{node_id}', eval '{eval_name}': failure_behaviour must be"
                    f" one of {valid_behaviours} (got {behaviour!r})",
                    node_id=node_id,
                )

            config: dict[str, Any] = eval_def.get("config", {})

            if eval_type == "regex":
                self._check_regex_eval(node_id, eval_name, config, result)

            elif eval_type == "json_schema":
                self._check_json_schema_eval(node_id, eval_name, config, result)

    def _check_regex_eval(
        self,
        node_id: str,
        eval_name: str,
        config: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        if not config.get("field"):
            result.error(
                "COMPOSITE_VALIDATION_REGEX_NO_FIELD",
                f"Node '{node_id}', eval '{eval_name}': regex eval missing 'field' in config",
                node_id=node_id,
            )
        pattern = config.get("pattern")
        if pattern is None or (isinstance(pattern, str) and not pattern):
            result.error(
                "COMPOSITE_VALIDATION_REGEX_NO_PATTERN",
                f"Node '{node_id}', eval '{eval_name}': regex eval missing 'pattern' in config",
                node_id=node_id,
            )
        elif isinstance(pattern, str):
            try:
                re.compile(pattern)
            except re.error as exc:
                result.error(
                    "COMPOSITE_VALIDATION_REGEX_INVALID",
                    f"Node '{node_id}', eval '{eval_name}': regex pattern '{pattern}' failed to compile: {exc}",
                    node_id=node_id,
                )
        else:
            result.error(
                "COMPOSITE_VALIDATION_REGEX_INVALID_TYPE",
                f"Node '{node_id}', eval '{eval_name}': regex pattern must be a string, got {type(pattern).__name__}",
                node_id=node_id,
            )

    def _check_json_schema_eval(
        self,
        node_id: str,
        eval_name: str,
        config: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        if not config.get("schema") and not config.get("schema_ref"):
            result.error(
                "COMPOSITE_VALIDATION_SCHEMA_MISSING",
                f"Node '{node_id}', eval '{eval_name}': json_schema eval requires 'schema' or 'schema_ref' in config",
                node_id=node_id,
            )

    # ------------------------------------------------------------------
    # Sandbox agent config
    # ------------------------------------------------------------------

    @staticmethod
    def _check_sandbox_agent_config(graph_json: dict[str, Any], result: ValidationResult) -> None:
        """Validate sandbox_agent node configurations.

        Checks:
        1. agent_command (llm mode) / script_command (script mode) is non-empty,
           per the shared mode-aware validator (FAR-296).
        2. template_id is set.
        3. timeout_seconds within bounds (60-604800) if set.
        4. context_files source paths start with /.
        5. env_vars keys avoid reserved prefixes.
        6. output_schema_json has valid JSON Schema structure if present.
        7. stall_timeout_seconds is a positive number, not exceeding timeout_seconds.
        8. FAR-306 opt-in stall-detector fields (stdout_percentage_delta,
           watch_globs, watch_log_path, enable_heartbeat) are well-formed.
        9. agent_command is Jinja-renderable (FAR-226).
        10. read_only / git_credentials are validated sandbox-only fields
            (FAR-212 PR B), and no non-sandbox node carries them.
        """
        _reserved_env_prefixes = ("MODULO_", "OPENCODE_API_KEY")

        _check_sandbox_policy_fields_only_on_sandbox_nodes(graph_json, result)
        for node in graph_json.get("nodes", []):
            if node.get("node_type") != "sandbox_agent":
                continue
            nid = _string_or_default(node.get("id"))
            _check_sandbox_command(node, nid, result)
            _check_sandbox_jinja(node, nid, result)
            _check_sandbox_template(node, nid, result)
            _check_sandbox_timeout(node, nid, result)
            _check_sandbox_timeout_e2b_cap(node, nid, result)
            _check_sandbox_stall_timeout(node, nid, result)
            _check_sandbox_context_files(node, nid, result)
            _check_sandbox_env_vars(node, nid, _reserved_env_prefixes, result)
            _check_sandbox_output_schema(node, nid, result)
            _check_sandbox_loop_intercept(node, nid, result)
            _check_sandbox_stall_detectors(node, nid, result)
            _check_sandbox_egress(node, nid, result)
            _check_sandbox_resource_limits(node, nid, result)
            _check_sandbox_read_only(node, nid, result)
            _check_sandbox_git_credentials(node, nid, result)
            _check_sandbox_wallclock_budget(node, nid, result)

    # ------------------------------------------------------------------
    # Node idempotency
    # ------------------------------------------------------------------

    @staticmethod
    def _check_node_idempotent(graph_json: dict[str, Any], result: ValidationResult) -> None:
        """Validate the ``idempotent`` flag on every node (FAR-295).

        ``idempotent`` defaults to ``true``; a node that sets it must use an
        actual boolean. A non-boolean value (e.g. the string ``"false"``) would
        be silently treated as idempotent by the executor's retry gate — and a
        non-idempotent side effect could then be re-run by an auto-retry. Fail
        closed at save time instead.
        """
        for node in graph_json.get("nodes", []) or []:
            if not isinstance(node, dict) or "idempotent" not in node:
                continue
            if not isinstance(node.get("idempotent"), bool):
                nid = _string_or_default(node.get("id"))
                result.error(
                    "NODE_IDEMPOTENT_INVALID",
                    f"Node '{nid}': idempotent must be a boolean (true = safe to re-run, "
                    f"false = never auto-retry), got {node.get('idempotent')!r}",
                    node_id=nid,
                )

    # ------------------------------------------------------------------
    # Failure & retry (FAR-402 P5 / §4F)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_failure_and_retry(graph_json: dict[str, Any], result: ValidationResult) -> None:
        """Compile-time failure/retry + compensation rules (FAR-402 P5 §4F).

        1. Per-node ``retry`` config must be well-formed.
        2. Per-edge transition ``retry`` config must be well-formed.
        3. An edge may not declare BOTH a transition ``retry`` and an
           ``on_failure_target`` (mutually exclusive per failure).
        4. An edge's ``on_failure_target`` must reference an existing node.
        5. The compensation graph must be ACYCLIC (a cycle is a typed error,
           rejected at compile time, not at run time).

        All checks are additive over the existing graph — a graph with no
        ``retry``/``on_failure_target`` config compiles exactly as before.
        """
        # Lazy import: the shim modules (pipeline_engine) import the executor
        # which imports this package at module-load time; importing
        # retry_compensation eagerly would re-enter that deadlock. At call time
        # the package is already initialised.
        from modulo.core.pipeline_engine import retry_compensation as _rc

        nodes = graph_json.get("nodes", []) if isinstance(graph_json, dict) else []
        edges = graph_json.get("edges", []) if isinstance(graph_json, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = _string_or_default(node.get("id"))
            _rc.validate_node_retry_config(node, nid, result)
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            edge_id = _string_or_default(edge.get("source", edge.get("source_node_id")))
            _rc.validate_edge_retry_config(edge, edge_id, result)
            _rc.validate_edge_mutual_exclusion(edge, result)
        _rc.validate_compensation_target_exists(
            [e for e in edges if isinstance(e, dict)],
            [n for n in nodes if isinstance(n, dict)],
            result,
        )
        _rc.validate_compensation_acyclic(graph_json, result)

    # ------------------------------------------------------------------

    # Node send budget reconcile — FAR-410 flat node-key path.
    # Kept so the branch's direct unit tests (GraphValidator._check_node_send_budget)
    # still exercise the flat-key reconcile; the validate path uses the
    # connector-config reconcile in :meth:`_check_node_send_budget_bindings`.
    @staticmethod
    def _check_node_send_budget(graph_json: dict[str, Any], result: ValidationResult) -> None:
        """Warn when a fan-out node's send budget exceeds its wait_for budget (FAR-410).

        A connector node may fan out over ``fanout_cardinality`` items and run
        each with a ``per_item_budget`` — but all of that must fit inside the
        node's total ``wait_for`` budget (``node_wait_for`` or ``timeout_seconds``
        when no explicit wait_for is set). When the per-item sends cannot
        sequentially fit in the budget, retries collide with the node deadline
        and every attempt gets cancelled mid-send (UNKNOWN outcomes). This is a
        save-time warning, not a hard error: nodes without these config keys
        (every existing graph) are unaffected, and it is advisory rather than
        a blocker so an operator can still save while they reconcile.
        """
        for node in graph_json.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            fanout = node.get("fanout_cardinality")
            per_item = node.get("per_item_budget")
            if fanout is None and per_item is None:
                continue
            nid = _string_or_default(node.get("id"))
            fanout_val = _as_positive_number(fanout)
            per_item_val = _as_positive_number(per_item)
            if fanout_val is None or per_item_val is None:
                continue
            wait_for = _as_positive_number(node.get("node_wait_for"))
            if wait_for is None:
                wait_for = _as_positive_number(node.get("timeout_seconds"))
            if wait_for is None:
                continue
            total = fanout_val * per_item_val
            if total > wait_for:
                result.warning(
                    "NODE_SEND_BUDGET_OVERSUBSCRIBED",
                    f"Node '{nid}': fanout_cardinality={fanout_val} x per_item_budget={per_item_val} "
                    f"({total:.1f}s) exceeds node wait_for budget {wait_for:.1f}s — retries will be "
                    "cancelled mid-send, producing UNKNOWN outcomes. Raise node_wait_for or lower "
                    "per_item_budget.",
                    node_id=nid,
                )

    # Node send budget reconcile (FAR-410 / FAR-411)
    # ------------------------------------------------------------------

    async def _check_node_send_budget_bindings(
        self,
        graph_json: dict[str, Any],
        connector_bindings: list[dict[str, Any]] | None,
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Warn when a fan-out node's send budget exceeds its timeout budget.

        A REST fan-out connector node may fan out over ``fan_out.max_cardinality``
        items and run each with a ``fan_out.per_item_timeout`` — but all of that
        must fit inside the node's total ``timeout_seconds`` budget. The per-item
        worst case carries a retry multiplier: each item may be retried
        ``fan_out.max_retries`` times (default 2 → 3 attempts), so the worst case
        is ``max_cardinality x per_item_timeout x (max_retries + 1)``.

        The fan-out config is read from the bound :class:`ConnectorInstance`
        ``config_json.fan_out`` (the single source of truth the connector also
        consumes), reconciled against the node's actual ``timeout_seconds`` — the
        previous flat-node-key path read a fabricated namespace and was a dead
        check. When the sends cannot sequentially fit in the budget, retries
        collide with the node deadline and every attempt gets cancelled mid-send
        (UNKNOWN outcomes). This is a save-time warning, not a hard error: nodes
        without a fan-out connector binding are unaffected, and it is advisory so
        an operator can still save while they reconcile.

        Raising when the bound connector instance cannot be resolved is avoided:
        the check only warns when the fan-out config is genuinely present, so an
        unresolved binding simply skips the advisory (the ``CONNECTOR_NOT_FOUND``
        check in :meth:`_check_connector_bindings` already covers it).
        """
        if not connector_bindings:
            return

        nodes = graph_json.get("nodes", []) or []
        node_map = {_string_or_default(n.get("id")): n for n in nodes if isinstance(n, dict)}

        raw_ids = [b.get("connector_instance_id") for b in connector_bindings]
        instance_ids, _ = try_parse_uuids(raw_ids)
        if not instance_ids:
            return

        rows = (
            (await session.execute(select(ConnectorInstance).where(ConnectorInstance.id.in_(instance_ids))))
            .scalars()
            .all()
        )
        found: dict[uuid.UUID, ConnectorInstance] = {r.id: r for r in rows}

        for binding in connector_bindings:
            self._reconcile_binding_send_budget(binding, node_map, found, result)

    def _reconcile_binding_send_budget(
        self,
        binding: dict[str, Any],
        node_map: dict[str, Any],
        found: dict[uuid.UUID, ConnectorInstance],
        result: ValidationResult,
    ) -> None:
        """Emit NODE_SEND_BUDGET_OVERSUBSCRIBED for a single fan-out binding.

        Mirrors :meth:`_check_node_send_budget`: a binding only counts when its
        node resolves, its connector instance is found, and the instance's
        ``fan_out`` config is genuinely fan-out-active (``items_path`` truthy).
        """
        node_id = str(binding.get("node_id")) if binding.get("node_id") else None
        if node_id is None:
            return
        node = node_map.get(node_id)
        if not isinstance(node, dict):
            return
        cid_obj = try_parse_uuid(binding.get("connector_instance_id"))
        if cid_obj is None:
            return
        instance = found.get(cid_obj)
        if instance is None:
            return
        fanout = (instance.config_json or {}).get("fan_out")
        if not isinstance(fanout, dict):
            return
        # The connector only fans out when ``items_path`` is truthy (rest/__init__.py
        # write()): ``_fanout_enabled = bool(enabled or items_path)`` and
        # ``_fanout_items_path = items_path``, so the effective predicate reduces to
        # ``bool(items_path)`` — ``enabled`` alone is NOT sufficient. An inert
        # fan_out (``{}`` / ``{"enabled": true}``) runs as a single call and must
        # not be reconciled against a fan-out send budget.
        if not fanout.get("items_path"):
            return
        params = self._resolve_fanout_budget(fanout)
        if params is None:
            return
        max_cardinality, per_item_timeout, attempts = params
        wait_for = _as_positive_number(node.get("timeout_seconds"))
        if wait_for is None:
            return
        total = max_cardinality * per_item_timeout * attempts
        if total > wait_for:
            result.warning(
                "NODE_SEND_BUDGET_OVERSUBSCRIBED",
                f"Node '{node_id}': fan_out max_cardinality={max_cardinality} "
                f"x per_item_timeout={per_item_timeout} x attempts={attempts} "
                f"({total:.1f}s) exceeds node timeout_seconds budget {wait_for:.1f}s — "
                "retries will be cancelled mid-send, producing UNKNOWN outcomes. Raise "
                "timeout_seconds, lower per_item_timeout, or reduce fan_out.max_retries.",
                node_id=node_id,
            )

    def _resolve_fanout_budget(self, fanout: dict[str, Any]) -> tuple[float, float, int] | None:
        """Resolve ``(max_cardinality, per_item_timeout, attempts)`` from fan_out.

        Returns ``None`` when a present numeric value is malformed, so the caller
        skips the advisory rather than emitting a spurious warning. Missing keys
        fall back to the connector's production defaults (imported from the
        connector module as the single source of truth).
        """
        if "max_cardinality" in fanout:
            max_cardinality = _as_positive_number(fanout.get("max_cardinality"))
            if max_cardinality is None:
                return None
        else:
            from modulo.connectors.rest import _DEFAULT_MAX_FANOUT_CARDINALITY

            max_cardinality = float(_DEFAULT_MAX_FANOUT_CARDINALITY)
        if "per_item_timeout" in fanout:
            per_item_timeout = _as_positive_number(fanout.get("per_item_timeout"))
            if per_item_timeout is None:
                return None
        else:
            from modulo.connectors.rest import _DEFAULT_TIMEOUT

            per_item_timeout = _DEFAULT_TIMEOUT
        max_retries_raw = fanout.get("max_retries")
        if isinstance(max_retries_raw, int) and not isinstance(max_retries_raw, bool) and max_retries_raw >= 0:
            attempts = max_retries_raw + 1
        else:
            attempts = 3
        return (max_cardinality, per_item_timeout, attempts)

    # ------------------------------------------------------------------
    # Pipeline retry_policy
    # ------------------------------------------------------------------

    @staticmethod
    def check_retry_policy(policy: Any, result: ValidationResult) -> None:
        """Validate a pipeline's ``retry_policy``, emitting an ERROR when malformed.

        Valid shape: ``{"on": ["stall"|"timeout"|"failure"|"eval_failed"], "max_retries": 0-5}``.
        ``None``/``{}`` (no policy) passes. A malformed policy would silently
        disable retries at run time, so it is surfaced as a hard error here.
        """
        if policy is None or policy == {}:
            return
        if not isinstance(policy, dict):
            result.error(
                "RETRY_POLICY_MALFORMED",
                "retry_policy must be an object like "
                "{'on': ['stall','timeout','failure','eval_failed'], 'max_retries': 0-5}",
            )
            return
        events = policy.get("on", [])
        if not isinstance(events, list) or any(not isinstance(e, str) for e in events):
            result.error(
                "RETRY_POLICY_MALFORMED",
                "retry_policy 'on' must be a list of strings from ['stall','timeout','failure','eval_failed']",
            )
        else:
            unknown = set(events) - _RETRY_POLICY_EVENTS
            if unknown:
                result.error(
                    "RETRY_POLICY_MALFORMED",
                    f"retry_policy 'on' contains unknown values {sorted(unknown)}; "
                    "allowed values are ['stall','timeout','failure','eval_failed']",
                )
        max_retries = policy.get("max_retries", 0)
        if not _is_valid_retry_budget(max_retries):
            result.error(
                "RETRY_POLICY_MALFORMED",
                "retry_policy 'max_retries' must be an integer between 0 and 5",
            )

    @staticmethod
    def check_retry_policy_schedule(policy: Any, result: ValidationResult) -> None:
        """Validate the OPTIONAL run-level ``backoff_schedule`` key (FAR-525).

        DISTINCT from :meth:`check_retry_policy` (which owns the core
        ``on``/``max_retries`` shape): a malformed schedule never disables
        retries, so it gets its own issue code (``RETRY_POLICY_SCHEDULE_MALFORMED``).
        Severity is a caller decision: ERROR at the write sites (API + import),
        WARNING at run start (the runtime resolver fail-opens to the hardcoded
        default schedule, so a faulting schedule must not block the run).

        Valid shape: ``{"delay_seconds": 1-300, "multiplier": 1.0-10.0}`` (multiplier
        optional, default 2.0). ``backoff_schedule`` ``None``/``{}``/absent (or a
        non-dict policy) is "no schedule" and passes.
        """
        # Lazy import (see _check_retry): retry_compensation lazy-loads to
        # avoid the executor <-> graph_validator import cycle.
        from modulo.core.pipeline_engine import retry_compensation as rc

        if not isinstance(policy, dict):
            return
        schedule = policy.get("backoff_schedule")
        if schedule is None or schedule == {}:
            return
        if not isinstance(schedule, dict):
            result.error(
                "RETRY_POLICY_SCHEDULE_MALFORMED",
                "retry_policy 'backoff_schedule' must be an object like "
                "{'delay_seconds': 1-300, 'multiplier': 1.0-10.0}",
            )
            return
        unknown = set(schedule) - rc.RETRY_SCHEDULE_ALLOWED_KEYS
        if unknown:
            result.error(
                "RETRY_POLICY_SCHEDULE_MALFORMED",
                f"retry_policy 'backoff_schedule' contains unknown keys {sorted(str(k) for k in unknown)}; "
                f"allowed keys are {sorted(rc.RETRY_SCHEDULE_ALLOWED_KEYS)}",
            )

        # A JSON integer literal with more digits than float can represent
        # (e.g. 10**400) parses to an arbitrary-precision Python int whose
        # float() conversion raises OverflowError BEFORE the range comparison
        # — contain it so a huge int lands in the same malformed bucket as any
        # other bound fault (never a 500 / hard abort).
        def _as_float(value: Any) -> float | None:
            try:
                return float(value)
            except OverflowError:
                return None

        delay = schedule.get("delay_seconds")
        if delay is None:
            result.error(
                "RETRY_POLICY_SCHEDULE_MALFORMED",
                "retry_policy 'backoff_schedule' must include 'delay_seconds' "
                f"(integer seconds, {rc.RETRY_SCHEDULE_MIN_DELAY_SECONDS}-{rc.RETRY_SCHEDULE_MAX_DELAY_SECONDS})",
            )
        else:
            delay_f = None if isinstance(delay, bool) or not isinstance(delay, (int, float)) else _as_float(delay)
            if delay_f is None or not (
                rc.RETRY_SCHEDULE_MIN_DELAY_SECONDS <= delay_f <= rc.RETRY_SCHEDULE_MAX_DELAY_SECONDS
                and delay_f == int(delay_f)
            ):
                result.error(
                    "RETRY_POLICY_SCHEDULE_MALFORMED",
                    "retry_policy 'backoff_schedule' 'delay_seconds' must be an integer between "
                    f"{rc.RETRY_SCHEDULE_MIN_DELAY_SECONDS} and {rc.RETRY_SCHEDULE_MAX_DELAY_SECONDS}",
                )
        if "multiplier" in schedule:
            mult = schedule["multiplier"]
            mult_f = None if isinstance(mult, bool) or not isinstance(mult, (int, float)) else _as_float(mult)
            if mult_f is None or not rc.RETRY_SCHEDULE_MIN_MULTIPLIER <= mult_f <= rc.RETRY_SCHEDULE_MAX_MULTIPLIER:
                result.error(
                    "RETRY_POLICY_SCHEDULE_MALFORMED",
                    "retry_policy 'backoff_schedule' 'multiplier' must be a number between "
                    f"{rc.RETRY_SCHEDULE_MIN_MULTIPLIER} and {rc.RETRY_SCHEDULE_MAX_MULTIPLIER}",
                )

    # ------------------------------------------------------------------
    # Edge validation
    # ------------------------------------------------------------------

    @staticmethod
    def _check_edges(graph_json: dict[str, Any], result: ValidationResult) -> None:
        """Validate edge-related graph configuration."""
        nodes = graph_json.get("nodes", [])
        edges = graph_json.get("edges", [])

        if nodes and not edges:
            result.warning(
                "GRAPH_NO_EDGES",
                f"Graph has {len(nodes)} nodes but no edges. Single-node pipeline? "
                "If the graph should have flow connections, add edges between nodes.",
            )

        # Check for duplicate node IDs (belt-and-suspenders with Pydantic/MCP layer).
        seen_ids: set[str] = set()
        for n in nodes:
            nid = n.get("id")
            if nid is None:
                continue
            nid_str = str(nid)
            if nid_str in seen_ids:
                result.error(
                    "GRAPH_DUPLICATE_NODE_ID",
                    f"Duplicate node ID '{nid_str}' found in graph",
                    node_id=nid_str,
                )
            seen_ids.add(nid_str)

        # Check node ID format (warn on non-UUID-like values).
        _uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
        for n in nodes:
            nid = n.get("id")
            if nid is None:
                continue
            nid_str = str(nid)
            if not _uuid_re.match(nid_str):
                result.warning(
                    "GRAPH_NODE_ID_FORMAT",
                    f"Node ID '{nid_str}' does not look like a standard UUID format",
                    node_id=nid_str,
                )

    # ------------------------------------------------------------------
    # Port-addressed typed state (FAR-416 / F1)
    # ------------------------------------------------------------------

    def _check_ports(self, graph_json: dict[str, Any], result: ValidationResult) -> None:
        """Compile-time port rules: fan-in safety + port-type validation.

        Delegates to :func:`validate_port_topology`. Fully-legacy graphs (no
        explicit port metadata) keep their backward-compatible behaviour —
        the rule is lenient so existing flat-dict pipelines compile identically.
        """
        # Lazy import: importing the submodule would otherwise trigger
        # modulo.core.pipeline_engine.__init__ (which imports the executor, which
        # imports this package) and create a circular-import deadlock at
        # module-load time. At call time the package is already initialised.
        from modulo.core.pipeline_engine.port_resolver import validate_port_topology

        validate_port_topology(graph_json, result)

    # ------------------------------------------------------------------
    # Parallel fan-out / run_context writes (FAR-171)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_parallel_run_context_writes(
        graph_json: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """Flag same-key parallel context-setter writes to ``run_context`` (§8.18).

        When a source node has MULTIPLE normal outgoing edges (a parallel
        fan-out, compiled to native LangGraph parallel edges) and two or more of
        its direct downstream branches are context-setter nodes (``role ==
        "context_setter"``), their writes to ``run_context`` are last-write-wins
        and order-dependent: whichever branch's reducer application lands last
        (superstep completion order) wins. The outcome is deterministic for a
        given run but not author-controllable, so it is flagged as a pipeline
        validation WARNING at save time.

        When a branch declares the keys it writes (``run_context_writes`` on the
        node def), only overlapping keys trigger the warning — parallel writes
        to DISJOINT keys are safe (per-key merge). When the written keys are
        unknown (the common case — the field is not persisted on node defs),
        any two parallel context-setter branches are warned conservatively.

        Not a fan-out (skipped): sources with conditional edges, llm routing,
        or loop edges — those compile to single-target routers, so no parallel
        context-setter writes exist.
        """
        nodes = graph_json.get("nodes", [])
        edges = graph_json.get("edges", [])
        if not nodes or not edges:
            return

        nodes_by_id: dict[str, dict[str, Any]] = {}
        for n in nodes:
            nid = n.get("id")
            if nid is not None:
                nodes_by_id[str(nid)] = n

        loop_sources, by_source = _collect_parallel_fanout_candidates(edges)

        for source, src_edges in by_source.items():
            src_node = nodes_by_id.get(source, {})
            if src_node.get("routing_mode") == "llm":
                continue
            # A source with ANY loop edge routes ALL its outgoing edges through
            # the loop counter (single target, graph_cache.build_graph_from_json),
            # so it is never a parallel fan-out.
            if source in loop_sources:
                continue
            # Any conditional edge => ALL outgoing edges go through the router
            # (single target chosen), so this source is NOT a parallel fan-out.
            if any(_edge_type(e) == "conditional" for e in src_edges):
                continue
            normal = [e for e in src_edges if _edge_type(e) != "conditional"]
            if len(normal) <= 1:
                continue

            setters = _collect_context_setter_targets(normal, nodes_by_id)
            if len(setters) < 2:
                continue

            detail = _parallel_write_detail(setters)
            if detail is None:
                continue

            result.warning(
                "PARALLEL_RUN_CONTEXT_WRITE",
                f"Source '{source}' fans out to multiple context-setter branches; "
                f"{detail}. run_context writes are last-write-wins and order-dependent "
                "(the branch that completes last wins).",
                node_id=source,
            )


def _edge_type(edge: dict[str, Any]) -> str:
    raw = edge.get("type") if edge.get("type") is not None else edge.get("edge_type")
    return str(raw or "")


def _collect_parallel_fanout_candidates(
    edges: list[dict[str, Any]],
) -> tuple[set[str], dict[str, list[dict[str, Any]]]]:
    """Group normal outgoing edges by source; track loop-edge sources separately."""
    loop_sources: set[str] = set()
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in edges:
        etype = _edge_type(e)
        if etype == "loop":
            src = e.get("source", e.get("source_node_id"))
            if src is not None:
                loop_sources.add(str(src))
            continue
        if etype in ("reject", "kickback"):
            continue
        src = e.get("source", e.get("source_node_id"))
        if src is None:
            continue
        by_source[str(src)].append(e)
    return loop_sources, by_source


def _collect_context_setter_targets(
    normal: list[dict[str, Any]],
    nodes_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve the normal-edge target nodes that are context-setters."""
    target_nodes: list[dict[str, Any]] = []
    for e in normal:
        tgt = e.get("target", e.get("target_node_id"))
        if tgt is not None:
            tnode = nodes_by_id.get(str(tgt))
            if tnode is not None:
                target_nodes.append(tnode)
    return [t for t in target_nodes if t.get("role") == "context_setter"]


def _parallel_write_detail(setters: list[dict[str, Any]]) -> str | None:
    """Describe the overlapping run_context keys for parallel setters.

    Returns ``None`` when the setters' declared write keys are disjoint (safe
    per-key merge — no warning).
    """
    key_sets: list[set[str] | None] = []
    for n in setters:
        raw = n.get("run_context_writes")
        if isinstance(raw, list) and raw:
            key_sets.append({str(k) for k in raw})
        else:
            key_sets.append(None)
    if any(ks is None for ks in key_sets):
        return "parallel branches are both context-setters (written keys unknown)"
    common = set.intersection(*(ks or set() for ks in key_sets))
    if not common:
        return None
    return f"parallel branches write the same run_context keys: {sorted(common)}"
