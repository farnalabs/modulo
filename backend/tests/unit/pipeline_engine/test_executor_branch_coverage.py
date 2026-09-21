"""Additional branch-coverage tests for executor.py pure functions.

Targets uncovered branches from the baseline measurement (91 BrPart).
Every test exercises the REAL function under test.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from modulo.core.pipeline_engine.executor import (
    _compute_otel_run_context,
    _connector_scope_agent_ids,
    _graph_has_script_mode,
    _graph_is_idempotent,
    _graph_is_interactive,
    _map_lg_event,
    _node_output_agent_failure,
    _node_output_has_idempotency_gate,
    _node_output_sandbox_session_lost,
    _node_output_stall_reason,
    _record_chain_end_output,
    _seed_state,
    _should_skip_retry,
    _stream_terminal_reason,
    _streamed_interrupts,
    _StreamState,
    compute_retry_aware_topology_hash,
)

# ---------------------------------------------------------------------------
# _compute_otel_run_context
# ---------------------------------------------------------------------------


def test_compute_otel_run_context_with_thread_id():
    """Thread present → returns trace_id + root span."""
    bridge = MagicMock()
    bridge.start_run_root.return_value = "root-span"
    config = {"configurable": {"thread_id": "t-123"}}
    trace_id, root_span = _compute_otel_run_context(config, bridge)
    assert trace_id is not None
    assert root_span == "root-span"
    bridge.start_run_root.assert_called_once_with("t-123")


def test_compute_otel_run_context_without_thread_id():
    """No thread → returns (None, None)."""
    bridge = MagicMock()
    config = {"configurable": {}}
    trace_id, root_span = _compute_otel_run_context(config, bridge)
    assert trace_id is None
    assert root_span is None
    bridge.start_run_root.assert_not_called()


def test_compute_otel_run_context_none_config():
    """None config → returns (None, None)."""
    bridge = MagicMock()
    trace_id, root_span = _compute_otel_run_context({}, bridge)
    assert trace_id is None
    assert root_span is None


# ---------------------------------------------------------------------------
# _record_chain_end_output — extra branches
# ---------------------------------------------------------------------------


def test_record_chain_end_output_stamp_span_only():
    """Stamp with span_id only (no run_trace_id)."""
    completed: dict[str, Any] = {}
    bridge = MagicMock()
    bridge.span_id_for_run.return_value = "span-xyz"
    result = _record_chain_end_output(completed, "n1", {"output": {"k": "v"}}, None, bridge, uuid.uuid4())
    assert result["otel_span_id"] == "span-xyz"
    assert "otel_trace_id" not in result


def test_record_chain_end_output_stamp_trace_only():
    """Stamp with trace_id only (no span_id)."""
    completed: dict[str, Any] = {}
    bridge = MagicMock()
    bridge.span_id_for_run.return_value = None
    result = _record_chain_end_output(completed, "n1", {"output": {"k": "v"}}, "trace-1", bridge, uuid.uuid4())
    assert "otel_span_id" not in result
    assert result["otel_trace_id"] == "trace-1"


# ---------------------------------------------------------------------------
# _seed_state — overrides seeding
# ---------------------------------------------------------------------------


def test_seed_state_variant_config_with_run_overrides():
    """Dict variant with _run_overrides → seeded into run_context."""
    snap = MagicMock()
    snap.run_context_defaults = {}
    snap.default_autonomy_level = None
    overrides = {"some_key": "some_val"}
    state = _seed_state(snap, {}, {"_run_overrides": overrides})
    assert state["run_context"]["_run_overrides"] == overrides


def test_seed_state_variant_config_overrides_not_dict():
    """Dict variant with non-dict _run_overrides → not seeded."""
    snap = MagicMock()
    snap.run_context_defaults = {}
    snap.default_autonomy_level = None
    state = _seed_state(snap, {}, {"_run_overrides": "not-a-dict"})
    assert "_run_overrides" not in state["run_context"]


def test_seed_state_default_autonomy_level():
    """Non-empty autonomy level → seeded."""
    snap = MagicMock()
    snap.run_context_defaults = {}
    snap.default_autonomy_level = "auto_approve"
    state = _seed_state(snap, {})
    assert state["run_context"]["_pipeline_default_autonomy"] == "auto_approve"


def test_seed_state_no_feedback_correction():
    """No _feedback_correction → not seeded."""
    snap = MagicMock()
    snap.run_context_defaults = {}
    snap.default_autonomy_level = None
    state = _seed_state(snap, {"key": "val"})
    assert "feedback_correction" not in state["run_context"]
    assert state["run_context"]["input"]["key"] == "val"


# ---------------------------------------------------------------------------
# _should_skip_retry — marker delivery done path
# ---------------------------------------------------------------------------


def test_should_skip_retry_delivery_done():
    """Marker indicates delivery_done → should skip retry."""
    markers = {
        "delivery_done:run:abc:node:n1": True,
    }
    with patch(
        "modulo.core.pipeline_engine.executor._marker_delivery_done_for_node",
        return_value=True,
    ):
        assert _should_skip_retry("n1", markers, "abc") is True


def test_should_skip_retry_no_delivery_done():
    """Marker does not indicate delivery_done → should not skip."""
    with patch(
        "modulo.core.pipeline_engine.executor._marker_delivery_done_for_node",
        return_value=False,
    ):
        assert _should_skip_retry("n1", {}, "abc") is False


# ---------------------------------------------------------------------------
# _node_output_has_idempotency_gate — extra branches
# ---------------------------------------------------------------------------


def test_idempotency_gate_no_output_json_in_inner():
    """Inner dict present but no output_json key → False."""
    assert _node_output_has_idempotency_gate({"output": {"other": "val"}}) is False


def test_idempotency_gate_output_json_not_dict():
    """output_json is a string → False."""
    assert _node_output_has_idempotency_gate({"output": {"output_json": "str"}}) is False


def test_idempotency_gate_empty_artifacts_list():
    """Empty artifacts list → False."""
    assert _node_output_has_idempotency_gate({"artifacts": []}) is False


# ---------------------------------------------------------------------------
# _node_output_stall_reason — extra branches
# ---------------------------------------------------------------------------


def test_stall_reason_non_string_reason():
    """Non-string stall_reason → None."""
    assert _node_output_stall_reason({"output": {"stall_reason": 42}}) is None


def test_stall_reason_empty_string():
    """Empty string stall_reason → None."""
    assert _node_output_stall_reason({"output": {"stall_reason": ""}}) is None


# ---------------------------------------------------------------------------
# _node_output_agent_failure — extra branches
# ---------------------------------------------------------------------------


def test_agent_failure_both_status_and_outcome_not_failed():
    """Both agent_status and agent_outcome present but neither is 'failed'."""
    output = {"output": {"agent_status": "success", "agent_outcome": "success"}}
    assert _node_output_agent_failure(output) is None


# ---------------------------------------------------------------------------
# _node_output_sandbox_session_lost — extra branches
# ---------------------------------------------------------------------------


def test_session_lost_not_true():
    """sandbox_session_lost = 'yes' (not True) → None."""
    assert _node_output_sandbox_session_lost({"output": {"sandbox_session_lost": "yes"}}) is None


def test_session_lost_with_summary():
    """Session lost with summary instead of error → returns summary."""
    output = {"output": {"sandbox_session_lost": True, "summary": "session dead"}}
    assert _node_output_sandbox_session_lost(output) == "session dead"


# ---------------------------------------------------------------------------
# _map_lg_event — extra branches
# ---------------------------------------------------------------------------


def test_map_lg_event_chain_error_non_dict_data_error():
    """on_chain_error with non-dict data → error defaults to ''."""
    result = _map_lg_event({"event": "on_chain_error", "name": "n1", "data": [1, 2]}, {"n1"})
    assert result == ("node_failed", {"node_id": "n1", "error": ""})


# ---------------------------------------------------------------------------
# _connector_scope_agent_ids — extra branches
# ---------------------------------------------------------------------------


def test_connector_scope_empty_nodes():
    """Empty nodes list → empty result."""
    assert not _connector_scope_agent_ids({"nodes": []})


def test_connector_scope_no_nodes_key():
    """No 'nodes' key → empty result."""
    assert not _connector_scope_agent_ids({})


# ---------------------------------------------------------------------------
# _graph_is_interactive — extra branches
# ---------------------------------------------------------------------------


def test_graph_interactive_skip_non_dict_edge():
    """Non-dict edge in edges list → skipped."""
    graph = {"nodes": [], "edges": ["not-a-dict"]}
    assert _graph_is_interactive(graph) is False


def test_graph_interactive_empty_edges():
    """Empty edges list → False."""
    graph = {"nodes": [], "edges": []}
    assert _graph_is_interactive(graph) is False


# ---------------------------------------------------------------------------
# _graph_has_script_mode — extra branches
# ---------------------------------------------------------------------------


def test_graph_has_script_mode_empty_mode_string():
    """Empty string mode → not script."""
    graph = {"nodes": [{"id": "a", "node_type": "sandbox_agent", "mode": ""}]}
    assert _graph_has_script_mode(graph) is False


def test_graph_has_script_mode_whitespace_script():
    """Whitespace-padded 'script' → matches after strip."""
    graph = {"nodes": [{"id": "a", "node_type": "sandbox_agent", "mode": "  script  "}]}
    assert _graph_has_script_mode(graph) is True


# ---------------------------------------------------------------------------
# _graph_is_idempotent — extra branches
# ---------------------------------------------------------------------------


def test_graph_is_idempotent_missing_nodes_key():
    """No 'nodes' key → True."""
    assert _graph_is_idempotent({"edges": []}) is True


def test_graph_is_idempotent_none_node_type():
    """Node with no idempotent key → True (defaults to True)."""
    assert _graph_is_idempotent({"nodes": [{"id": "a"}]}) is True


# ---------------------------------------------------------------------------
# _stream_terminal_reason — agent_failure with elevation disabled
# ---------------------------------------------------------------------------


def test_stream_terminal_reason_agent_failure_elevation_disabled():
    """Agent failure but elevation disabled → falls through to stall (or None)."""
    state = _StreamState(agent_failure_reason="error")
    with patch("modulo.settings.get_settings") as mock_settings:
        mock_settings.return_value.modulo_agent_failure_elevation_enabled = False
        result = _stream_terminal_reason(state, MagicMock(), uuid.uuid4())
    assert result is None


def test_stream_terminal_reason_agent_failure_elevation_exception():
    """Agent failure, settings raises → fail-open, falls through to stall."""
    state = _StreamState(agent_failure_reason="error")
    with patch("modulo.settings.get_settings", side_effect=RuntimeError("boom")):
        result = _stream_terminal_reason(state, MagicMock(), uuid.uuid4())
    assert result is None


def test_stream_terminal_reason_session_lost_with_stall():
    """session_lost + stall → stall wins (session_lost ignored)."""
    state = _StreamState(session_lost_reason="dead", stall_reason="stalled")
    result = _stream_terminal_reason(state, MagicMock(), uuid.uuid4())
    assert result[0] == "stalled"


# ---------------------------------------------------------------------------
# _streamed_interrupts — extra branches
# ---------------------------------------------------------------------------


def test_streamed_interrupts_empty_tuple():
    """Empty tuple of interrupts → empty tuple."""
    result = _streamed_interrupts({"event": "on_chain_stream", "data": {"chunk": {"__interrupt__": ()}}})
    assert result == ()


# ---------------------------------------------------------------------------
# compute_retry_aware_topology_hash — empty dict
# ---------------------------------------------------------------------------


def test_retry_aware_topology_hash_none_graph():
    """None graph_json → base hash."""
    result = compute_retry_aware_topology_hash(None, None)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _seed_state — empty payload
# ---------------------------------------------------------------------------


def test_seed_state_empty_payload():
    """Empty input payload → valid state."""
    snap = MagicMock()
    snap.run_context_defaults = {}
    snap.default_autonomy_level = None
    state = _seed_state(snap, {})
    assert state["run_context"]["cancelled"] is False
    assert not state["run_context"]["input"]
    assert not state["artifacts"]
    assert not state["_iteration_counts"]


def test_seed_state_with_context_defaults():
    """Snapshot defaults are merged into run_context."""
    snap = MagicMock()
    snap.run_context_defaults = {"custom_key": "custom_val"}
    snap.default_autonomy_level = None
    state = _seed_state(snap, {})
    assert state["run_context"]["custom_key"] == "custom_val"
