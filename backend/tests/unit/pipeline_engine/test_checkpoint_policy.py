"""Unit tests for FAR-432 checkpoint persistence policy.

Covering:
  - ``_graph_is_interactive`` — the DERIVED persist decision: batch pipelines
    (no HITL gate / no manual node) are NOT checkpointed; interactive ones are.
  - ``_trim_checkpoint_channels`` — bounding the voluminous ``messages`` /
    ``__root__`` channel so a long conversation is not re-dumped whole at
    every superstep.
"""

from typing import Any

from modulo.core.pipeline_engine.executor import _graph_is_interactive
from modulo.core.pipeline_engine.modulo_saver import (
    _CHECKPOINT_MESSAGE_TRIM_TAIL,
    _trim_checkpoint_channels,
)

# ---------------------------------------------------------------------------
# _graph_is_interactive
# ---------------------------------------------------------------------------


def test_graph_is_interactive_batch_pipeline_is_false():
    """Pure agent/connector/sandbox graph (no HITL) must NOT checkpoint."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "a", "node_type": "agent"},
            {"id": "b", "node_type": "connector"},
            {"id": "c", "node_type": "sandbox_agent"},
        ],
        "edges": [{"source": "a", "target": "b", "type": "normal"}],
    }
    assert _graph_is_interactive(graph) is False


def test_graph_is_interactive_hitl_gate_edge_is_true():
    """An edge carrying ``hitl_gate_config`` must be checkpointed."""
    graph: dict[str, Any] = {
        "nodes": [{"id": "a", "node_type": "agent"}, {"id": "b", "node_type": "agent"}],
        "edges": [
            {
                "source": "a",
                "target": "b",
                "type": "normal",
                "hitl_gate_config": {"human_only": True},
            }
        ],
    }
    assert _graph_is_interactive(graph) is True


def test_graph_is_interactive_manual_node_is_true():
    """A ``manual`` input node interrupts for human output → must checkpoint."""
    graph: dict[str, Any] = {
        "nodes": [{"id": "a", "node_type": "manual"}],
        "edges": [],
    }
    assert _graph_is_interactive(graph) is True


def test_graph_is_interactive_unknown_graph_is_conservative():
    """An uninspectable graph is treated as interactive (never break resume)."""
    assert _graph_is_interactive(None) is True
    assert _graph_is_interactive("not-a-dict") is True


def test_graph_is_interactive_malformed_entries_are_safe():
    """Nodes/edges that are not dicts are ignored, not raised on."""
    graph: dict[str, Any] = {
        "nodes": ["junk", {"id": "a", "node_type": "agent"}],
        "edges": [None, "junk"],
    }
    assert _graph_is_interactive(graph) is False


# ---------------------------------------------------------------------------
# _trim_checkpoint_channels
# ---------------------------------------------------------------------------


def _messages(n: int) -> list[dict[str, Any]]:
    return [{"id": f"m{i}", "type": "human"} for i in range(n)]


def test_trim_truncates_long_messages_channel():
    """A long top-level ``messages`` channel is trimmed to the tail."""
    long_messages = _messages(_CHECKPOINT_MESSAGE_TRIM_TAIL + 50)
    checkpoint: dict[str, Any] = {"channel_values": {"messages": long_messages}}
    result = _trim_checkpoint_channels(checkpoint)
    assert len(result["channel_values"]["messages"]) == _CHECKPOINT_MESSAGE_TRIM_TAIL
    # The tail (not the head) survives.
    assert result["channel_values"]["messages"][-1]["id"] == f"m{len(long_messages) - 1}"


def test_trim_truncates_messages_nested_in_root_channel():
    """A ``messages`` list nested under the ``__root__`` channel is trimmed."""
    long_messages = _messages(_CHECKPOINT_MESSAGE_TRIM_TAIL + 25)
    checkpoint: dict[str, Any] = {
        "channel_values": {"__root__": {"messages": long_messages, "run_context": {"input": {"x": 1}}}}
    }
    result = _trim_checkpoint_channels(checkpoint)
    assert len(result["channel_values"]["__root__"]["messages"]) == _CHECKPOINT_MESSAGE_TRIM_TAIL
    # Non-conversational state under __root__ is preserved byte-for-byte.
    assert result["channel_values"]["__root__"]["run_context"] == {"input": {"x": 1}}


def test_trim_keeps_short_or_absent_channels_untouched():
    """Messages at/below the bound, or absent, are left entirely unmodified."""
    short_messages = _messages(3)
    checkpoint: dict[str, Any] = {
        "channel_values": {
            "messages": short_messages,
            "artifacts": [{"node_id": "a"}],
        }
    }
    result = _trim_checkpoint_channels(checkpoint)
    assert result["channel_values"]["messages"] is short_messages
    assert result["channel_values"]["artifacts"] == [{"node_id": "a"}]


def test_trim_preserves_non_conversational_channels():
    """A checkpoint with no conversational channel is returned unchanged."""
    checkpoint: dict[str, Any] = {
        "channel_values": {"run_context": {"cancelled": False}, "artifacts": [{"node_id": "a"}]}
    }
    result = _trim_checkpoint_channels(checkpoint)
    assert result is checkpoint
    assert result["channel_values"]["run_context"] == {"cancelled": False}
