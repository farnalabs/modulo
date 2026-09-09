"""Category validator — validates node_category_id references on graph nodes.

Standalone function usable outside GraphValidator.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator._types import ValidationResult
from modulo.db.models.node_category import NodeCategory


async def validate_node_category(
    node: dict[str, Any],
    category_id: str | None,
    session: AsyncSession,
) -> ValidationResult:
    """Validate a single node's category reference.

    Checks:
    1. The referenced ``NodeCategory`` exists

    Returns a ``ValidationResult`` (empty = valid).
    """
    result = ValidationResult()
    node_id: str | None = node.get("id")

    if category_id is None:
        return result

    try:
        parsed = uuid.UUID(str(category_id))
    except (ValueError, TypeError):
        result.error(
            "CATEGORY_INVALID_ID",
            f"Node '{node_id}' has invalid category_id '{category_id}'",
            node_id=node_id,
        )
        return result

    row = (await session.execute(select(NodeCategory).where(NodeCategory.id == parsed))).scalar_one_or_none()

    if row is None:
        result.error(
            "CATEGORY_NOT_FOUND",
            f"Node '{node_id}' references category '{category_id}' which does not exist",
            node_id=node_id,
        )
        return result

    return result


def _group_nodes_by_category(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group nodes by their (raw, stringified) ``node_category_id``, skipping un-categorised nodes."""
    category_refs: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        cat_id = node.get("node_category_id")
        if cat_id is not None:
            category_refs.setdefault(str(cat_id), []).append(node)
    return category_refs


def _split_parsed_and_invalid(
    category_refs: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Split raw category ids into UUID-parseable and invalid groups (same node lists preserved)."""
    parsed: dict[str, list[dict[str, Any]]] = {}
    invalid: dict[str, list[dict[str, Any]]] = {}
    for raw, nodes in category_refs.items():
        try:
            uuid.UUID(raw)
            parsed[raw] = nodes
        except (ValueError, TypeError):
            invalid[raw] = nodes
    return parsed, invalid


def _report_invalid_categories(result: ValidationResult, invalid: dict[str, list[dict[str, Any]]]) -> None:
    """Emit ``CATEGORY_INVALID_ID`` for every node carrying an unparseable category id."""
    for raw, nodes in invalid.items():
        for node in nodes:
            node_id: str | None = str(node.get("id"))
            result.error(
                "CATEGORY_INVALID_ID",
                f"Node '{node_id}' has invalid category_id '{raw}'",
                node_id=node_id,
            )


async def _report_missing_categories(
    result: ValidationResult,
    parsed: dict[str, list[dict[str, Any]]],
    session: AsyncSession,
) -> None:
    """Batch-fetch the parsed category ids and emit ``CATEGORY_NOT_FOUND`` for the missing ones."""
    uuids = {uuid.UUID(raw) for raw in parsed}
    rows: Sequence[NodeCategory] = (
        (await session.execute(select(NodeCategory).where(NodeCategory.id.in_(uuids)))).scalars().all()
    )
    found: dict[str, NodeCategory] = {str(r.id): r for r in rows}

    for raw, nodes in parsed.items():
        category = found.get(raw)
        for node in nodes:
            node_id = str(node.get("id"))
            if category is None:
                result.error(
                    "CATEGORY_NOT_FOUND",
                    f"Node '{node_id}' references category '{raw}' which does not exist",
                    node_id=node_id,
                )


async def validate_node_categories(
    graph_json: dict[str, Any],
    session: AsyncSession,
) -> ValidationResult:
    """Validate all ``node_category_id`` references in the graph.

    Convenience wrapper that calls ``validate_node_category`` for every node.
    Batch-fetches all referenced categories first to avoid N+1 queries.
    """
    result = ValidationResult()
    nodes: list[dict[str, Any]] = graph_json.get("nodes", [])

    category_refs = _group_nodes_by_category(nodes)
    if not category_refs:
        return result

    parsed, invalid = _split_parsed_and_invalid(category_refs)
    _report_invalid_categories(result, invalid)

    if parsed:
        await _report_missing_categories(result, parsed, session)

    return result
