"""Unit tests for graph_cache HITL graph building.

Only tests unique to the HITL code path live here. Tests that are pure
graph-cache logic (empty nodes, cycle detection, cache/evict) are in
test_graph_cache.py to avoid duplication.
"""

from unittest.mock import MagicMock, patch


def test_build_graph_with_nodes_succeeds() -> None:
    from modulo.core.pipeline_engine.graph_cache import build_graph_from_json

    graph_json = {
        "nodes": [
            {"id": "node-a", "role": "context_setter"},
            {"id": "node-b", "role": None},
        ],
        "edges": [
            {"source": "node-a", "target": "node-b"},
        ],
    }

    with patch("modulo.core.pipeline_engine.graph_cache.make_node_fn") as mock_make:
        mock_make.return_value = MagicMock()
        compiled = build_graph_from_json(graph_json)
        assert compiled is not None

    assert mock_make.call_count == 2
