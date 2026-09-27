"""Unit tests for ``_resolved_node_timeouts`` (FAR-369 node-deadline watchdog).

Regression guard: a graph node persisted with an explicit
``"timeout_seconds": null`` (the API model defaults the field to ``None`` and
serialises the key) must fall back to the pipeline-level default. Resolving it
with ``dict.get(key, default)`` returns the persisted ``None`` and ``int(None)``
raises a TypeError that fails the whole run before any node starts — observed on
staging when an API-authored manual-node graph was triggered.
"""

from modulo.core.pipeline_engine.executor import _resolved_node_timeouts


def test_explicit_node_timeout_is_honoured():
    graph = {"nodes": [{"id": "n1", "timeout_seconds": 90}]}
    assert _resolved_node_timeouts(graph, 300) == {"n1": 90}


def test_absent_node_timeout_falls_back_to_default():
    graph = {"nodes": [{"id": "n1", "node_type": "manual"}]}
    assert _resolved_node_timeouts(graph, 300) == {"n1": 300}


def test_explicit_null_node_timeout_falls_back_to_default():
    """The regression: a serialised ``null`` is NOT the same as an absent key."""
    graph = {"nodes": [{"id": "n1", "node_type": "manual", "timeout_seconds": None}]}
    assert _resolved_node_timeouts(graph, 300) == {"n1": 300}


def test_mixed_nodes_resolve_independently():
    graph = {
        "nodes": [
            {"id": "a", "timeout_seconds": 120},
            {"id": "b", "timeout_seconds": None},
            {"id": "c"},
        ]
    }
    assert _resolved_node_timeouts(graph, 300) == {"a": 120, "b": 300, "c": 300}


def test_empty_and_none_graphs_resolve_to_empty():
    assert _resolved_node_timeouts({"nodes": []}, 300) == {}
    assert _resolved_node_timeouts(None, 300) == {}


def test_nodes_without_id_are_skipped():
    graph = {"nodes": [{"timeout_seconds": 60}, {"id": "n1", "timeout_seconds": 60}]}
    assert _resolved_node_timeouts(graph, 300) == {"n1": 60}
