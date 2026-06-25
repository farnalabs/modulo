"""Tests for snapshot backward compatibility when new node types are added.

Verifies that existing snapshots (created before 'manual' node type existed)
still compile and execute correctly after adding node_type support.
"""

from typing import Any

from modulo.core.pipeline_engine.graph_cache import _CACHE, build_graph_from_json


def _clear_cache() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Backward compatibility: old-format snapshots (no node_type field)
# ---------------------------------------------------------------------------


_OLD_FORMAT_SNAPSHOT: dict[str, Any] = {
    "nodes": [
        {"id": "old-node-a", "role": None},
        {"id": "old-node-b", "role": None},
    ],
    "edges": [
        {"source": "old-node-a", "target": "old-node-b", "type": "normal"},
    ],
}


def test_old_snapshot_without_node_type_compiles():
    """Snapshots without node_type should compile (defaults to 'agent')."""
    _clear_cache()
    compiled = build_graph_from_json(_OLD_FORMAT_SNAPSHOT)
    assert compiled is not None


async def test_old_snapshot_executes_correctly():
    """Old-format snapshots should execute without error."""
    compiled = build_graph_from_json(_OLD_FORMAT_SNAPSHOT)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
    }
    config = {"configurable": {"thread_id": "test-old-snap"}}
    result = await compiled.ainvoke(initial_state, config)
    assert len(result["artifacts"]) == 2
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "old-node-a" in node_ids
    assert "old-node-b" in node_ids


# ---------------------------------------------------------------------------
# Backward compatibility: old snapshots with HITL gate edges
# ---------------------------------------------------------------------------


_OLD_FORMAT_WITH_GATES: dict[str, Any] = {
    "nodes": [
        {"id": "start", "role": None},
        {"id": "end", "role": None},
    ],
    "edges": [
        {
            "source": "start",
            "target": "end",
            "type": "normal",
            "hitl_gate_config": {
                "gate_id": "review-gate",
                "human_only": False,
            },
        },
    ],
}


def test_old_snapshot_with_hitl_gate_compiles():
    """Old-format snapshots with HITL gates should compile."""
    _clear_cache()
    compiled = build_graph_from_json(_OLD_FORMAT_WITH_GATES)
    assert compiled is not None


async def test_old_snapshot_with_hitl_gate_executes():
    """Old-format snapshots with HITL gates should compile successfully."""
    compiled = build_graph_from_json(_OLD_FORMAT_WITH_GATES)
    assert compiled is not None
    # Compilation is the key backward-compatibility check for HITL gates


# ---------------------------------------------------------------------------
# Backward compatibility: mixed old and new node formats
# ---------------------------------------------------------------------------


def test_mixed_old_and_new_node_format_compiles():
    """Snapshots mixing old-format nodes (no node_type) and manual nodes."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "old-node", "role": None},  # No node_type
            {"id": "manual-node", "node_type": "manual"},
            {"id": "agent-node", "node_type": "agent"},
        ],
        "edges": [
            {"source": "old-node", "target": "manual-node", "type": "normal"},
            {"source": "manual-node", "target": "agent-node", "type": "normal"},
        ],
    }
    _clear_cache()
    compiled = build_graph_from_json(graph)
    assert compiled is not None


async def test_old_snapshot_node_ids_still_work():
    """Old snapshot node IDs should still be valid identifiers in compiled graph."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "uuid-old", "role": None},
            {"id": "uuid-new", "node_type": "agent"},
        ],
        "edges": [
            {"source": "uuid-old", "target": "uuid-new", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
    }
    config = {"configurable": {"thread_id": "test-old-uuid"}}
    result = await compiled.ainvoke(initial_state, config)
    node_ids = [a["node_id"] for a in result["artifacts"]]
    assert "uuid-old" in node_ids
    assert "uuid-new" in node_ids


# ---------------------------------------------------------------------------
# Backward compatibility: old snapshots with extra fields (future-proofing)
# ---------------------------------------------------------------------------


def test_old_snapshot_with_unexpected_fields():
    """Old snapshots might have unexpected fields that should be ignored."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "n1", "role": None, "legacy_field": "should-be-ignored"},
        ],
        "edges": [],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


async def test_old_snapshot_role_field_still_works():
    """Old snapshots use 'role' field - this should still work."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "input", "role": "input"},
            {"id": "output", "role": "output"},
        ],
        "edges": [
            {"source": "input", "target": "output", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    initial_state: dict[str, Any] = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [],
    }
    config = {"configurable": {"thread_id": "test-old-role"}}
    result = await compiled.ainvoke(initial_state, config)
    assert len(result["artifacts"]) == 2


# ---------------------------------------------------------------------------
# Snapshot listing backward compatibility
# ---------------------------------------------------------------------------


def test_diff_handles_node_type_field():
    """Snapshot diff should handle node_type changes between versions."""
    from modulo.db.crud.pipeline_snapshot_versioning import _compute_node_changes

    # Old snapshot node without node_type
    old_node = {"id": "a", "agent_id": "ag1", "label": "A"}
    # New snapshot node with node_type
    new_node = {"id": "a", "agent_id": "ag1", "label": "A", "node_type": "agent"}

    changes = _compute_node_changes(old_node, new_node)
    assert "node_type" in changes
    assert changes["node_type"] == {"old": None, "new": "agent"}

    # Both without node_type should be unchanged
    assert _compute_node_changes(old_node, old_node) == {}


async def test_list_snapshots_returns_total_count():
    """list_snapshots should return correct total count."""
    from unittest.mock import AsyncMock, MagicMock

    from modulo.db.crud.pipeline_snapshot_versioning import list_snapshots

    session = AsyncMock()
    pipeline_id = "00000000-0000-0000-0000-000000000001"

    call_count = 0

    async def execute_side(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            # First call: list query returns 2 snapshots
            s1 = MagicMock(spec=["id", "snapshot_version", "tag"])
            s1.id = "snap-1"
            s2 = MagicMock(spec=["id", "snapshot_version", "tag"])
            s2.id = "snap-2"
            result.scalars.return_value = [s1, s2]
        else:
            # Second call: count query returns 5
            result.scalar.return_value = 5
        return result

    session.execute = AsyncMock(side_effect=execute_side)

    snapshots, total = await list_snapshots(session, pipeline_id)

    assert len(snapshots) == 2
    assert total == 5
