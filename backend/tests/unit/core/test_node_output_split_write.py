"""Write-side integration tests for the FAR-125 P1b split-then-merge write-flip.

Covers ``finalize._split_merge_outputs`` and the finalize/cancel write paths:
the split-then-merge produces LOCKSTEP two-column rows, already-pure rows are
idempotent no-ops, the cancel path re-feeds BOTH stored columns without
re-splitting pure rows, the legacy fallback writes shape-identical rows, and
recovery telemetry survives a later finalize merge (recovery-vs-finalize).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.cost_controller.finalize import (
    _split_merge_outputs,
    finalize_cancelled_run,
    finalize_cost,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _sandbox_envelope(*, agent_return: Any, wall_ms: int = 1200) -> dict[str, Any]:
    """A realistic legacy sandbox_agent envelope (mirrors node_runner)."""
    inner: dict[str, Any] = {
        "status": "completed",
        "summary": "did the thing",
        "wall_clock_time_ms": wall_ms,
        "exit_code": 0,
        "model_cost_usd": 0.001,
        "output_json": agent_return,
    }
    outer = {key: value for key, value in inner.items() if key != "output_json"}
    return {"artifacts": [{"node_id": "node-a", "status": "completed", "output": inner}], "output": outer}


def _make_run(**kw: Any) -> MagicMock:
    run = MagicMock()
    run.id = kw.get("id", uuid.uuid4())
    run.organisation_id = kw.get("organisation_id", _ORG_ID)
    run.owner_team_id = kw.get("owner_team_id")
    run.node_token_usage = kw.get("node_token_usage")
    run.outputs_json = kw.get("outputs_json")
    run.node_telemetry_json = kw.get("node_telemetry_json")
    run.started_at = kw.get("started_at", datetime.now(UTC))
    run.snapshot_id = kw.get("snapshot_id", uuid.uuid4())
    run.ledger_written = kw.get("ledger_written", False)
    run.ledger_refused_at = kw.get("ledger_refused_at")
    return run


def _mock_session(run: Any) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    return session


# ---------------------------------------------------------------------------
# split-then-merge -> LOCKSTEP two-column rows
# ---------------------------------------------------------------------------


async def test_finalize_writes_lockstep_columns_from_legacy_segment() -> None:
    """The main finalize path splits a LEGACY segment into LOCKSTEP columns: the
    pure return lands in ``outputs_json`` and the exhaustive telemetry in
    ``node_telemetry_json`` — every outputs key has a telemetry key."""
    seg_return = {"summary": "agent summary", "changed_files": []}
    segment = {"node-a": _sandbox_envelope(agent_return=seg_return, wall_ms=1200)}
    run = _make_run()
    session = _mock_session(run)
    with (
        patch("modulo.core.cost_controller.finalize.load_live_components", return_value=[]),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs,
        patch("modulo.core.cost_controller.finalize.record_run_facts", new=AsyncMock()),
    ):
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=segment,
            node_type_map={"node-a": "sandbox_agent"},
            is_terminal=True,
        )
    kwargs = mock_urs.await_args.kwargs
    assert set(kwargs["outputs_json"]) == {"node-a"}
    assert set(kwargs["node_telemetry_json"]) == {"node-a"}  # lockstep
    assert kwargs["outputs_json"]["node-a"] == seg_return
    assert kwargs["node_telemetry_json"]["node-a"]["wall_clock_time_ms"] == 1200
    assert "output_json" not in kwargs["node_telemetry_json"]["node-a"]


def test_split_merge_mixed_segment_lockstep_and_values() -> None:
    """A mixed legacy segment (sandbox + agent + gate) splits every row and the
    merged result stays LOCKSTEP with the correct pure returns."""
    agent_return = {"answer": 42}
    segment = {
        "node-sb": _sandbox_envelope(agent_return={"done": True}),
        "node-ag": {
            "artifacts": [{"node_id": "node-ag", "status": "completed", "output": agent_return}],
            "output": agent_return,
        },
        "node-gate": {
            "artifacts": [
                {
                    "node_id": "hitl_gate_a_b",
                    "status": "interrupted",
                    "result": "approved",
                    "human_data": {"action": "approved"},
                }
            ]
        },
    }
    type_map = {
        "node-sb": "sandbox_agent",
        "node-ag": "agent",
        "hitl_gate_a_b": "gate",
    }
    outputs, telemetry = _split_merge_outputs(None, None, segment, type_map, run_id="run-1")
    assert set(outputs) == set(segment)  # lockstep on both columns
    assert set(telemetry) == set(segment)
    assert outputs["node-sb"] == {"done": True}
    assert outputs["node-ag"] == agent_return
    assert outputs["node-gate"] == {"action": "approved"}
    assert telemetry["node-gate"]["result"] == "approved"


# ---------------------------------------------------------------------------
# already-pure rows are idempotent
# ---------------------------------------------------------------------------


async def test_finalize_already_pure_rows_idempotent_noop() -> None:
    """A stored PURE row re-fed as the segment is an idempotent no-op: both
    columns come back byte-identical (never re-split, never clobbered)."""
    pure_return = {"summary": "x", "data": 1}
    stored_outputs = {"node-a": pure_return}
    stored_telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 10}}
    run = _make_run(outputs_json=stored_outputs, node_telemetry_json=stored_telemetry)
    session = _mock_session(run)
    with (
        patch("modulo.core.cost_controller.finalize.load_live_components", return_value=[]),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs,
        patch("modulo.core.cost_controller.finalize.record_run_facts", new=AsyncMock()),
    ):
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=run.outputs_json,
            node_type_map={"node-a": "sandbox_agent"},
            is_terminal=True,
        )
    kwargs = mock_urs.await_args.kwargs
    assert kwargs["outputs_json"] == stored_outputs
    assert kwargs["node_telemetry_json"] == stored_telemetry


def test_split_merge_pure_row_return_is_same_object() -> None:
    """Idempotence holds at the object level for the pure row."""
    pure_return = {"summary": "x"}
    stored_telemetry = {"node-a": {"status": "completed"}}
    outputs, telemetry = _split_merge_outputs(
        {"node-a": pure_return}, stored_telemetry, {"node-a": pure_return}, {"node-a": "sandbox_agent"}
    )
    assert outputs["node-a"] is pure_return
    assert telemetry["node-a"] is stored_telemetry["node-a"]


# ---------------------------------------------------------------------------
# cancel path — re-feeds both stored columns, pure rows NOT re-split
# ---------------------------------------------------------------------------


async def test_cancel_path_does_not_resplit_pure_rows() -> None:
    """``finalize_cancelled_run`` re-feeds BOTH stored columns; already-pure
    rows are idempotent no-ops — no re-split log, no telemetry clobber."""
    pure_return = {"summary": "x"}
    stored_outputs = {"node-a": pure_return}
    stored_telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 5000}}
    run = _make_run(
        node_token_usage={"node-a": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
        outputs_json=stored_outputs,
        node_telemetry_json=stored_telemetry,
    )
    session = _mock_session(run)
    with (
        patch("modulo.core.cost_controller.finalize.load_live_components", return_value=[]),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs,
        patch("modulo.core.cost_controller.finalize.record_run_facts", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_cancelled_run(session, run_id=run.id, org_id=_ORG_ID)
    kwargs = mock_urs.await_args.kwargs
    assert mock_urs.await_args.args[2] == "cancelled"
    assert kwargs["outputs_json"] == stored_outputs
    assert kwargs["node_telemetry_json"] == stored_telemetry
    logged = [str(c) for c in mock_log.info.call_args_list]
    assert all("legacy_output_resplit" not in line for line in logged)


# ---------------------------------------------------------------------------
# fallback path — shape-identical two-column rows
# ---------------------------------------------------------------------------


async def test_fallback_writes_shape_identical_two_column_rows() -> None:
    """The legacy fallback write persists BOTH columns shape-identical to the
    split-then-merge result — never an un-split envelope."""
    seg_return = {"summary": "s"}
    segment = {"node-a": _sandbox_envelope(agent_return=seg_return, wall_ms=1200)}
    run = _make_run()
    session = _mock_session(run)
    with (
        patch(
            "modulo.core.cost_controller.finalize.load_live_components",
            side_effect=RuntimeError("boom"),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs,
        patch("modulo.core.cost_controller.finalize.record_run_facts", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.1332")),
    ):
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="failed",
            segment_node_token_usage=None,
            segment_completed_node_outputs=segment,
            node_type_map={"node-a": "sandbox_agent"},
            is_terminal=True,
        )
    kwargs = mock_urs.await_args.kwargs
    assert kwargs["outputs_json"]["node-a"] == seg_return
    assert kwargs["node_telemetry_json"]["node-a"]["wall_clock_time_ms"] == 1200
    assert set(kwargs["outputs_json"]) == set(kwargs["node_telemetry_json"])  # lockstep
    assert "output_json" not in kwargs["node_telemetry_json"]["node-a"]


# ---------------------------------------------------------------------------
# recovery-vs-finalize — stored recovery fields survive a later merge
# ---------------------------------------------------------------------------


async def test_recovery_fields_survive_later_finalize_merge() -> None:
    """Round 1 splits a LEGACY recovery marker into two columns via the FINALIZE
    write path; round 2 (a later finalize re-feeding the now-PURE rows) keeps the
    stored recovery telemetry — the merge never clobbers recovery facts."""
    input_data = {"claim": "input"}
    legacy_recovery = {"input": input_data, "output": input_data, "recovered": True}

    # Round 1 — finalize splits the legacy recovery marker.
    run = _make_run()
    session = _mock_session(run)
    with (
        patch("modulo.core.cost_controller.finalize.load_live_components", return_value=[]),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs,
        patch("modulo.core.cost_controller.finalize.record_run_facts", new=AsyncMock()),
    ):
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs={"node-a": legacy_recovery},
            node_type_map={"node-a": "sandbox_agent"},
            is_terminal=True,
        )
    stored_outputs = mock_urs.await_args.kwargs["outputs_json"]
    stored_telemetry = mock_urs.await_args.kwargs["node_telemetry_json"]
    assert stored_outputs["node-a"] == input_data
    assert stored_telemetry["node-a"] == {"recovered": True, "recovery_input": input_data}

    # Round 2 — a LATER finalize re-feeds the now-PURE stored rows. The segment
    # no longer carries recovery info; the stored recovery fields must survive.
    run2 = _make_run(outputs_json=stored_outputs, node_telemetry_json=stored_telemetry)
    session2 = _mock_session(run2)
    with (
        patch("modulo.core.cost_controller.finalize.load_live_components", return_value=[]),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs2,
        patch("modulo.core.cost_controller.finalize.record_run_facts", new=AsyncMock()),
    ):
        await finalize_cost(
            session2,
            run_id=run2.id,
            org_id=_ORG_ID,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=run2.outputs_json,
            node_type_map={"node-a": "sandbox_agent"},
            is_terminal=True,
        )
    kwargs2 = mock_urs2.await_args.kwargs
    assert kwargs2["outputs_json"]["node-a"] == input_data
    assert kwargs2["node_telemetry_json"]["node-a"]["recovered"] is True
    assert kwargs2["node_telemetry_json"]["node-a"]["recovery_input"] == input_data
