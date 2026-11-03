"""Unit tests for the shared System Map revision graph validator."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from modulo.core.system_map_validation import validate_system_map_graph_references


@dataclass(frozen=True)
class _Node:
    node_key: str
    node_type: str = "pipeline"


@dataclass(frozen=True)
class _Relationship:
    source_node_key: str
    target_node_key: str
    relationship_type: str = "flow"


@dataclass(frozen=True)
class _LoopConfig:
    node_key: str


def test_accepts_a_consistent_graph() -> None:
    result = validate_system_map_graph_references(
        nodes=[_Node("a"), _Node("loop-1", "improvement_loop")],
        relationships=[_Relationship("a", "loop-1")],
        loop_configs=[_LoopConfig("loop-1")],
    )

    # The contract is silence: a consistent graph validates and returns None.
    assert result is None


def test_rejects_duplicate_node_keys() -> None:
    with pytest.raises(ValueError, match="node_key values must be unique"):
        validate_system_map_graph_references(
            nodes=[_Node("a"), _Node("a")],
            relationships=[],
            loop_configs=[],
        )


def test_rejects_duplicate_relationships() -> None:
    relationship = _Relationship("a", "b")
    with pytest.raises(ValueError, match="relationships must be unique"):
        validate_system_map_graph_references(
            nodes=[_Node("a"), _Node("b")],
            relationships=[relationship, relationship],
            loop_configs=[],
        )


def test_rejects_relationship_to_missing_node() -> None:
    with pytest.raises(ValueError, match="relationships reference missing nodes: ghost"):
        validate_system_map_graph_references(
            nodes=[_Node("a")],
            relationships=[_Relationship("a", "ghost")],
            loop_configs=[],
        )


def test_rejects_duplicate_loop_configs() -> None:
    loop = _LoopConfig("loop-1")
    with pytest.raises(ValueError, match="loop_configs must be unique"):
        validate_system_map_graph_references(
            nodes=[_Node("loop-1", "improvement_loop")],
            relationships=[],
            loop_configs=[loop, loop],
        )


def test_rejects_loop_config_for_missing_node() -> None:
    with pytest.raises(ValueError, match="loop_configs reference missing nodes: ghost"):
        validate_system_map_graph_references(
            nodes=[_Node("a")],
            relationships=[],
            loop_configs=[_LoopConfig("ghost")],
        )


def test_rejects_loop_config_on_non_loop_node() -> None:
    with pytest.raises(ValueError, match="loop_configs require improvement_loop nodes: a"):
        validate_system_map_graph_references(
            nodes=[_Node("a")],
            relationships=[],
            loop_configs=[_LoopConfig("a")],
        )
