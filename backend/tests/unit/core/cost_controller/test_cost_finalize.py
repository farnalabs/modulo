"""Unit tests for the executor finalize block + ledger (PR A2).

Covers ``_merge`` (segment-wins / empty-accumulator normalization), the
ENRICHED-union construction (the SPLIT sandbox signal, the ONE-mechanism
stored-union rule, the resume-of-stored-unclamped band clamp, the schema-drift
gate), the server-measured token derivation, the legacy-fallback DE-TRUSTS
``cost_estimate_usd`` rule, the pre-component-read terminal transition, and the
``finalize_cancelled_run`` cancellation classes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller.breakdown.constants import MAX_REPORTABLE_BAND_USD
from modulo.core.cost_controller.finalize import (
    _derive_total_tokens,
    _enrich_union,
    _fold_token_usage,
    _legacy_sandbox_cost,
    _merge,
    _split_merge_outputs,
    _token_cost,
    _write_back_node_cost,
    derive_node_type_map,
    finalize_cancelled_run,
    finalize_cost,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# _merge
# ---------------------------------------------------------------------------


def test_merge_segment_wins_on_collision() -> None:
    stored = {"a": {"input_tokens": 1}, "b": {"input_tokens": 2}}
    segment = {"b": {"input_tokens": 99}}
    merged = _merge(stored, segment, segment_wins=True)
    assert merged["b"]["input_tokens"] == 99  # segment wins (replaced, never summed)
    assert merged["a"]["input_tokens"] == 1


def test_merge_empty_accumulator_leaves_stored_untouched() -> None:
    stored = {"a": {"input_tokens": 1}}
    assert _merge(stored, None, segment_wins=True) == stored
    assert _merge(stored, {}, segment_wins=True) == stored


# ---------------------------------------------------------------------------
# _split_merge_outputs — the FAR-125 P1b two-column write-flip
# ---------------------------------------------------------------------------


def test_split_merge_legacy_row_splits_into_lockstep_columns() -> None:
    """A LEGACY stored envelope is re-split: the pure return lands in the
    outputs column and the exhaustive telemetry in the telemetry column —
    LOCKSTEP (every outputs key has a telemetry key)."""
    agent_return = {"summary": "agent summary", "changed_files": []}
    stored_outputs = {
        "node-a": {
            "artifacts": [
                {
                    "node_id": "node-a",
                    "status": "completed",
                    "output": {
                        "status": "completed",
                        "summary": "did the thing",
                        "wall_clock_time_ms": 1200,
                        "output_json": agent_return,
                    },
                }
            ],
            "output": {"status": "completed", "summary": "did the thing", "wall_clock_time_ms": 1200},
        }
    }
    outputs, telemetry = _split_merge_outputs(stored_outputs, None, None, {"node-a": "sandbox_agent"}, run_id="run-1")
    assert set(outputs) == {"node-a"}
    assert set(telemetry) == {"node-a"}  # lockstep
    assert outputs["node-a"] == agent_return
    assert telemetry["node-a"]["status"] == "completed"
    assert telemetry["node-a"]["wall_clock_time_ms"] == 1200
    assert "output_json" not in telemetry["node-a"]


def test_split_merge_already_pure_rows_are_idempotent_noop() -> None:
    """A stored PURE row (telemetry entry exists) passes through UNCHANGED —
    never re-split, never clobbered by a later segment."""
    pure_return = {"summary": "x", "data": 1}
    stored_telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 10}}
    outputs, telemetry = _split_merge_outputs(
        {"node-a": pure_return}, stored_telemetry, {"node-a": pure_return}, {"node-a": "sandbox_agent"}
    )
    assert outputs["node-a"] is pure_return
    assert telemetry["node-a"] is stored_telemetry["node-a"]


def test_split_merge_skipped_recovery_is_telemetry_only() -> None:
    """A skipped recovery marker has NO outputs key — the telemetry entry is the
    sole record (lockstep holds because the outputs key is omitted)."""
    outputs, telemetry = _split_merge_outputs(
        {"node-a": {"input": None, "output": None, "skipped": True}},
        None,
        None,
        {"node-a": "agent"},
        run_id="run-1",
    )
    assert "node-a" not in outputs
    assert telemetry == {"node-a": {"skipped": True}}


# ---------------------------------------------------------------------------
# _enrich_union — the split sandbox signal + the ONE-mechanism rule
# ---------------------------------------------------------------------------


def test_enrich_union_split_sandbox_signal() -> None:
    usage = {"node-a": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    outputs = {"node-a": {"output": {"status": "completed", "wall_clock_time_ms": 3_600_000}}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=True)
    entry = union["node-a"]
    assert entry["is_sandbox_for_wallclock"] is True
    assert entry["sandbox_by_map"] is True
    assert entry["wall_clock_time_ms"] == 3_600_000
    # token fields stay the SERVER entries — no fold, no cap.
    assert entry["input_tokens"] == 10
    assert entry["output_tokens"] == 5


def test_enrich_union_map_absent_wallclock_failsafe() -> None:
    """A map-absent node is sandbox for wall-clock, NEVER self-report-eligible."""
    usage = {"node-a": {"model_cost_usd": 5.0}}
    outputs = {"node-a": {"output": {"wall_clock_time_ms": 1000}}}
    union = _enrich_union(usage, outputs, {}, is_terminal=False)
    assert union["node-a"]["is_sandbox_for_wallclock"] is True
    assert union["node-a"]["sandbox_by_map"] is False


def test_enrich_union_agent_node_with_model_cost_not_wallclock() -> None:
    """An agent node carrying model_cost_usd is NOT sandbox by either signal."""
    usage = {"node-a": {"model_cost_usd": 5.0}}
    outputs = {"node-a": {"output": {"wall_clock_time_ms": 1000}}}
    union = _enrich_union(usage, outputs, {"node-a": "agent"}, is_terminal=False)
    assert union["node-a"]["is_sandbox_for_wallclock"] is False
    assert union["node-a"]["sandbox_by_map"] is False


def test_enrich_union_resume_of_stored_unclamped_band_clamp() -> None:
    """A stored UNCLAMPED model_cost_usd (written before PR A deployed) is
    re-clamped through clamp_reported at enrichment — the $6000 -> band clamp."""
    usage = {"node-a": {"model_cost_usd": 6000.0, "wall_clock_time_ms": 1000}}
    union = _enrich_union(usage, {}, {"node-a": "sandbox_agent"}, is_terminal=False)
    assert union["node-a"]["model_cost_usd"] == float(MAX_REPORTABLE_BAND_USD)
    assert union["node-a"]["model_cost_clamped"] is True
    assert union["node-a"]["model_cost_out_of_band_high"] is True


def test_enrich_union_output_present_overwrites_with_reclamped_fold() -> None:
    usage = {"node-a": {"model_cost_usd": 0.01}}
    outputs = {"node-a": {"output": {"model_cost_usd": 0.04, "model_cost_raw_usd": 0.0412}}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=False)
    # The union stores the RE-CLAMPED value from the RAW input (0.0412 unchanged
    # under the band) — the producer's own 0.04 clamp is not the union authority.
    assert union["node-a"]["model_cost_usd"] == pytest.approx(0.0412)
    assert union["node-a"]["model_cost_raw_usd"] == pytest.approx(0.0412)


def test_enrich_union_output_present_but_lacking_pops_sibling_flags() -> None:
    """Case (2): output PRESENT but LACKS model_cost_usd -> the node is estimated."""
    usage = {"node-a": {"model_cost_usd": 5.0, "model_cost_raw_usd": 5.0}}
    outputs = {"node-a": {"output": {"status": "completed", "wall_clock_time_ms": 1000}}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=False)
    for key in ("model_cost_usd", "model_cost_raw_usd", "model_cost_clamped", "model_cost_out_of_band_high"):
        assert key not in union["node-a"]


@pytest.mark.parametrize(
    ("is_terminal", "map_type", "pin_failed", "should_increment"),
    [
        (True, "sandbox_agent", False, True),
        (False, "sandbox_agent", False, False),  # terminal-only increment
        (True, "agent", False, False),  # non-sandbox provenance gate
        (True, "sandbox_agent", True, False),  # pin_failed gate
    ],
)
def test_enrich_union_schema_drift_increment_gated(
    is_terminal: bool, map_type: str, pin_failed: bool, should_increment: bool
) -> None:
    output = {"schema_drift": True}
    if pin_failed:
        output["pin_failed"] = True
    outputs = {"node-a": {"output": output}}
    with patch("modulo.core.cost_controller.finalize.record_schema_drift") as mock_counter:
        _enrich_union({}, outputs, {"node-a": map_type}, is_terminal=is_terminal)
    if should_increment:
        mock_counter.assert_called_once()
    else:
        mock_counter.assert_not_called()


def test_enrich_union_reads_split_telemetry_column() -> None:
    """FAR-125 P1b: the union folds wall-clock + model cost from the SPLIT
    telemetry column, not the (now PURE) outputs column."""
    usage = {"node-a": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    outputs = {"node-a": {"summary": "agent summary"}}  # pure return
    telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 3_600_000, "model_cost_usd": 0.04}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    entry = union["node-a"]
    assert entry["wall_clock_time_ms"] == 3_600_000
    assert entry["model_cost_usd"] == pytest.approx(0.04)
    assert entry["is_sandbox_for_wallclock"] is True


def test_enrich_union_schema_drift_detected_from_split_telemetry() -> None:
    """P2b: schema-drift is read DIRECTLY from the telemetry entry (the former
    output_json sub-lookup is gone) — still detected for a split row."""
    outputs = {"node-a": {"summary": "agent summary"}}  # pure return
    telemetry = {"node-a": {"schema_drift": True}}
    with patch("modulo.core.cost_controller.finalize.record_schema_drift") as mock_counter:
        _enrich_union({}, outputs, {"node-a": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    mock_counter.assert_called_once()


# ---------------------------------------------------------------------------
# _fold_token_usage — agent-reported token usage, DISTINCT reported_* keys (FAR-491)
# ---------------------------------------------------------------------------

_REPORTED_TOKENS = {
    "model_tokens_input": 1234,
    "model_tokens_output": 567,
    "model_tokens_total": 1801,
    "model_tokens_cache_read": 100,
    "model_tokens_cache_write": 8,
}


def test_enrich_union_folds_reported_tokens_display_only() -> None:
    """A sandbox node's agent-reported tokens fold into the DISTINCT
    ``reported_*`` union keys while the SERVER token entries stay 0 — the
    reported values never overwrite ``input_tokens`` / ``output_tokens`` /
    ``total_tokens``."""
    usage = {"node-a": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {"node-a": {"summary": "agent summary"}}  # pure return
    telemetry = {"node-a": {"status": "completed", **_REPORTED_TOKENS}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    entry = union["node-a"]
    assert entry["reported_input_tokens"] == 1234
    assert entry["reported_output_tokens"] == 567
    assert entry["reported_total_tokens"] == 1801
    assert entry["reported_cache_read_tokens"] == 100
    assert entry["reported_cache_write_tokens"] == 8
    # Server-measured fields untouched by the fold.
    assert entry["input_tokens"] == 0
    assert entry["output_tokens"] == 0
    assert entry["total_tokens"] == 0
    assert _derive_total_tokens(union) == 0


def test_enrich_union_sandbox_without_report_has_no_reported_keys() -> None:
    """A sandbox node whose telemetry carries NO model_tokens_* fields gets NO
    reported_* keys (never a 0 placeholder)."""
    usage = {"node-a": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {"node-a": {"summary": "agent summary"}}
    telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 1200}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    assert not any(key.startswith("reported_") for key in union["node-a"])


def test_fold_token_usage_output_absent_keeps_stored_reported() -> None:
    """Branch 3 (output ABSENT): the stored-union reported_* values are the
    fallback authority — left untouched, mirroring ``_fold_model_cost``."""
    node_dict = {"reported_input_tokens": 5, "reported_total_tokens": 5}
    _fold_token_usage(node_dict, None)
    assert node_dict["reported_input_tokens"] == 5
    assert node_dict["reported_total_tokens"] == 5


def test_fold_token_usage_output_without_fields_pops_stale_reported() -> None:
    """A node output present but lacking model_tokens_* pops any previously
    folded reported_* values — a stale fold can never survive a re-enrich."""
    node_dict = {"reported_input_tokens": 5, "reported_total_tokens": 5}
    _fold_token_usage(node_dict, {"status": "completed"})
    assert not any(key.startswith("reported_") for key in node_dict)


def test_fold_token_usage_invalid_values_treated_absent() -> None:
    """Tri-state at the fold: bool / non-int / negative stored values are
    treated as ABSENT (popped, never a 0 placeholder); valid 0 is a real
    report."""
    node_dict: dict[str, Any] = {}
    _fold_token_usage(
        node_dict,
        {
            "model_tokens_input": True,
            "model_tokens_output": "many",
            "model_tokens_total": -3,
            "model_tokens_cache_read": 0,
            "model_tokens_cache_write": 4,
        },
    )
    assert "reported_input_tokens" not in node_dict
    assert "reported_output_tokens" not in node_dict
    assert "reported_total_tokens" not in node_dict
    assert node_dict["reported_cache_read_tokens"] == 0
    assert node_dict["reported_cache_write_tokens"] == 4


def test_fold_token_usage_prove_the_fix_end_to_end() -> None:
    """PROVE-THE-FIX: the full chain only the real path exercises — a
    producer output.json ``token_usage`` flows through node_runner envelope
    extraction → the split telemetry view → the union fold → the telemetry
    sum, ending as display-only analytics with server tokens untouched."""
    from modulo.core.cost_controller.breakdown.params import build_telemetry
    from modulo.core.node_output_split import split_node_output
    from modulo.core.pipeline_engine.node_runner import (
        _build_sandbox_node_envelope,
        _SandboxNodeOutput,
    )

    output = _SandboxNodeOutput(
        status="completed",
        summary="did the thing",
        exit_code=0,
        wall_clock_time_ms=1200,
        cost_estimate_usd=0.01,
        cost_source={"token_usage": {"input": 1234, "output": 567, "total": 1801, "cache_read": 100, "cache_write": 8}},
    )
    envelope = _build_sandbox_node_envelope(node_id="node-a", output=output)
    _return_value, telemetry = split_node_output(envelope, "sandbox_agent", None)

    # Sandbox nodes have NO server-measured token entries at all.
    union = _enrich_union(
        {},
        {"node-a": {"summary": "agent summary"}},
        {"node-a": "sandbox_agent"},
        is_terminal=True,
        merged_telemetry={"node-a": telemetry},
    )
    entry = union["node-a"]
    assert entry["input_tokens"] == 0
    assert entry["output_tokens"] == 0
    assert entry["total_tokens"] == 0
    assert entry["reported_input_tokens"] == 1234
    assert entry["reported_output_tokens"] == 567
    assert entry["reported_total_tokens"] == 1801
    assert entry["reported_cache_read_tokens"] == 100
    assert entry["reported_cache_write_tokens"] == 8

    tele, per_node_cost = build_telemetry(union, [])
    assert tele.tokens_input_reported == 1234
    assert tele.tokens_output_reported == 567
    assert tele.tokens_total_reported == 1801
    assert tele.tokens_cache_read_reported == 100
    assert tele.tokens_cache_write_reported == 8
    assert tele.tokens_input == 0
    assert tele.tokens_output == 0
    assert tele.tokens_estimated == 0
    assert per_node_cost["node-a"] == Decimal(0)


# ---------------------------------------------------------------------------
# _write_back_node_cost / _derive_total_tokens / derive_node_type_map
# ---------------------------------------------------------------------------


def test_write_back_node_cost_single_authority() -> None:
    enriched = {"node-a": {}}
    per_node_cost = {"node-a": Decimal("0.1332")}
    result = _write_back_node_cost(enriched, per_node_cost)
    assert result["node-a"]["cost_usd"] == pytest.approx(0.1332)


def test_derive_total_tokens_server_measured_only() -> None:
    """FAR-491 evolved pin: a sandbox node contributes 0 SERVER tokens (the
    union's ``input/output/total_tokens`` stay server-measured) — but its
    agent-reported ``reported_*`` fields are populated in the union and are
    IGNORED by ``_derive_total_tokens`` (display-only)."""
    enriched = {
        "node-a": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "node-b": {  # sandbox node: server tokens 0, agent-reported tokens real
            "reported_input_tokens": 100,
            "reported_output_tokens": 50,
            "reported_total_tokens": 150,
            "reported_cache_read_tokens": 20,
            "reported_cache_write_tokens": 4,
        },
    }
    assert _derive_total_tokens(enriched) == 15


def test_derive_node_type_map_reads_graph_nodes() -> None:
    graph = {"nodes": [{"id": "a", "node_type": "sandbox_agent"}, {"id": "b", "node_type": "agent"}]}
    assert derive_node_type_map(graph) == {"a": "sandbox_agent", "b": "agent"}


def test_derive_node_type_map_absent_type_defaults_empty() -> None:
    graph = {"nodes": [{"id": "a"}]}
    assert derive_node_type_map(graph) == {"a": ""}


# ---------------------------------------------------------------------------
# The legacy fallback — DE-TRUSTS cost_estimate_usd
# ---------------------------------------------------------------------------


def test_legacy_sandbox_cost_de_trusts_cost_estimate_usd() -> None:
    """The fallback total is SERVER-VERIFIED wall-clock ONLY — a hostile
    cost_estimate_usd contributes NOTHING (§1.5)."""
    outputs = {"node-a": {"output": {"wall_clock_time_ms": 3_600_000, "cost_estimate_usd": 99999.0}}}
    with patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.1332")):
        cost = _legacy_sandbox_cost(outputs)
    assert cost == Decimal("0.1332")


def test_token_cost_server_measured() -> None:
    usage = {"node-a": {"input_tokens": 100, "output_tokens": 100}}
    assert _token_cost(usage) == Decimal("0.001") + Decimal("0.003")


# ---------------------------------------------------------------------------
# finalize_cost — the pre-component-read terminal + the never-fail fallback
# ---------------------------------------------------------------------------


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


async def test_finalize_cost_pre_component_read_writes_zero_total() -> None:
    """A run with NO accumulated sets finalizes total 0, breakdown NULL, no ledger."""
    run = _make_run(started_at=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs:
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="failed",
            segment_node_token_usage=None,
            segment_completed_node_outputs=None,
            node_type_map={},
            is_terminal=True,
        )
    mock_urs.assert_awaited_once()
    kwargs = mock_urs.await_args.kwargs
    assert kwargs["total_cost_usd"] == Decimal(0)
    assert kwargs["total_tokens"] == 0


async def test_finalize_cost_stalled_persists_stalled_status() -> None:
    """A stalled run finalizes through update_run_status with status='stalled'
    (not 'complete'). Coupled with the persistence-layer test that drives the
    real update_run_status, this covers the full stalled write path."""
    run = _make_run(started_at=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs:
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="stalled",
            segment_node_token_usage=None,
            segment_completed_node_outputs=None,
            node_type_map={},
            is_terminal=True,
        )
    mock_urs.assert_awaited_once()
    assert mock_urs.await_args.args[2] == "stalled"


async def test_finalize_cost_fallback_de_trusts_cost_estimate_usd() -> None:
    """A cost-path exception degrades to the legacy fallback — wall-clock only.

    The fixture is the SPLIT two-column shape (FAR-125 P1b): the pure return
    lives in ``outputs_json`` and the exhaustive telemetry (incl. wall-clock +
    a hostile ``cost_estimate_usd``) in ``node_telemetry_json``.
    """
    stored_outputs = {"node-a": {"summary": "did the thing"}}
    stored_telemetry = {"node-a": {"wall_clock_time_ms": 3_600_000, "cost_estimate_usd": 99999.0}}
    run = _make_run(outputs_json=stored_outputs, node_telemetry_json=stored_telemetry)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.load_live_components",
            side_effect=RuntimeError("boom"),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.1332")),
        # This test exercises the fallback cost calc, not the ledger block
        # (covered by test_finalize_cost_fallback_runs_ledger_block).
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="failed",
            segment_node_token_usage=None,
            segment_completed_node_outputs=run.outputs_json,
            node_type_map={},
            is_terminal=True,
        )
    kwargs = mock_urs.await_args.kwargs
    assert kwargs["total_cost_usd"] == Decimal("0.1332")
    # The fallback persists the UN-ENRICHED merged set (cumulative write-back invariant).
    assert not kwargs["node_token_usage"]
    # Both output columns are written SHAPE-IDENTICAL — never an un-split envelope.
    assert kwargs["outputs_json"] == stored_outputs
    assert kwargs["node_telemetry_json"] == stored_telemetry
    # The fallback's breakdown is flat-clamped with the shared marker when over the cap.
    assert kwargs["cost_breakdown"]


async def test_finalize_cost_fallback_runs_ledger_block() -> None:
    """FAR-391 regression — the never-fail legacy fallback MUST still run the
    terminal ledger block (spend-ceiling gate + org accrual), not skip it.

    A cost-path exception degrades to the legacy fallback; the ledger block is
    asserted to be invoked with the fallback-derived total so a breached ceiling
    is still refused on the legacy path.
    """
    stored_outputs = {"node-a": {"summary": "did the thing"}}
    stored_telemetry = {"node-a": {"wall_clock_time_ms": 3_600_000}}
    run = _make_run(outputs_json=stored_outputs, node_telemetry_json=stored_telemetry)
    run.owner_team_id = uuid.uuid4()
    run.cancellation_requested = False
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.load_live_components",
            side_effect=RuntimeError("boom"),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.1332")),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()) as mock_block,
    ):
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=run.outputs_json,
            node_type_map={},
            is_terminal=True,
        )
    mock_block.assert_awaited_once()
    assert mock_block.await_args.kwargs["total"] == Decimal("0.1332")
    # The fallback path passes through the run's terminal status.
    assert mock_block.await_args.kwargs["status"] == "complete"


# ---------------------------------------------------------------------------
# finalize_cancelled_run — the cancellation classes (§4.2)
# ---------------------------------------------------------------------------


async def test_finalize_cancelled_run_never_paused_forfeits_accrued_cost() -> None:
    """A never-paused in-flight run cancelled cross-process has NO stored sets;
    its accrued cost is forfeited and only the partial_spend_lost log fires."""
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with patch("modulo.core.cost_controller.finalize._log") as mock_log:
        await finalize_cancelled_run(session, run_id=run.id, org_id=_ORG_ID)
    logged = [str(c) for c in mock_log.warning.call_args_list]
    assert any("cost_components_partial_spend_lost" in line for line in logged)


async def test_finalize_cancelled_run_streamed_with_prior_pause_finalizes() -> None:
    """A streamed run that HAS PAUSED has stored cumulative sets -> finalize_cost
    is invoked with the STORED outputs as the segment (DATA SOURCE PINNED). The
    split telemetry source is read from the stored ``node_telemetry_json``
    column inside finalize_cost (FAR-125 P1b), so already-pure rows stay
    idempotent in the cancel path."""
    stored_usage = {"node-a": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}}
    stored_outputs = {"node-a": {"output": {"status": "completed", "wall_clock_time_ms": 1000}}}
    stored_telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 1000}}
    run = _make_run(
        node_token_usage=stored_usage,
        outputs_json=stored_outputs,
        node_telemetry_json=stored_telemetry,
        snapshot_id=uuid.uuid4(),
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=run)),  # the run row
            MagicMock(scalar_one_or_none=MagicMock(return_value={"nodes": [{"id": "node-a"}]})),  # graph_json
        ]
    )
    with patch("modulo.core.cost_controller.finalize.finalize_cost", new=AsyncMock()) as mock_finalize:
        await finalize_cancelled_run(session, run_id=run.id, org_id=_ORG_ID)
    mock_finalize.assert_awaited_once()
    kwargs = mock_finalize.await_args.kwargs
    assert kwargs["status"] == "cancelled"
    assert kwargs["segment_node_token_usage"] == stored_usage
    assert kwargs["segment_completed_node_outputs"] == stored_outputs
    assert kwargs["is_terminal"] is True
