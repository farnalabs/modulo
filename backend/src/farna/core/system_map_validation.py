"""Shared graph-reference validation for System Map revisions.

The REST request models (``api.schemas.system_maps``) and the CLI apply
entities (``cli.apply.models``) accept the same revision graph shape, so the
reference-integrity rules live here once instead of being written twice and
drifting apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class NodeLike(Protocol):
    node_key: str
    node_type: str


class RelationshipLike(Protocol):
    source_node_key: str
    target_node_key: str
    relationship_type: str


class LoopConfigLike(Protocol):
    node_key: str


def validate_system_map_graph_references(
    nodes: Sequence[NodeLike],
    relationships: Sequence[RelationshipLike],
    loop_configs: Sequence[LoopConfigLike],
) -> None:
    """Raise ``ValueError`` when a revision graph is internally inconsistent."""
    node_keys = [node.node_key for node in nodes]
    node_key_set = set(node_keys)
    if len(node_keys) != len(node_key_set):
        raise ValueError("node_key values must be unique within a revision")
    relationship_keys = {(rel.source_node_key, rel.target_node_key, rel.relationship_type) for rel in relationships}
    if len(relationship_keys) != len(relationships):
        raise ValueError("relationships must be unique within a revision")
    missing_relationship_nodes = sorted(
        {key for rel in relationships for key in (rel.source_node_key, rel.target_node_key) if key not in node_key_set}
    )
    if missing_relationship_nodes:
        raise ValueError(f"relationships reference missing nodes: {', '.join(missing_relationship_nodes)}")
    loop_keys = [loop.node_key for loop in loop_configs]
    if len(loop_keys) != len(set(loop_keys)):
        raise ValueError("loop_configs must be unique by node_key within a revision")
    missing_loop_nodes = sorted(key for key in loop_keys if key not in node_key_set)
    if missing_loop_nodes:
        raise ValueError(f"loop_configs reference missing nodes: {', '.join(missing_loop_nodes)}")
    nodes_by_key = {node.node_key: node for node in nodes}
    wrong_loop_types = sorted(
        loop.node_key for loop in loop_configs if nodes_by_key[loop.node_key].node_type != "improvement_loop"
    )
    if wrong_loop_types:
        raise ValueError(f"loop_configs require improvement_loop nodes: {', '.join(wrong_loop_types)}")
