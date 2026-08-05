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
    _legacy_sandbox_cost,
    _merge,
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
    assert union["node-a"]["model_cost_usd"] == 0.0412
    assert union["node-a"]["model_cost_raw_usd"] == 0.0412


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


# ---------------------------------------------------------------------------
# _write_back_node_cost / _derive_total_tokens / derive_node_type_map
# ---------------------------------------------------------------------------


def test_write_back_node_cost_single_authority() -> None:
    enriched = {"node-a": {}}
    per_node_cost = {"node-a": Decimal("0.1332")}
    result = _write_back_node_cost(enriched, per_node_cost)
    assert result["node-a"]["cost_usd"] == 0.1332


def test_derive_total_tokens_server_measured_only() -> None:
    enriched = {
        "node-a": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "node-b": {},  # sandbox node contributes 0
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


async def test_finalize_cost_fallback_de_trusts_cost_estimate_usd() -> None:
    """A cost-path exception degrades to the legacy fallback — wall-clock only."""
    run = _make_run(
        outputs_json={"node-a": {"output": {"wall_clock_time_ms": 3_600_000, "cost_estimate_usd": 99999.0}}}
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.load_live_components",
            side_effect=RuntimeError("boom"),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status") as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.1332")),
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
    assert kwargs["node_token_usage"] == {}
    assert kwargs["outputs_json"] == run.outputs_json
    # The fallback's breakdown is flat-clamped with the shared marker when over the cap.
    assert kwargs["cost_breakdown"]


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
    is invoked with the STORED sets (DATA SOURCE PINNED)."""
    stored_usage = {"node-a": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}}
    stored_outputs = {"node-a": {"output": {"status": "completed", "wall_clock_time_ms": 1000}}}
    run = _make_run(node_token_usage=stored_usage, outputs_json=stored_outputs, snapshot_id=uuid.uuid4())
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
