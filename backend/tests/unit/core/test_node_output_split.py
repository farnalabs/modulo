"""Unit tests for the per-node output split helpers (FAR-124).

Covers the full ``split_node_output`` extraction table (sandbox_agent,
regular agent, connector completed + failure, HITL gate incl. the pinned
``deliver_manual`` shape, manual-node resume, recovery markers, already-pure
idempotence, pure-return-with-artifacts/output keys NOT re-split, lossless
telemetry, malformed + unknown), the READ accessors (``node_return`` /
``node_telemetry``), and the edge-driven type-map extension for HITL gates.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from modulo.core.cost_controller.finalize import _store_split_result
from modulo.core.node_output_split import (
    DISPATCH_PROVENANCE_FIELDS,
    NODE_TYPE_GATE,
    TELEMETRY_FIELDS,
    extend_node_type_map_from_edges,
    has_telemetry_entry,
    is_split_envelope,
    node_return,
    node_stderr_artifact,
    node_stdout_artifact,
    node_telemetry,
    split_node_output,
    stored_row_is_legacy,
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


def test_split_dispatch_failure_preserves_witness_provenance() -> None:
    """FAR-1141 (MAJOR 6): the failed branch DROPS ``output``, so the stamped
    witness provenance is lifted out of it first — otherwise a dispatch node
    that failed after firing an external job reached failure inspection with
    only the hand-duplicated ``substrate_status`` (and, before the fix, with
    nothing at all from the stamped output)."""
    stamped_output = {
        "id": "job-1",
        "status": "failure",
        "witnessed_via": "inst-1",
        "execution_identity": "customer_substrate",
        "declared_external_cost": None,
        "substrate_status": "failure",
        "provenance": "reported",
    }
    envelope = {
        "artifacts": [{"node_id": "d1", "status": "failed", "error": "failure"}],
        "output": stamped_output,
        "substrate_status": "failure",
    }
    value, telemetry = split_node_output(envelope, "dispatch", None)
    assert value is None
    assert telemetry["status"] == "failed"
    for key in DISPATCH_PROVENANCE_FIELDS:
        assert telemetry[key] == stamped_output[key], key
    # the output itself stays out of the failed branch's telemetry
    assert "output" not in telemetry


def test_split_dispatch_failure_without_provenance_keys_is_a_noop() -> None:
    """A failed output carrying NO provenance keys must not raise while the
    splitter lifts the declared set — every key is simply absent."""
    envelope = {
        "artifacts": [{"node_id": "d1", "status": "failed", "error": "boom"}],
        "output": {"foo": 1},
    }
    value, telemetry = split_node_output(envelope, "dispatch", None)
    assert value is None
    assert telemetry["status"] == "failed"
    assert telemetry["error"] == "boom"
    assert "output" not in telemetry


# ---------------------------------------------------------------------------
# split_node_output -- HITL gate (incl. pinned deliver_manual)
# ---------------------------------------------------------------------------


def test_split_gate_approved() -> None:
    decision = {"action": "approved"}
    envelope = {
        "artifacts": [
            {"node_id": "hitl_review_a_b", "status": "interrupted", "result": "approved", "human_data": decision}
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
                "node_id": "hitl_review_a_b",
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
            {"node_id": "hitl_review_x_y", "status": "interrupted", "result": "rejected", "human_data": decision}
        ]
    }
    value, telemetry = split_node_output(envelope, None, None)
    assert value == decision
    assert telemetry["result"] == "rejected"


def test_split_gate_no_human_data_returns_none() -> None:
    envelope = {
        "artifacts": [{"node_id": "hitl_review_a_b", "status": "auto_approved", "autonomy": "fully_autonomous"}]
    }
    value, telemetry = split_node_output(envelope, NODE_TYPE_GATE, None)
    assert value is None
    assert telemetry["status"] == "auto_approved"
    assert telemetry["autonomy"] == "fully_autonomous"


def test_split_gate_detected_by_interrupted_status_only() -> None:
    # No human_data/autonomy/result on the artifact: the gate is still detected
    # from the interrupted status alone.
    envelope = {"artifacts": [{"node_id": "hitl_review_a_b", "status": "interrupted"}]}
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
    # record ({skipped: true}); the writer must not write outputs_json[id].
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


def test_split_new_format_output_telemetry_artifacts_idempotent() -> None:
    """FAR-130 proof: the already-split sandbox envelope carries ``output`` +
    ``telemetry`` + the state-accumulator ``artifacts`` — re-splitting it must
    return the pure return and the flat telemetry verbatim, never nest the
    telemetry under the return nor fold the artifact in."""
    pure_return = {"pr_url": "https://github.com/farnalabs/modulo/pull/1", "summary": "done"}
    telemetry = {"status": "completed", "cost_estimate_usd": 0.1, "agent_stdout": "hi"}
    envelope = {
        "output": pure_return,
        "telemetry": telemetry,
        "artifacts": [{"node_id": "sandbox-1", "status": "completed", "output": {"output_json": pure_return}}],
    }

    value, split_telemetry = split_node_output(envelope, "sandbox_agent", None)

    assert value == pure_return
    assert split_telemetry == telemetry
    assert "telemetry" not in split_telemetry


def test_split_bare_output_telemetry_without_artifacts_is_not_split() -> None:
    """Collision guard (FAR-130 review): the production split envelope ALWAYS
    carries ``artifacts``, so ``is_split_envelope`` requires it. A bare
    ``{output, telemetry}`` pair is therefore NOT treated as already split --
    it falls through to the per-type splitter, which returns the envelope's
    ``output`` and folds the remaining keys into telemetry losslessly (the
    already-split branch would instead have returned the telemetry channel
    FLAT, never nested under a ``telemetry`` key)."""
    pure_return = {"ok": True}
    telemetry = {"status": "completed", "wall_clock_time_ms": 10}
    value, split_telemetry = split_node_output({"output": pure_return, "telemetry": telemetry}, "router", None)
    assert value == pure_return
    assert split_telemetry == {"telemetry": telemetry}


def test_split_new_format_folds_extra_toplevel_stamps_losslessly() -> None:
    """The already-split envelope may carry extra top-level stamps (FAR-130:
    ``_record_chain_end_output`` stamps ``otel_span_id`` / ``otel_trace_id``
    before persistence). Those must be folded into telemetry losslessly rather
    than dropped by the idempotent branch."""
    stamped = {
        "output": {"pr_url": "u"},
        "telemetry": {"status": "completed"},
        "artifacts": [{"node_id": "sb", "status": "completed", "output": {}}],
        "otel_span_id": "span-123",
        "otel_trace_id": "trace-abc",
    }
    ret, tel = split_node_output(stamped, "sandbox_agent", None)
    assert ret == {"pr_url": "u"}
    assert tel["status"] == "completed"
    assert tel["otel_span_id"] == "span-123"
    assert tel["otel_trace_id"] == "trace-abc"
    # the split channels are NOT re-folded into telemetry
    assert "output" not in tel
    assert "telemetry" not in tel
    assert "artifacts" not in tel


# ---------------------------------------------------------------------------
# is_split_envelope -- the ONE canonical split-detection predicate
# ---------------------------------------------------------------------------


def test_is_split_envelope_true_for_real_three_key_shape() -> None:
    """The production envelope carries output + dict telemetry + artifacts."""
    envelope = {
        "output": {"pr_url": "u"},
        "telemetry": {"status": "completed"},
        "artifacts": [{"node_id": "sb", "status": "completed", "output": {}}],
    }
    assert is_split_envelope(envelope) is True


def test_is_split_envelope_false_without_artifacts() -> None:
    """Collision guard: a bare ``{output, telemetry}`` pair is not the
    production shape, so it must NOT be detected as split."""
    assert is_split_envelope({"output": {"ok": True}, "telemetry": {"status": "completed"}}) is False


def test_is_split_envelope_false_for_legacy_artifacts_output() -> None:
    """A legacy envelope carries ``artifacts`` + ``output`` but NO telemetry."""
    legacy = {"artifacts": [{"node_id": "sb", "output": {}}], "output": {"status": "completed"}}
    assert is_split_envelope(legacy) is False


def test_is_split_envelope_false_for_non_dict_and_non_dict_telemetry() -> None:
    assert is_split_envelope(None) is False
    assert is_split_envelope("not-a-dict") is False
    assert is_split_envelope({"output": {}, "telemetry": "flat", "artifacts": []}) is False


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
# node_return -- pure-return accessor
# ---------------------------------------------------------------------------


def test_node_return_does_not_interpret_legacy_envelope() -> None:
    envelope = {"artifacts": [{"node_id": "n1", "status": "completed"}], "output": {"x": 1}}
    assert node_return({"n1": envelope}, None, "n1") is envelope


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
# node_telemetry -- pure telemetry accessor
# ---------------------------------------------------------------------------


def test_node_telemetry_does_not_interpret_legacy_envelope() -> None:
    envelope = {"output": {"status": "completed", "wall_clock_time_ms": 1000}}
    assert node_telemetry(None, {"n1": envelope}, "n1") is None


def test_node_telemetry_absent_returns_none() -> None:
    assert node_telemetry(None, {"n1": {"output": {"a": 1}}}, "nope") is None
    assert node_telemetry(None, None, "n1") is None


def test_node_telemetry_pure_row_returns_stored() -> None:
    telemetry = {"n1": {"status": "completed", "wall_clock_time_ms": 1000}}
    outputs = {"n1": {"artifacts": [{"x": 1}], "output": {"a": 1}}}
    assert node_telemetry(telemetry, outputs, "n1") == telemetry["n1"]


# ---------------------------------------------------------------------------
# has_telemetry_entry -- presence predicate
# ---------------------------------------------------------------------------


def test_has_telemetry_entry_true_when_entry_present() -> None:
    """A split row keyed by the node -> True (the accessor returns it verbatim)."""
    assert has_telemetry_entry({"n1": {"status": "completed"}}, "n1")
    # Presence only, never the value's content: an entry of ANY shape counts.
    assert has_telemetry_entry({"n1": None}, "n1")
    assert has_telemetry_entry({"other": {"status": "completed"}, "n1": {}}, "n1")


def test_has_telemetry_entry_false_when_entry_absent() -> None:
    """A node absent from telemetry -> False; outputs are never interpreted."""
    assert not has_telemetry_entry({"n1": {"status": "completed"}}, "nope")
    assert not has_telemetry_entry({}, "n1")


def test_has_telemetry_entry_false_for_non_dict_telemetry() -> None:
    """Non-dict telemetry columns can never carry an entry."""
    assert not has_telemetry_entry(None, "n1")
    assert not has_telemetry_entry("not a dict", "n1")
    assert not has_telemetry_entry([{"n1": 1}], "n1")
    assert not has_telemetry_entry(42, "n1")


# ---------------------------------------------------------------------------
# node_stdout_artifact -- FAR-811 full-stdout pointer accessor
# ---------------------------------------------------------------------------


def _stdout_pointer() -> dict[str, Any]:
    return {
        "rel_path": "00000000-0000-0000-0000-000000000001/run-1/n1/a.stdout.zst",
        "size_bytes": 12345,
        "sha256": "0" * 64,
        "stream": "stdout",
        "compression": "zstd",
        "truncated": False,
        "redacted": True,
    }


def test_node_stdout_artifact_pure_row_returns_stored_pointer_verbatim() -> None:
    pointer = _stdout_pointer()
    telemetry = {"n1": {"status": "completed", "stdout_artifact": pointer}}
    assert node_stdout_artifact(telemetry, {}, "n1") is pointer


def test_node_stdout_artifact_does_not_interpret_legacy_envelope() -> None:
    pointer = _stdout_pointer()
    inner = {"status": "completed", "stdout_artifact": pointer, "output_json": {"ok": 1}}
    outputs = {"n1": {"artifacts": [{"node_id": "n1", "status": "completed", "output": inner}], "output": inner}}
    assert node_stdout_artifact(None, outputs, "n1") is None


def test_node_stdout_artifact_none_when_absent() -> None:
    assert node_stdout_artifact({"n1": {"status": "completed"}}, {}, "n1") is None
    assert node_stdout_artifact(None, {"n1": {"output": {"status": "completed"}}}, "n1") is None


def test_node_stdout_artifact_ignores_non_dict_pointer() -> None:
    # A stringly/None ``stdout_artifact`` value is never surfaced as a pointer.
    assert node_stdout_artifact({"n1": {"stdout_artifact": "just-a-log-line"}}, {}, "n1") is None
    assert node_stdout_artifact({"n1": {"stdout_artifact": None}}, {}, "n1") is None


def test_node_stdout_artifact_missing_node_returns_none() -> None:
    assert node_stdout_artifact({"other": {"stdout_artifact": _stdout_pointer()}}, {}, "n1") is None


# ---------------------------------------------------------------------------
# node_stderr_artifact -- FAR-879 full-stderr pointer accessor
# ---------------------------------------------------------------------------


def _stderr_pointer() -> dict[str, Any]:
    return {
        "rel_path": "00000000-0000-0000-0000-000000000001/run-1/n1/a.stderr.zst",
        "size_bytes": 8192,
        "sha256": "1" * 64,
        "stream": "stderr",
        "compression": "zstd",
        "truncated": False,
        "redacted": True,
    }


def test_node_stderr_artifact_pure_row_returns_stored_pointer_verbatim() -> None:
    pointer = _stderr_pointer()
    telemetry = {"n1": {"status": "completed", "stderr_artifact": pointer}}
    assert node_stderr_artifact(telemetry, {}, "n1") is pointer


def test_node_stderr_artifact_does_not_interpret_legacy_envelope() -> None:
    pointer = _stderr_pointer()
    inner = {"status": "completed", "stderr_artifact": pointer, "output_json": {"ok": 1}}
    outputs = {"n1": {"artifacts": [{"node_id": "n1", "status": "completed", "output": inner}], "output": inner}}
    assert node_stderr_artifact(None, outputs, "n1") is None


def test_node_stderr_artifact_none_when_absent() -> None:
    assert node_stderr_artifact({"n1": {"status": "completed"}}, {}, "n1") is None
    assert node_stderr_artifact(None, {"n1": {"output": {"status": "completed"}}}, "n1") is None


def test_node_stderr_artifact_ignores_non_dict_pointer() -> None:
    assert node_stderr_artifact({"n1": {"stderr_artifact": "just-a-log-line"}}, {}, "n1") is None
    assert node_stderr_artifact({"n1": {"stderr_artifact": None}}, {}, "n1") is None


def test_node_stderr_artifact_missing_node_returns_none() -> None:
    assert node_stderr_artifact({"other": {"stderr_artifact": _stderr_pointer()}}, {}, "n1") is None


def test_node_stderr_artifact_and_stdout_artifact_coexist() -> None:
    """Both pointers can live on the same node telemetry independently."""
    stdout_ptr = _stdout_pointer()
    stderr_ptr = _stderr_pointer()
    telemetry = {"n1": {"stdout_artifact": stdout_ptr, "stderr_artifact": stderr_ptr}}
    assert node_stdout_artifact(telemetry, {}, "n1") is stdout_ptr
    assert node_stderr_artifact(telemetry, {}, "n1") is stderr_ptr


def test_stderr_artifact_in_telemetry_fields() -> None:
    """stderr_artifact is in TELEMETRY_FIELDS so the split preserves it."""
    assert "stderr_artifact" in TELEMETRY_FIELDS


# ---------------------------------------------------------------------------
# extend_node_type_map_from_edges
# ---------------------------------------------------------------------------


def test_extend_type_map_stamps_gates_from_edges() -> None:
    graph = {
        "nodes": [{"id": "a", "node_type": "agent"}, {"id": "b", "node_type": "agent"}],
        "edges": [
            {"source": "a", "target": "b", "hitl_review_config": {"review_id": "hitl_review_a_b"}},
            {"source": "b", "target": "a", "hitl_review_config": {"review_id": "hitl_review_b_a"}, "type": "reject"},
            {"source": "a", "target": "c", "type": "normal"},
        ],
    }
    input_map = {"a": "agent", "b": "agent"}
    result = extend_node_type_map_from_edges(input_map, graph)
    assert result["a"] == "agent"
    assert result["hitl_review_a_b"] == NODE_TYPE_GATE
    assert result["hitl_review_b_a"] == NODE_TYPE_GATE
    assert "c" not in result
    # Input map is never mutated.
    assert "hitl_review_a_b" not in input_map


def test_extend_type_map_source_node_id_fallback() -> None:
    graph = {"edges": [{"source_node_id": "x", "target_node_id": "y", "hitl_review_config": {}}]}
    result = extend_node_type_map_from_edges(None, graph)
    assert result["hitl_review_x_y"] == NODE_TYPE_GATE


def test_extend_type_map_safe_on_malformed_graph() -> None:
    assert extend_node_type_map_from_edges({"a": "agent"}, None) == {"a": "agent"}
    assert extend_node_type_map_from_edges({"a": "agent"}, {"edges": "nope"}) == {"a": "agent"}
    assert not extend_node_type_map_from_edges(None, {"nodes": [], "edges": []})


def test_extend_type_map_skips_edges_without_source_or_target() -> None:
    graph = {
        "edges": [
            {"hitl_review_config": {"review_id": "g1"}, "source": "a"},
            {"hitl_review_config": {"review_id": "g2"}, "target": "b"},
            {"hitl_review_config": {"review_id": "g3"}},
        ]
    }
    assert not extend_node_type_map_from_edges(None, graph)


# ---------------------------------------------------------------------------
# Current writer invariant
# ---------------------------------------------------------------------------


def test_split_node_output_never_yields_a_legacy_row_for_splittable_types() -> None:
    """INVARIANT (FAR-132): a row written by the current writer for a splittable
    node type NEVER matches the legacy predicate.

    ``finalize._store_split_result`` is the single place that decides which of
    the two LOCKSTEP columns gets a key, so this test derives the row's
    presence flags from THAT writer's output rather than hardcoding them.
    ``split_node_output`` returning a non-None telemetry dict means the writer
    stores a telemetry side, and ``stored_row_is_legacy`` is therefore False for
    a new row. If a writer regression ever stopped storing the telemetry side,
    the derived ``telemetry_present`` would be False and this test would fail
    (hardcoding True made the old form tautological).
    """
    from modulo.core.node_output_split import SPLITTABLE_NODE_TYPES

    envelopes: dict[str, Any] = {
        "sandbox_agent": {"artifacts": [{"output": {"output_json": {"pr_url": "u"}}}]},
        "agent": {"output": {"answer": 1}, "status": "completed"},
        "connector": {"artifacts": [{"output": {"rows": 3}}]},
        "dispatch": {"artifacts": [{"output": {"fired": True}}]},
        "manual": {"manual_output": {"decision": "ok"}},
        NODE_TYPE_GATE: {"artifacts": [{"human_data": {"approved": True}}]},
    }
    assert set(envelopes) == set(SPLITTABLE_NODE_TYPES)

    for node_type, envelope in envelopes.items():
        ret, telemetry = split_node_output(envelope, node_type, None, run_id="r", node_id="n")
        # The splitter MUST yield a telemetry dict for a splittable type (the
        # writer stores a side only when it is non-None).
        assert telemetry is not None, node_type
        # Drive the WRITER's column assignment, then read the presence flags
        # back off its output.
        merged_outputs: dict[str, Any] = {}
        merged_telemetry: dict[str, Any] = {}
        _store_split_result(merged_outputs, merged_telemetry, "n", ret, telemetry)
        outputs_present = "n" in merged_outputs
        telemetry_present = "n" in merged_telemetry
        assert telemetry_present is True, f"writer dropped the telemetry side for {node_type}"
        assert stored_row_is_legacy(outputs_present=outputs_present, telemetry_present=telemetry_present) is False, (
            node_type
        )
