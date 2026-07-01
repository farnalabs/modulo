"""Graph expansion engine — expands composite nodes inline into sub-pipeline nodes."""

import logging
import re
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from modulo.core.composite_engine.composite_binding import (
    CompositeValidationError,
    OutputValidation,
    ValidationResult,
)
from modulo.core.composite_engine.schema_mapping import apply_field_mapping

logger = logging.getLogger(__name__)

_PARAM_PLACEHOLDER_RE = re.compile(r"\{\{parameter\.(\w+)\}\}")


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
        config = eval_def.config
        match eval_def.type:
            case "regex":
                pattern = config.get("pattern", "")
                if not isinstance(pattern, str):
                    failures.append(f"Eval '{eval_def.name}': 'pattern' must be a string")
                    continue
                if not pattern:
                    failures.append(f"Eval '{eval_def.name}': missing 'pattern' in config")
                    continue
                field = config.get("field", "")
                if not isinstance(field, str):
                    failures.append(f"Eval '{eval_def.name}': 'field' must be a string")
                    continue
                if not field:
                    failures.append(f"Eval '{eval_def.name}': missing 'field' in config")
                    continue
                raw = mapped_output.get(field)
                value = "" if raw is None else str(raw)
                try:
                    flags = 0
                    flags_str = config.get("flags", "")
                    if "i" in flags_str:
                        flags |= re.IGNORECASE
                    if not re.search(pattern, value, flags):
                        failures.append(
                            f"Eval '{eval_def.name}': regex /{pattern}/ did not match field '{field}'"
                        )
                except re.error as exc:
                    failures.append(f"Eval '{eval_def.name}': regex error: {exc}")

            case "json_schema":
                schema = config.get("schema", {})
                if not schema:
                    failures.append(f"Eval '{eval_def.name}': missing 'schema' in config")
                    continue
                field = config.get("field", "")
                data = mapped_output.get(field, mapped_output) if field else mapped_output
                try:
                    jsonschema.validate(data, schema)
                except jsonschema.ValidationError as exc:
                    failures.append(
                        f"Eval '{eval_def.name}': JSON Schema validation failed: {exc.message}"
                    )

            case "llm_judge":
                if llm_judge_callable is None:
                    failures.append(
                        f"Eval '{eval_def.name}': llm_judge requires a callable but none provided"
                    )
                    continue
                try:
                    raw = llm_judge_callable(mapped_output, config)
                    if not raw.get("passed"):
                        detail = raw.get("detail", "llm_judge evaluated as failed")
                        failures.append(f"Eval '{eval_def.name}': {detail}")
                except Exception as exc:
                    failures.append(f"Eval '{eval_def.name}': llm_judge raised: {exc}")

            case _:
                raise ValueError(f"Unknown eval type for output validation: {eval_def.type}")

    return ValidationResult(passed=len(failures) == 0, failures=failures)


def execute_composite_with_retry(
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
    retry_count = 0

    while True:
        expand_composite_node(node_def, composite_template, parameter_values)

        output_mapping = node_def.get("composite_output_mapping")
        mapped_output = apply_field_mapping(input_payload, output_mapping)

        if not output_validation.eval_definitions:
            return mapped_output

        result = run_output_validation(mapped_output, output_validation, llm_judge_callable)

        if result.passed:
            result.retry_count = retry_count
            return mapped_output

        retry_eligible_failures: list[str] = []
        blocking_failures: list[str] = []
        for eval_def in output_validation.eval_definitions:
            eval_failures = [
                f for f in result.failures if f.startswith(f"Eval '{eval_def.name}':")
            ]
            if eval_failures:
                if eval_def.failure_behaviour == "block":
                    blocking_failures.extend(eval_failures)
                elif eval_def.failure_behaviour == "retry":
                    retry_eligible_failures.extend(eval_failures)
                else:
                    logger.warning(
                        "Composite output validation warn: %s",
                        "; ".join(eval_failures),
                    )

        if blocking_failures:
            raise CompositeValidationError(blocking_failures, retry_count)

        if not retry_eligible_failures:
            result.retry_count = retry_count
            return mapped_output

        if retry_count >= max_retries:
            raise CompositeValidationError(result.failures, retry_count)

        retry_count += 1
        logger.info(
            "Composite output validation retry %d/%d — %d failure(s)",
            retry_count,
            max_retries,
            len(result.failures),
        )


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
