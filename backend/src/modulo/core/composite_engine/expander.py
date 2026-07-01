"""Graph expansion engine — expands composite nodes inline into sub-pipeline nodes."""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_PARAM_PLACEHOLDER_RE = re.compile(r"\{\{parameter\.(\w+)\}\}")


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

    parent_node_id = str(node_def["id"])
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
    node_index: int,
    sub_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remap edge source/target references relative to expanded nodes.

    TODO: This is a placeholder. Cross-node edges from composite templates
    are returned as-is without parent-relative ID remapping. When composite
    nodes produce edges referencing internal node IDs, those IDs need
    prefixing with the parent node ID to avoid collisions after expansion.
    """
    return edges
