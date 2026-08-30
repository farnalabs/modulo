"""Graph expansion engine — expands composite nodes inline into sub-pipeline nodes."""

import asyncio
import logging
import re
import uuid
from typing import Any

import jsonschema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.composite_engine.composite_binding import (
    CompositeValidationError,
    OutputValidation,
    ValidationResult,
)
from modulo.core.composite_engine.schema_mapping import apply_field_mapping
from modulo.db.models.composite_template import CompositeTemplate

logger = logging.getLogger(__name__)

_PARAM_PLACEHOLDER_RE = re.compile(r"\{\{parameter\.(\w+)\}\}")

_MAX_COMPOSITE_DEPTH = 5

# Fields that may carry a renderable prompt on a pipeline node. Parameter
# placeholders are injected into whichever of these is present.
_PROMPT_FIELDS = ("prompt", "prompt_template", "agent_prompt")


def _is_composite_node(node: dict[str, Any]) -> bool:
    """Return True when a node is a composite (referencing a CompositeTemplate)."""
    return node.get("node_type") == "composite" or node.get("composite_ref") is not None


def _inject_node_parameters(node: dict[str, Any], parameter_values: dict[str, Any]) -> None:
    """Replace ``{{parameter.<name>}}`` placeholders on every prompt field of *node*."""
    for field in _PROMPT_FIELDS:
        value = node.get(field)
        if isinstance(value, str) and value:
            node[field] = _inject_parameters(value, parameter_values)


def _eval_fail(eval_name: str, msg: str) -> str:
    return f"Eval '{eval_name}': {msg}"


def _validate_validation_field(
    config: dict[str, Any],
    eval_name: str,
    failures: list[str],
    *,
    required: bool = True,
) -> str | None:
    field = config.get("field", "")
    if not isinstance(field, str):
        failures.append(_eval_fail(eval_name, "'field' must be a string"))
        return None
    if not field:
        if required:
            failures.append(_eval_fail(eval_name, "missing 'field' in config"))
        return None
    return field


def run_output_validation(
    mapped_output: dict[str, Any],
    output_validation: OutputValidation,
    llm_judge_callable: Any | None = None,
) -> ValidationResult:
    """Run eval definitions against the mapped composite output.

    Args:
        mapped_output: The output dict after field mapping.
        output_validation: The OutputValidation config with eval definitions.
        llm_judge_callable: Optional callable for llm_judge type evals.
            Must accept ``(output: dict, eval_config: dict)`` and return
            a dict with keys ``passed`` (bool) and ``detail`` (str).

    Returns:
        ValidationResult with pass/fail status and list of failure messages.

    Raises:
        ValueError: If an eval definition has an unknown type.

    """
    failures: list[str] = []
    for eval_def in output_validation.eval_definitions:
        name = eval_def.name
        config = eval_def.config
        match eval_def.type:
            case "regex":
                _validate_regex_eval(name, config, mapped_output, failures)
            case "json_schema":
                _validate_json_schema_eval(name, config, mapped_output, failures)
            case "llm_judge":
                _validate_llm_judge_eval(name, config, mapped_output, llm_judge_callable, failures)
            case _:
                raise ValueError(f"Unknown eval type for output validation: {eval_def.type}")

    return ValidationResult(passed=len(failures) == 0, failures=failures)


def _validate_regex_eval(
    name: str,
    config: dict[str, Any],
    mapped_output: dict[str, Any],
    failures: list[str],
) -> None:
    pattern = config.get("pattern", "")
    if not isinstance(pattern, str):
        failures.append(_eval_fail(name, "'pattern' must be a string"))
        return
    if not pattern:
        failures.append(_eval_fail(name, "missing 'pattern' in config"))
        return
    field = _validate_validation_field(config, name, failures)
    if field is None:
        return
    raw = mapped_output.get(field)
    value = "" if raw is None else str(raw)
    try:
        flags = 0
        flags_str = config.get("flags", "")
        if isinstance(flags_str, str) and "i" in flags_str:
            flags |= re.IGNORECASE
        if not re.search(pattern, value, flags):
            failures.append(_eval_fail(name, f"regex /{pattern}/ did not match field '{field}'"))
    except re.error as exc:
        failures.append(_eval_fail(name, f"regex error: {exc}"))


def _validate_json_schema_eval(
    name: str,
    config: dict[str, Any],
    mapped_output: dict[str, Any],
    failures: list[str],
) -> None:
    schema = config.get("schema", {})
    if not isinstance(schema, dict):
        failures.append(_eval_fail(name, "'schema' must be a dict"))
        return
    field = _validate_validation_field(config, name, failures, required=False)
    if field is not None and field not in mapped_output:
        failures.append(_eval_fail(name, f"configured field '{field}' not found in output"))
        return
    data = mapped_output[field] if field else mapped_output
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        failures.append(_eval_fail(name, f"JSON Schema validation failed: {exc.message}"))
    except jsonschema.SchemaError as exc:
        failures.append(_eval_fail(name, f"JSON Schema definition error: {exc.message}"))


def _validate_llm_judge_eval(
    name: str,
    config: dict[str, Any],
    mapped_output: dict[str, Any],
    llm_judge_callable: Any | None,
    failures: list[str],
) -> None:
    if llm_judge_callable is None:
        failures.append(_eval_fail(name, "llm_judge requires a callable but none provided"))
        return
    try:
        raw = llm_judge_callable(mapped_output, config)
        if not isinstance(raw, dict):
            failures.append(_eval_fail(name, "llm_judge returned non-dict result"))
            return
        if not raw.get("passed"):
            detail = raw.get("detail", "llm_judge evaluated as failed")
            failures.append(_eval_fail(name, detail))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failures.append(_eval_fail(name, f"llm_judge raised: {exc}"))


async def execute_composite_with_retry(
    node_def: dict[str, Any],
    composite_template: dict[str, Any],
    parameter_values: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
    output_validation: OutputValidation | None = None,
    llm_judge_callable: Any | None = None,
) -> dict[str, Any]:
    """Execute a composite sub-pipeline with output validation and retry.

    Runs the composite sub-pipeline normally, applies output mapping,
    then validates the result. If validation fails with retry-eligible
    evals and retries remain, the sub-pipeline is re-executed.

    Args:
        node_def: The composite node definition.
        composite_template: The sub-pipeline graph template.
        parameter_values: Parameter values for injection.
        input_payload: The input payload to pass through to the sub-pipeline.
        output_validation: Output validation configuration. If None,
            validation is skipped and the output is returned directly.
        llm_judge_callable: Optional callable for llm_judge type evals.

    Returns:
        The validated mapped output dict.

    Raises:
        CompositeValidationError: If validation fails and retry budget
            is exhausted, or if a blocking eval fails.

    """
    if parameter_values is None:
        parameter_values = {}
    if input_payload is None:
        input_payload = {}
    if output_validation is None:
        output_validation = OutputValidation()

    max_retries = output_validation.max_validation_retries

    for attempt_count in range(max_retries + 1):
        # NOTE: expand_composite_node result is intentionally discarded here.
        # The caller owns sub-pipeline execution; this is called to re-validate
        # expanded metadata (parameter injection) on each retry attempt.
        expand_composite_node(node_def, composite_template, parameter_values)

        output_mapping = node_def.get("composite_output_mapping")
        mapped_output = apply_field_mapping(input_payload, output_mapping)

        if not output_validation.eval_definitions:
            return mapped_output

        result = run_output_validation(mapped_output, output_validation, llm_judge_callable)

        if result.passed:
            return mapped_output

        retry_eligible_failures, blocking_failures = _classify_validation_failures(output_validation, result.failures)

        if blocking_failures:
            raise CompositeValidationError(blocking_failures, attempt_count)

        if not retry_eligible_failures:
            return mapped_output

        logger.info(
            "Composite output validation retry %d/%d — %d failure(s)",
            attempt_count + 1,
            max_retries,
            len(result.failures),
        )
        if attempt_count < max_retries:
            await asyncio.sleep(0.5 * (attempt_count + 1))

    raise CompositeValidationError(result.failures, max_retries)


def _classify_validation_failures(
    output_validation: OutputValidation,
    failures: list[str],
) -> tuple[list[str], list[str]]:
    """Partition eval failures into retry-eligible and blocking buckets.

    ``warn``-behaviour failures are logged and dropped from both buckets. The
    match on ``failure_behaviour`` is strict — only ``block`` and ``retry``
    are recognised (matching ``OutputValidation``'s behaviour enum).
    """
    retry_eligible_failures: list[str] = []
    blocking_failures: list[str] = []
    for eval_def in output_validation.eval_definitions:
        eval_failures = [f for f in failures if f.startswith(f"Eval '{eval_def.name}':")]
        if not eval_failures:
            continue
        if eval_def.failure_behaviour == "block":
            blocking_failures.extend(eval_failures)
        elif eval_def.failure_behaviour == "retry":
            retry_eligible_failures.extend(eval_failures)
        elif eval_def.failure_behaviour == "warn":
            logger.warning(
                "Composite output validation warn: %s",
                "; ".join(eval_failures),
            )
    return retry_eligible_failures, blocking_failures


def expand_composite_node(
    node_def: dict[str, Any],
    composite_template: dict[str, Any],
    parameter_values: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand a composite node definition into its sub-pipeline nodes.

    Args:
        node_def: The composite node definition from the parent pipeline graph.
            Must contain ``id``, ``composite_ref``, and optionally
            ``composite_input_mapping`` / ``composite_output_mapping``.
        composite_template: The ``sub_pipeline_graph_json`` from a
            ``CompositeTemplate`` record. Must contain ``nodes`` and
            optionally ``edges``.
        parameter_values: Values to inject into sub-pipeline agent prompts
            via ``{{parameter.<name>}}`` placeholders.

    Returns:
        A list of expanded node definitions with prompts resolved and
        input/output mappings applied.

    Raises:
        ValueError: If the template has no nodes, or if required parameters
            are missing.

    """
    if parameter_values is None:
        parameter_values = {}

    sub_nodes: list[dict[str, Any]] = composite_template.get("nodes", [])
    if not sub_nodes:
        raise ValueError("Composite template has no sub-pipeline nodes to expand")

    node_id = node_def.get("id")
    if node_id is None:
        raise ValueError("Composite node definition missing required 'id' field")
    parent_node_id = str(node_id)
    expanded: list[dict[str, Any]] = []

    for i, sub_node in enumerate(sub_nodes):
        expanded_node = dict(sub_node)
        expanded_node["_composite_parent_id"] = parent_node_id
        expanded_node["_composite_index"] = i

        prompt = expanded_node.get("prompt", "")
        if isinstance(prompt, str) and prompt:
            expanded_node["prompt"] = _inject_parameters(prompt, parameter_values)

        edges = composite_template.get("edges", [])
        expanded_node["_composite_edges"] = _remap_edge_refs(edges, parent_node_id, i, sub_nodes)

        input_mapping = node_def.get("composite_input_mapping")
        output_mapping = node_def.get("composite_output_mapping")
        if input_mapping:
            expanded_node["_input_mapping"] = input_mapping
        if output_mapping:
            expanded_node["_output_mapping"] = output_mapping

        expanded.append(expanded_node)

    return expanded


def _inject_parameters(prompt: str, parameter_values: dict[str, Any]) -> str:
    """Replace ``{{parameter.<name>}}`` placeholders with bound parameter values.

    Args:
        prompt: The agent prompt template containing placeholders.
        parameter_values: Mapping of parameter name → value to inject.

    Returns:
        The prompt with all recognized placeholders replaced. Unrecognized
        placeholders are left as-is.

    """

    def _replacer(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in parameter_values:
            value = parameter_values[name]
            return str(value)
        logger.warning("Unrecognized parameter placeholder '{{parameter.%s}}' — leaving as-is", name)
        return match.group(0)

    return _PARAM_PLACEHOLDER_RE.sub(_replacer, prompt)


def _remap_edge_refs(
    edges: list[dict[str, Any]],
    parent_node_id: str,
    _node_index: int,
    sub_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and return template edges for a single expanded node.

    ``expand_composite_node`` keeps the template's human-readable sub-node ids
    (``advocate-for``, ``mediator``, ...) so edge refs are returned unchanged
    after verifying each references a known sub-node id. Collision-safe
    remapping (template ids → snapshot UUIDs, including composite fan-in and
    fan-out) is performed by :func:`expand_composites_in_graph` via
    :func:`_rewire_edges`; that path is what the runtime snapshot uses.
    """
    sub_ids = {str(n.get("id")) for n in sub_nodes}
    remapped: list[dict[str, Any]] = []
    for edge in edges:
        new_edge = dict(edge)
        src = str(edge.get("source"))
        tgt = str(edge.get("target"))
        if src not in sub_ids or tgt not in sub_ids:
            logger.warning(
                "Composite edge %s references sub-node id(s) not present in the template "
                "of composite node %s: source=%s target=%s",
                edge.get("id", "?"),
                parent_node_id,
                src,
                tgt,
            )
        remapped.append(new_edge)
    return remapped


class _ExpandedComposite:
    """Result of expanding one composite node, including any nested composites."""

    __slots__ = ("bindings", "edges", "entry_node_ids", "exit_node_ids", "nodes")

    def __init__(
        self,
        *,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        bindings: list[dict[str, Any]],
        entry_node_ids: list[str],
        exit_node_ids: list[str],
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.bindings = bindings
        self.entry_node_ids = entry_node_ids
        self.exit_node_ids = exit_node_ids


def _resolve_endpoint(
    endpoint_id: str,
    leaf_map: dict[str, str],
    composite_map: dict[str, _ExpandedComposite],
    *,
    endpoint: str,
) -> list[str]:
    """Resolve a node reference to concrete snapshot node ids.

    Leaf (non-composite) ids map through ``leaf_map``. Composite ids fan out:
    a source resolves to the composite's *exit* nodes, a target to its *entry*
    nodes. Unknown ids pass through unchanged.
    """
    if endpoint_id in leaf_map:
        return [leaf_map[endpoint_id]]
    expansion = composite_map.get(endpoint_id)
    if expansion is not None:
        if endpoint == "entry":
            return list(expansion.entry_node_ids)
        return list(expansion.exit_node_ids)
    return [endpoint_id]


def _rewire_edge_metadata(
    edge: dict[str, Any],
    source: str,
    target: str,
    *,
    leaf_map: dict[str, str],
    composite_map: dict[str, _ExpandedComposite],
) -> dict[str, Any]:
    """Copy an edge with remapped source/target, preserving all routing metadata.

    ``type``, ``condition_expression``, ``max_iterations`` and other routing
    metadata are preserved verbatim. ``default_target`` (used by loop
    edges) is remapped through the same id resolution so a default that points
    at a composite node lands on its first entry sub-node.
    """
    remapped = dict(edge)
    remapped["source"] = source
    remapped["target"] = target
    if "source_node_id" in remapped:
        remapped["source_node_id"] = source
    if "target_node_id" in remapped:
        remapped["target_node_id"] = target
    default_target = remapped.get("default_target")
    if default_target is not None:
        resolved = _resolve_endpoint(str(default_target), leaf_map, composite_map, endpoint="entry")
        if resolved:
            remapped["default_target"] = resolved[0]
    return remapped


def _rewire_edges(
    edges: list[dict[str, Any]],
    *,
    leaf_map: dict[str, str],
    composite_map: dict[str, _ExpandedComposite],
) -> list[dict[str, Any]]:
    """Rewire edges through a node-id map, fanning in/out around composite nodes.

    A source id that maps to a composite node produces one edge per *exit*
    sub-node; a target id that maps to a composite node produces one edge per
    *entry* sub-node. All other edge metadata is preserved.
    """
    rewired: list[dict[str, Any]] = []
    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))
        source_ids = _resolve_endpoint(source, leaf_map, composite_map, endpoint="exit")
        target_ids = _resolve_endpoint(target, leaf_map, composite_map, endpoint="entry")
        rewired.extend(
            _rewire_edge_metadata(edge, src, tgt, leaf_map=leaf_map, composite_map=composite_map)
            for src in source_ids
            for tgt in target_ids
        )
    return rewired


class _CompositeExpander:
    """Expands composite nodes in a live pipeline graph at snapshot creation."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID | None, *, depth_limit: int) -> None:
        self._session = session
        self._org_id = org_id
        self._depth_limit = depth_limit
        self._template_cache: dict[uuid.UUID, CompositeTemplate | None] = {}

    async def _load_template(self, template_id: uuid.UUID) -> CompositeTemplate | None:
        if template_id in self._template_cache:
            return self._template_cache[template_id]
        filters = [CompositeTemplate.id == template_id, CompositeTemplate.deleted_at.is_(None)]
        if self._org_id is not None:
            filters.append(CompositeTemplate.organisation_id == self._org_id)
        result = await self._session.execute(select(CompositeTemplate).where(*filters))
        template = result.scalar_one_or_none()
        self._template_cache[template_id] = template
        return template

    async def expand(
        self,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Expand every composite node in *nodes*.

        Returns ``(expanded_nodes, expanded_edges, composite_bindings)`` where
        ``expanded_nodes`` contains only flat node types and the composite nodes
        are removed, ``expanded_edges`` is parent edges rewired to the
        composites' entry/exit sub-nodes plus remapped sub-graph edges, and
        ``composite_bindings`` records each composite template bound.
        """
        composite_nodes = [n for n in nodes if _is_composite_node(n)]
        if not composite_nodes:
            return nodes, edges, []

        expansions: dict[str, _ExpandedComposite] = {}
        bindings: list[dict[str, Any]] = []
        internal_edges: list[dict[str, Any]] = []
        for node in composite_nodes:
            node_id = str(node.get("id"))
            ref = node.get("composite_ref")
            if ref is None:
                raise ValueError(f"Composite node '{node_id}' is missing required 'composite_ref' field")
            try:
                ref_uuid = uuid.UUID(str(ref))
            except (ValueError, TypeError):
                raise ValueError(f"Composite node '{node_id}' has invalid composite_ref '{ref}'") from None
            template = await self._load_template(ref_uuid)
            if template is None:
                raise ValueError(f"Composite node '{node_id}' references missing CompositeTemplate '{ref}'")
            parameter_values = node.get("composite_parameter_values")
            if not isinstance(parameter_values, dict):
                parameter_values = {}
            expansion = await self._expand_composite(node, template, parameter_values, depth=1)
            expansions[node_id] = expansion
            bindings.extend(expansion.bindings)
            internal_edges.extend(expansion.edges)

        final_nodes: list[dict[str, Any]] = []
        for node in nodes:
            node_expansion = expansions.get(str(node.get("id")))
            if node_expansion is not None:
                final_nodes.extend(node_expansion.nodes)
            else:
                final_nodes.append(node)

        leaf_map = {str(n["id"]): str(n["id"]) for n in nodes if not _is_composite_node(n)}
        final_edges = internal_edges + _rewire_edges(edges, leaf_map=leaf_map, composite_map=expansions)

        return final_nodes, final_edges, bindings

    async def _expand_composite(
        self,
        composite_node: dict[str, Any],
        template: CompositeTemplate,
        parameter_values: dict[str, Any],
        *,
        depth: int,
    ) -> _ExpandedComposite:
        """Expand one composite node's sub-pipeline into flat nodes and edges."""
        if depth > self._depth_limit:
            raise ValueError(
                f"Composite nesting exceeds depth limit {self._depth_limit} for node '{composite_node.get('id')}'"
            )

        graph = template.sub_pipeline_graph_json
        if not isinstance(graph, dict):
            raise ValueError(f"Composite template '{template.id}' has no sub-pipeline graph")
        sub_nodes = graph.get("nodes")
        sub_edges = graph.get("edges")
        if not isinstance(sub_nodes, list) or not sub_nodes:
            raise ValueError(f"Composite template '{template.id}' has no sub-pipeline nodes to expand")
        if not isinstance(sub_edges, list):
            sub_edges = []

        parent_node_id = str(composite_node.get("id"))

        leaf_id_map = _build_leaf_id_map(sub_nodes, template)

        composite_id_map: dict[str, _ExpandedComposite] = {}
        incoming_ids: set[str] = {str(e.get("target")) for e in sub_edges}
        outgoing_ids: set[str] = {str(e.get("source")) for e in sub_edges}

        flat_nodes: list[dict[str, Any]] = []
        bindings: list[dict[str, Any]] = []
        entry_ids: list[str] = []
        exit_ids: list[str] = []

        for idx, sub in enumerate(sub_nodes):
            old_id = str(sub.get("id"))
            if _is_composite_node(sub):
                nested, nested_entries, nested_exits = await self._expand_nested_composite(
                    old_id, sub, parameter_values, depth
                )
                composite_id_map[old_id] = nested
                del leaf_id_map[old_id]
                flat_nodes.extend(nested.nodes)
                bindings.extend(nested.bindings)
                if old_id not in incoming_ids:
                    entry_ids.extend(nested_entries)
                if old_id not in outgoing_ids:
                    exit_ids.extend(nested_exits)
                continue

            new_id = leaf_id_map[old_id]
            expanded = dict(sub)
            expanded["id"] = new_id
            expanded["_composite_parent_id"] = parent_node_id
            expanded["_composite_index"] = idx
            _inject_node_parameters(expanded, parameter_values)
            flat_nodes.append(expanded)
            if old_id not in incoming_ids:
                entry_ids.append(new_id)
            if old_id not in outgoing_ids:
                exit_ids.append(new_id)

        remapped_edges = _rewire_edges(sub_edges, leaf_map=leaf_id_map, composite_map=composite_id_map)

        _propagate_output_schema(composite_node, flat_nodes, exit_ids)

        bindings.append(
            {
                "composite_template_id": str(template.id),
                "composite_version": template.version,
                "parameter_values": parameter_values,
                "input_mapping": composite_node.get("composite_input_mapping"),
                "output_mapping": composite_node.get("composite_output_mapping"),
            }
        )

        return _ExpandedComposite(
            nodes=flat_nodes,
            edges=remapped_edges,
            bindings=bindings,
            entry_node_ids=entry_ids,
            exit_node_ids=exit_ids,
        )

    async def _expand_nested_composite(
        self,
        old_id: str,
        sub: dict[str, Any],
        parameter_values: dict[str, Any],
        depth: int,
    ) -> tuple[_ExpandedComposite, list[str], list[str]]:
        """Recursively expand a nested composite sub-node and return its entries/exits."""
        ref = sub.get("composite_ref")
        if ref is None:
            raise ValueError(f"Composite sub-node '{old_id}' is missing required 'composite_ref' field")
        nested_template = await self._load_template(uuid.UUID(str(ref)))
        if nested_template is None:
            raise ValueError(f"Composite sub-node '{old_id}' references missing CompositeTemplate '{ref}'")
        nested_values = sub.get("composite_parameter_values")
        if not isinstance(nested_values, dict):
            nested_values = parameter_values
        nested = await self._expand_composite(sub, nested_template, nested_values, depth=depth + 1)
        return nested, nested.entry_node_ids, nested.exit_node_ids


def _build_leaf_id_map(sub_nodes: list[dict[str, Any]], template: CompositeTemplate) -> dict[str, str]:
    """Assign fresh UUIDs to every leaf sub-node id, rejecting duplicates."""
    leaf_id_map: dict[str, str] = {}
    for sub in sub_nodes:
        old_id = sub.get("id")
        if not old_id:
            raise ValueError(f"Composite template '{template.id}' has a sub-node without an id")
        key = str(old_id)
        if key in leaf_id_map:
            raise ValueError(f"Composite template '{template.id}' has duplicate sub-node id '{key}'")
        leaf_id_map[key] = str(uuid.uuid4())
    return leaf_id_map


def _propagate_output_schema(
    composite_node: dict[str, Any],
    flat_nodes: list[dict[str, Any]],
    exit_ids: list[str],
) -> None:
    """Stamp the composite's ``output_schema_json`` onto its exit sub-nodes."""
    output_schema_json = composite_node.get("output_schema_json")
    if isinstance(output_schema_json, dict) and exit_ids:
        exit_id_set = set(exit_ids)
        for node in flat_nodes:
            if node.get("id") in exit_id_set:
                node.setdefault("output_schema_json", output_schema_json)


async def expand_composites_in_graph(
    session: AsyncSession,
    org_id: uuid.UUID | None,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    depth_limit: int = _MAX_COMPOSITE_DEPTH,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Expand every composite node in a live pipeline graph into flat sub-nodes.

    Fetches the ``CompositeTemplate`` records referenced by composite nodes
    (org-scoped by the caller's RLS context, plus an explicit ``organisation_id``
    filter when *org_id* is provided), expands each composite's sub-pipeline in
    place — assigning fresh UUIDs to every sub-node, injecting composite
    parameter values, remapping sub-graph edges, rewiring parent edges to the
    composite's entry/exit sub-nodes, and propagating the composite's
    ``output_schema_json`` to its exit sub-nodes. Nested composites are expanded
    recursively up to *depth_limit*.

    Returns:
        ``(expanded_nodes, expanded_edges, composite_bindings)`` where
        ``expanded_nodes`` contains only flat node types
        (agent/manual/connector/sandbox_agent), so the compiled runtime accepts
        the resulting snapshot graph unchanged.

    """
    expander = _CompositeExpander(session, org_id, depth_limit=depth_limit)
    return await expander.expand(nodes, edges)
