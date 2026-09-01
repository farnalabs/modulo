"""Unit tests for the per-node output split helpers (FAR-124, P0).

Covers the full ``split_node_output`` extraction table (sandbox_agent,
regular agent, connector completed + failure, HITL gate incl. the pinned
``deliver_manual`` shape, manual-node resume, recovery markers, already-pure
idempotence, pure-return-with-artifacts/output keys NOT re-split, lossless
telemetry, malformed + unknown), the legacy-safe READ accessors
(``node_return`` / ``node_telemetry`` incl. the equivalence with
``finalize._node_output_dict``), and the edge-driven type-map extension for
HITL gates.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from modulo.core.cost_controller.finalize import _node_output_dict
from modulo.core.node_output_split import (
    NODE_TYPE_GATE,
    TELEMETRY_FIELDS,
    extend_node_type_map_from_edges,
    node_return,
    node_telemetry,
    split_node_output,
)


def _sandbox_envelope(
    *,
    agent_return: Any,
    status: str = "completed",
    summary: str = "did the thing",
    wall_ms: int = 1200,
    extra_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A realistic legacy sandbox_agent envelope (mirrors node_runner)."""
    inner: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "changed_files": ["a.py"],
        "pr_url": "https://github.com/farnalabs/modulo/pull/1",
        "exit_code": 0 if status == "completed" else 1,
        "wall_clock_time_ms": wall_ms,
        "cost_estimate_usd": 0.01,
        "model_cost_usd": 0.001,
        "model_cost_raw_usd": 0.001,
        "output_json": agent_return,
        "agent_stdout": "hello",
        "agent_stderr": "",
        "stdout_length": 5,
        "stderr_length": 0,
    }
    if extra_telemetry:
        inner.update(extra_telemetry)
    outer = {
        key: value for key, value in inner.items() if key not in ("output_json", "changed_files", "pr_url", "exit_code")
    }
    return {
        "artifacts": [{"node_id": "sandbox-1", "status": status, "output": inner}],
        "output": outer,
    }


# ---------------------------------------------------------------------------
# split_node_output -- sandbox_agent
# ---------------------------------------------------------------------------


def test_split_sandbox_agent_pure_return() -> None:
    agent_return = {"summary": "agent summary", "changed_files": [], "pr_url": ""}
    envelope = _sandbox_envelope(agent_return=agent_return)
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value == agent_return
    assert telemetry["status"] == "completed"
    assert telemetry["summary"] == "did the thing"
    assert telemetry["wall_clock_time_ms"] == 1200
    assert telemetry["exit_code"] == 0
    assert telemetry["agent_stdout"] == "hello"
    assert telemetry["changed_files"] == ["a.py"]
    assert "output_json" not in telemetry


def test_split_sandbox_agent_missing_output_json_returns_none() -> None:
    envelope = _sandbox_envelope(agent_return=None)
    envelope["artifacts"][0]["output"].pop("output_json", None)
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value is None
    assert telemetry["status"] == "completed"
    assert telemetry["wall_clock_time_ms"] == 1200


def test_split_sandbox_agent_carries_reported_token_fields() -> None:
    """FAR-491: agent-reported token-usage fields ride the envelope's telemetry
    views and land in the split telemetry (they are declared in
    TELEMETRY_FIELDS and are losslessly folded either way)."""
    tokens = {
        "model_tokens_input": 1234,
        "model_tokens_output": 567,
        "model_tokens_total": 1801,
        "model_tokens_cache_read": 100,
        "model_tokens_cache_write": 8,
    }
    envelope = _sandbox_envelope(agent_return={"summary": "agent summary"}, extra_telemetry=tokens)
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value == {"summary": "agent summary"}
    for key, expected in tokens.items():
        assert telemetry[key] == expected
        assert key in TELEMETRY_FIELDS


def test_split_sandbox_agent_non_dict_output_json_returns_none() -> None:
    envelope = _sandbox_envelope(agent_return="not a dict")
    value, _telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value is None


def test_split_sandbox_agent_no_artifacts_lossless() -> None:
    # The ``{status: skipped, summary}`` skip shape written by node_runner.
    envelope = {"status": "skipped", "summary": "Skipped: missing input fields"}
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value is None
    assert telemetry["status"] == "skipped"
    assert telemetry["summary"] == "Skipped: missing input fields"


def test_split_sandbox_agent_non_dict_inner_output() -> None:
    # ``artifacts[0].output`` not a dict: no return, only the surfaced fields.
    envelope = {
        "artifacts": [{"node_id": "sandbox-1", "status": "skipped", "output": "not-a-dict"}],
        "output": {"status": "skipped"},
    }
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value is None
    assert telemetry == {"status": "skipped"}


def test_split_sandbox_agent_non_dict_outer_output_lossless() -> None:
    # A non-dict outer ``output`` is not consumed into telemetry; it is folded
    # in losslessly while the artifact's ``output_json`` stays the return.
    envelope = {
        "artifacts": [
            {
                "node_id": "sandbox-1",
                "status": "completed",
                "output": {"output_json": {"ok": 1}, "status": "completed"},
            }
        ],
        "output": "not-a-dict",
    }
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value == {"ok": 1}
    assert telemetry["status"] == "completed"
    assert telemetry["output"] == "not-a-dict"


# ---------------------------------------------------------------------------
# split_node_output -- regular agent
# ---------------------------------------------------------------------------


def test_split_agent_returns_outer_output() -> None:
    result = {"answer": 42}
    envelope = {"artifacts": [{"node_id": "a1", "status": "completed", "output": result}], "output": result}
    value, telemetry = split_node_output(envelope, "agent", None)
    assert value == result
    assert telemetry["status"] == "completed"


def test_split_agent_stub_without_output_returns_none() -> None:
    envelope = {"artifacts": [{"node_id": "a1", "status": "executed"}]}
    value, telemetry = split_node_output(envelope, "agent", None)
    assert value is None
    assert telemetry["status"] == "executed"


def test_split_agent_envelope_level_status_without_artifacts() -> None:
    # No artifact: status/summary come from the envelope top level.
    result = {"answer": 42}
    envelope = {"status": "completed", "summary": "no artifacts", "output": result}
    value, telemetry = split_node_output(envelope, "agent", None)
    assert value == result
    assert telemetry["status"] == "completed"
    assert telemetry["summary"] == "no artifacts"


def test_split_agent_envelope_status_not_overwritten_by_artifact() -> None:
    # The artifact status wins; the top-level key is already represented in
    # telemetry and is NOT folded in again (lossless fold skips it).
    result = {"answer": 42}
    envelope = {"artifacts": [{"node_id": "a1", "status": "completed"}], "status": "pending", "output": result}
    value, telemetry = split_node_output(envelope, "agent", None)
    assert value == result
    assert telemetry["status"] == "completed"


# ---------------------------------------------------------------------------
# split_node_output -- connector
# ---------------------------------------------------------------------------


def test_split_connector_completed_returns_artifact_output() -> None:
    result = {"rows": 3}
    envelope = {
        "artifacts": [{"node_id": "c1", "status": "completed", "output": result}],
        "output": result,
    }
    value, telemetry = split_node_output(envelope, "connector", None)
    assert value == result
    assert telemetry["status"] == "completed"


def test_split_connector_failure_returns_none() -> None:
    envelope = {"artifacts": [{"node_id": "c1", "status": "failed", "error": "boom"}]}
    value, telemetry = split_node_output(envelope, "connector", None)
    assert value is None
    assert telemetry == {"status": "failed", "error": "boom"}


def test_split_connector_failure_without_error() -> None:
    envelope = {"artifacts": [{"node_id": "c1", "status": "failed"}]}
    value, telemetry = split_node_output(envelope, "connector", None)
    assert value is None
    assert telemetry == {"status": "failed"}


def test_split_connector_without_artifacts_uses_envelope_output() -> None:
    result = {"rows": 3}
    envelope = {"output": result, "status": "completed"}
    value, telemetry = split_node_output(envelope, "connector", None)
    assert value == result
    assert telemetry["status"] == "completed"
    # The outer ``output`` is folded into telemetry losslessly (no artifact to
    # prove it was consumed as the return).
    assert telemetry["output"] is result


# ---------------------------------------------------------------------------
# split_node_output -- HITL gate (incl. pinned deliver_manual)
# ---------------------------------------------------------------------------


def test_split_gate_approved() -> None:
    decision = {"action": "approved"}
    envelope = {
        "artifacts": [
            {"node_id": "hitl_gate_a_b", "status": "interrupted", "result": "approved", "human_data": decision}
        ]
    }
    value, telemetry = split_node_output(envelope, NODE_TYPE_GATE, None)
    assert value == decision
    assert telemetry["status"] == "interrupted"
    assert telemetry["result"] == "approved"
    assert telemetry["human_data"] == decision


def test_split_gate_deliver_manual_pinned() -> None:
    decision = {"action": "deliver_manual", "output": {"fixed": True}}
    manual_output = {"fixed": True}
    envelope = {
        "artifacts": [
            {
                "node_id": "hitl_gate_a_b",
                "status": "interrupted",
                "result": "delivered_manual",
                "human_data": decision,
                "manual_output": manual_output,
            }
        ],
        "output": manual_output,
    }
    value, telemetry = split_node_output(envelope, NODE_TYPE_GATE, None)
    assert value == decision
    assert telemetry["status"] == "interrupted"
    assert telemetry["result"] == "delivered_manual"
    assert telemetry["human_data"] == decision
    # Lossless: the outer manual output survives in telemetry.
    assert telemetry["output"] == manual_output


def test_split_gate_detected_by_shape_without_type() -> None:
    decision = {"action": "rejected"}
    envelope = {
        "artifacts": [
            {"node_id": "hitl_gate_x_y", "status": "interrupted", "result": "rejected", "human_data": decision}
        ]
    }
    value, telemetry = split_node_output(envelope, None, None)
    assert value == decision
    assert telemetry["result"] == "rejected"


def test_split_gate_no_human_data_returns_none() -> None:
    envelope = {"artifacts": [{"node_id": "hitl_gate_a_b", "status": "auto_approved", "autonomy": "fully_autonomous"}]}
    value, telemetry = split_node_output(envelope, NODE_TYPE_GATE, None)
    assert value is None
    assert telemetry["status"] == "auto_approved"
    assert telemetry["autonomy"] == "fully_autonomous"


def test_split_gate_detected_by_interrupted_status_only() -> None:
    # No human_data/autonomy/result on the artifact: the gate is still detected
    # from the interrupted status alone.
    envelope = {"artifacts": [{"node_id": "hitl_gate_a_b", "status": "interrupted"}]}
    value, telemetry = split_node_output(envelope, None, None)
    assert value is None
    assert telemetry["status"] == "interrupted"


def test_split_gate_without_artifacts() -> None:
    # A gate envelope with no artifact list: return None, telemetry from the
    # top-level keys (lossless).
    envelope = {"status": "completed", "human_data": {"action": "approved"}}
    value, telemetry = split_node_output(envelope, NODE_TYPE_GATE, None)
    assert value is None
    assert telemetry["status"] == "completed"
    assert telemetry["human_data"] == {"action": "approved"}


# ---------------------------------------------------------------------------
# split_node_output -- manual node
# ---------------------------------------------------------------------------


def test_split_manual_node_resume() -> None:
    manual_output = {"review": "approved"}
    envelope = {
        "artifacts": [{"node_id": "m1", "status": "completed", "human_output": manual_output}],
        "manual_output": manual_output,
    }
    value, telemetry = split_node_output(envelope, "manual", None)
    assert value == manual_output
    assert telemetry["status"] == "completed"


def test_split_manual_node_falls_back_to_artifact_human_output() -> None:
    # No top-level ``manual_output``: fall back to the artifact's human_output.
    manual_output = {"review": "approved"}
    envelope = {"artifacts": [{"node_id": "m1", "status": "completed", "human_output": manual_output}]}
    value, telemetry = split_node_output(envelope, "manual", None)
    assert value == manual_output
    assert telemetry["status"] == "completed"
    assert telemetry["human_output"] == manual_output


def test_split_manual_node_partial_artifact_fields() -> None:
    # The artifact only carries ``status`` (no human_output): return None and
    # only the present keys surface into telemetry.
    envelope = {"artifacts": [{"node_id": "m1", "status": "completed"}]}
    value, telemetry = split_node_output(envelope, "manual", None)
    assert value is None
    assert telemetry == {"status": "completed"}


def test_split_manual_node_with_recovered_flag() -> None:
    # Artifacts present (so the recovery-marker branch does not intercept) and a
    # top-level ``recovered`` flag: it is kept in telemetry.
    envelope = {
        "artifacts": [{"node_id": "m1", "status": "completed"}],
        "manual_output": {"x": 1},
        "recovered": True,
    }
    value, telemetry = split_node_output(envelope, "manual", None)
    assert value == {"x": 1}
    assert telemetry["status"] == "completed"
    assert telemetry["recovered"] is True


def test_split_manual_node_without_artifacts() -> None:
    # No artifact list: return the top-level ``manual_output`` and no
    # artifact-derived fields.
    envelope = {"manual_output": {"x": 1}, "status": "completed"}
    value, telemetry = split_node_output(envelope, "manual", None)
    assert value == {"x": 1}
    assert telemetry == {"status": "completed"}


# ---------------------------------------------------------------------------
# split_node_output -- recovery markers
# ---------------------------------------------------------------------------


def test_split_recovery_recovered_returns_input() -> None:
    input_data = {"claim": "input"}
    envelope = {"input": input_data, "output": input_data, "recovered": True}
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value == input_data
    assert telemetry == {"recovered": True, "recovery_input": input_data}


def test_split_recovery_skipped_omits_return_key() -> None:
    envelope = {"input": None, "output": None, "skipped": True}
    value, telemetry = split_node_output(envelope, "agent", None)
    # The return key is OMITTED: value is None and the telemetry is the sole
    # record ({skipped: true}); the P1 writer must not write outputs_json[id].
    assert value is None
    assert telemetry == {"skipped": True}


# ---------------------------------------------------------------------------
# split_node_output -- already-pure idempotence + pure-return NOT re-split
# ---------------------------------------------------------------------------


def test_split_already_pure_idempotent_noop() -> None:
    pure_return = {"summary": "x", "data": 1}
    stored_telemetry = {"status": "completed", "wall_clock_time_ms": 10}
    value, telemetry = split_node_output(pure_return, "sandbox_agent", stored_telemetry)
    assert value is pure_return
    assert telemetry is stored_telemetry


def test_split_pure_return_with_artifacts_keys_not_resplit() -> None:
    # A pure agent return may legitimately contain "artifacts" / "output" keys.
    # Because a telemetry entry exists, it must be returned verbatim -- NEVER
    # structure-sniffed / re-split.
    pure_return = {"artifacts": [{"node_id": "n", "status": "completed", "output": {"v": 1}}], "output": {"v": 1}}
    stored_telemetry = {"status": "completed"}
    value, telemetry = split_node_output(pure_return, "agent", stored_telemetry)
    assert value is pure_return
    assert telemetry is stored_telemetry


def test_split_idempotent_across_round_trip() -> None:
    envelope = _sandbox_envelope(agent_return={"ok": True})
    first = split_node_output(envelope, "sandbox_agent", None)
    second = split_node_output(first[0], "sandbox_agent", first[1])
    assert second == first


# ---------------------------------------------------------------------------
# split_node_output -- malformed / unknown (never raises)
# ---------------------------------------------------------------------------


def test_split_malformed_non_dict_returns_empty(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="modulo.core.node_output_split")
    value, telemetry = split_node_output("not a dict", "agent", None)
    assert value is None
    assert telemetry == {}
    assert "node_output_split.malformed" in caplog.text


def test_split_unknown_node_type_best_effort(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="modulo.core.node_output_split")
    envelope = {"output": {"data": 1}, "custom": True}
    value, telemetry = split_node_output(envelope, "mystery_type", None, run_id="run-1", node_id="node-1")
    assert value == {"data": 1}
    assert telemetry == {"custom": True}
    assert "node_output_split.unknown" in caplog.text
    assert caplog.records[-1].run_id == "run-1"
    assert caplog.records[-1].node_id == "node-1"


def test_split_lossless_unknown_top_level_fields() -> None:
    agent_return = {"done": True}
    envelope = _sandbox_envelope(agent_return=agent_return)
    envelope["future_flag"] = {"kept": True}
    value, telemetry = split_node_output(envelope, "sandbox_agent", None)
    assert value == agent_return
    assert telemetry["future_flag"] == {"kept": True}


# ---------------------------------------------------------------------------
# node_return -- legacy-safe pure-return accessor
# ---------------------------------------------------------------------------


def test_node_return_legacy_verbatim() -> None:
    outputs = {"n1": {"artifacts": [{"node_id": "n1", "status": "completed"}], "output": {"x": 1}}}
    assert node_return(outputs, None, "n1") == outputs["n1"]


def test_node_return_pure_row_verbatim_not_resplit() -> None:
    pure_return = {"artifacts": [{"x": 1}], "output": {"y": 2}}
    telemetry = {"status": "completed"}
    outputs = {"n1": pure_return}
    assert node_return(outputs, telemetry, "n1") is pure_return


def test_node_return_telemetry_presence_only() -> None:
    # A telemetry entry for the node means the value is already pure -- returned
    # verbatim regardless of its shape.
    pure_return = {"artifacts": [{"x": 1}], "output": {"y": 2}}
    outputs = {"n1": pure_return}
    telemetry = {"n1": {"status": "completed"}}
    assert node_return(outputs, telemetry, "n1") is pure_return


def test_node_return_missing_node_returns_none() -> None:
    assert node_return({"n1": 1}, None, "nope") is None
    assert node_return(None, None, "n1") is None


# ---------------------------------------------------------------------------
# node_telemetry -- legacy extraction mirrors finalize._node_output_dict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node_value",
    [
        {"output": {"status": "completed", "wall_clock_time_ms": 1000}},
        {"output": {"a": 1}, "extra": 2},
        {"output": "a plain string", "other": 3},
        {"artifacts": [{"node_id": "n"}], "manual_output": {"m": 1}},
        {"artifacts": []},
        "just a string",
        None,
        42,
    ],
    ids=[
        "wallclock",
        "output-plus-extra",
        "plain-string-output",
        "artifacts-and-manual",
        "empty-artifacts",
        "plain-string",
        "none",
        "int",
    ],
)
def test_node_telemetry_legacy_equals_finalize_node_output_dict(node_value: Any) -> None:
    outputs = {"n1": node_value}
    assert node_telemetry(None, outputs, "n1") == _node_output_dict(outputs, "n1")


def test_node_telemetry_absent_returns_none() -> None:
    assert node_telemetry(None, {"n1": {"output": {"a": 1}}}, "nope") is None
    assert node_telemetry(None, None, "n1") is None


def test_node_telemetry_pure_row_returns_stored() -> None:
    telemetry = {"n1": {"status": "completed", "wall_clock_time_ms": 1000}}
    outputs = {"n1": {"artifacts": [{"x": 1}], "output": {"a": 1}}}
    assert node_telemetry(telemetry, outputs, "n1") == telemetry["n1"]


def test_node_telemetry_legacy_extracts_wallclock() -> None:
    outputs = {"n1": {"output": {"status": "completed", "wall_clock_time_ms": 3_600_000}}}
    telemetry = node_telemetry(None, outputs, "n1")
    assert telemetry is not None
    assert telemetry["wall_clock_time_ms"] == 3_600_000


# ---------------------------------------------------------------------------
# extend_node_type_map_from_edges
# ---------------------------------------------------------------------------


def test_extend_type_map_stamps_gates_from_edges() -> None:
    graph = {
        "nodes": [{"id": "a", "node_type": "agent"}, {"id": "b", "node_type": "agent"}],
        "edges": [
            {"source": "a", "target": "b", "hitl_gate_config": {"gate_id": "hitl_gate_a_b"}},
            {"source": "b", "target": "a", "hitl_gate_config": {"gate_id": "hitl_gate_b_a"}, "type": "reject"},
            {"source": "a", "target": "c", "type": "normal"},
        ],
    }
    input_map = {"a": "agent", "b": "agent"}
    result = extend_node_type_map_from_edges(input_map, graph)
    assert result["a"] == "agent"
    assert result["hitl_gate_a_b"] == NODE_TYPE_GATE
    assert result["hitl_gate_b_a"] == NODE_TYPE_GATE
    assert "c" not in result
    # Input map is never mutated.
    assert "hitl_gate_a_b" not in input_map


def test_extend_type_map_source_node_id_fallback() -> None:
    graph = {"edges": [{"source_node_id": "x", "target_node_id": "y", "hitl_gate_config": {}}]}
    result = extend_node_type_map_from_edges(None, graph)
    assert result["hitl_gate_x_y"] == NODE_TYPE_GATE


def test_extend_type_map_safe_on_malformed_graph() -> None:
    assert extend_node_type_map_from_edges({"a": "agent"}, None) == {"a": "agent"}
    assert extend_node_type_map_from_edges({"a": "agent"}, {"edges": "nope"}) == {"a": "agent"}
    assert not extend_node_type_map_from_edges(None, {"nodes": [], "edges": []})


def test_extend_type_map_skips_edges_without_source_or_target() -> None:
    graph = {
        "edges": [
            {"hitl_gate_config": {"gate_id": "g1"}, "source": "a"},
            {"hitl_gate_config": {"gate_id": "g2"}, "target": "b"},
            {"hitl_gate_config": {"gate_id": "g3"}},
        ]
    }
    assert not extend_node_type_map_from_edges(None, graph)
