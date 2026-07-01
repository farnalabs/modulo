"""Schema mapping — field-level mapping between composite node inputs/outputs."""

import logging
from typing import Any

import jmespath

logger = logging.getLogger(__name__)


def apply_field_mapping(source: dict[str, Any], field_map: dict[str, Any] | None) -> dict[str, Any]:
    """Apply a field mapping from a source dict to produce a target dict.

    The mapping follows the same JMESPath-based pattern as webhook
    ``payload_mapping`` in the trigger system.

    Args:
        source: The source data dict (e.g. composite node input payload).
        field_map: A dict of ``target_field: JMESPath_expression`` entries.
            Each expression is evaluated against *source*.
            If ``None``, returns *source* unchanged (passthrough).

    Returns:
        A new dict with keys from ``field_map`` and values as the result
        of evaluating the corresponding JMESPath expression against
        ``source``.

    Example:
        >>> apply_field_mapping(
        ...     {"user": {"name": "Alice", "id": 42}},
        ...     {"user_name": "user.name", "user_id": "user.id"},
        ... )
        {"user_name": "Alice", "user_id": 42}

    Passthrough mode:
        >>> apply_field_mapping({"a": 1, "b": 2}, None)
        {"a": 1, "b": 2}
    """
    if field_map is None:
        return dict(source)

    result: dict[str, Any] = {}
    for target_key, expression in field_map.items():
        if not isinstance(expression, str):
            result[target_key] = expression
            continue
        try:
            compiled = jmespath.compile(expression)
            value = compiled.search(source)
            result[target_key] = value
        except jmespath.exceptions.JMESPathError as exc:
            logger.warning("Field mapping JMESPath error for '%s': %s", target_key, exc)
            result[target_key] = None

    return result
