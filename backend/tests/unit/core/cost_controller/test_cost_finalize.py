"""Unit tests for the executor finalize block + ledger (PR A2).

Covers ``_merge`` (segment-wins / empty-accumulator normalization), the
ENRICHED-union construction (the SPLIT sandbox signal, the ONE-mechanism
stored-union rule, the resume-of-stored-unclamped band clamp, the schema-drift
gate), the server-measured token derivation, the legacy-fallback DE-TRUSTS
``cost_estimate_usd`` rule, the pre-component-read terminal transition, and the
``finalize_cancelled_run`` cancellation classes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller.breakdown.constants import (
    COST_COLUMN_CAP,
    MAX_REPORTABLE_BAND_USD,
    TOTAL_CLAMPED_MARKER,
)
from modulo.core.cost_controller.breakdown.params import (
    MAX_REPORTABLE_TOKEN_COUNT,
    REPORTED_TOKEN_CHAIN,
)
from modulo.core.cost_controller.finalize import (
    _REPORTED_TOKEN_FIELD_MAP,
    _attributed_cost_present,
    _derive_total_tokens,
    _enrich_union,
    _fold_model_cost,
    _fold_reported_token_fallback,
    _fold_stored_clamped,
    _fold_token_usage,
    _has_sandbox_attempt_evidence,
    _legacy_sandbox_cost,
    _merge,
    _node_output_dict,
    _record_node_schema_drift,
    _salvage_from_markers,
    _seed_union,
    _split_merge_outputs,
    _token_cost,
    _wall_clock_ms,
    _write_back_node_cost,
    derive_node_type_map,
    finalize_cancelled_run,
    finalize_cost,
    finalize_failed_run,
    salvage_cancelled_run_cost,
)
from modulo.db.crud.run_node_outputs import RunBlobs

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


_PROVEN_ZERO_NODE_OUTPUT = {
    "model_cost_usd": 0.0,
    "model_cost_raw_usd": 0.0,
    "model_cost_clamped": False,
    "model_cost_out_of_band_high": False,
    "model_tokens_input": 0,
    "model_tokens_output": 0,
    "model_tokens_total": 0,
}


def test_enrich_union_folds_proven_zero_report() -> None:
    """FAR-653: a node output carrying an exact-zero cost WITH the all-zero
    ``model_tokens_*`` proof folds as a REAL report — the union keeps 0.0."""
    usage = {"node-a": {"model_cost_usd": 5.0}}
    outputs = {"node-a": {"output": dict(_PROVEN_ZERO_NODE_OUTPUT)}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=False)
    entry = union["node-a"]
    assert entry["model_cost_usd"] == 0.0
    assert entry["model_cost_raw_usd"] == 0.0
    assert entry["model_cost_clamped"] is False
    assert entry["model_cost_out_of_band_high"] is False
    assert entry["reported_input_tokens"] == 0
    assert entry["reported_output_tokens"] == 0
    assert entry["reported_total_tokens"] == 0


@pytest.mark.parametrize(
    "output",
    [
        {"model_cost_usd": 0.0, "model_cost_raw_usd": 0.0},  # no token fields — unproven
        {  # a non-zero reported token count — unproven
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "model_tokens_input": 0,
            "model_tokens_output": 5,
            "model_tokens_total": 5,
        },
        {  # mandatory node field absent — unproven
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "model_tokens_input": 0,
            "model_tokens_output": 0,
        },
    ],
)
def test_enrich_union_pops_unproven_zero_fold(output: dict) -> None:
    """FAR-653: an unproven zero is NOT a report — the fold pops the fields."""
    usage = {"node-a": {"model_cost_usd": 5.0, "model_cost_raw_usd": 5.0}}
    outputs = {"node-a": {"output": output}}
    union = _enrich_union(usage, outputs, {"node-a": "sandbox_agent"}, is_terminal=False)
    for key in ("model_cost_usd", "model_cost_raw_usd", "model_cost_clamped", "model_cost_out_of_band_high"):
        assert key not in union["node-a"]


def test_enrich_union_keeps_stored_genuine_zero_without_outputs() -> None:
    """FAR-653 branch (3): a stored exact zero is server-written (only the
    validated proven-zero fold writes one) — a re-enrich of an outputs-pruned
    run keeps the genuine-zero report instead of resurrecting the warning."""
    usage = {
        "node-a": {
            "model_cost_usd": 0.0,
            "model_cost_clamped": False,
            "model_cost_out_of_band_high": False,
        }
    }
    union = _enrich_union(usage, {}, {"node-a": "sandbox_agent"}, is_terminal=False)
    assert union["node-a"]["model_cost_usd"] == 0.0
    assert union["node-a"]["model_cost_clamped"] is False
    assert union["node-a"]["model_cost_out_of_band_high"] is False


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


# ---------------------------------------------------------------------------
# _fold_datum_provenance — envelope per-datum classes propagate (FAR-1780)
# ---------------------------------------------------------------------------


_PROV_BASE_USAGE = {"node-a": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}


def test_enrich_union_propagates_envelope_datum_provenance() -> None:
    """A sandbox envelope's per-datum ``reported`` map propagates onto the
    union's corresponding folded keys (token datums via the shared chain,
    the model-cost datum onto its pinned keys) — springing from BOTH views
    (split telemetry, legacy inner output)."""
    telemetry = {
        "node-a": {
            "status": "completed",
            "model_cost_usd": 0.04,
            **_REPORTED_TOKENS,
            "provenance": {
                "model_cost_usd": "reported",
                "model_tokens_input": "reported",
                "model_tokens_total": "reported",
            },
        }
    }
    union = _enrich_union(_PROV_BASE_USAGE, {}, {"node-a": "sandbox_agent"}, merged_telemetry=telemetry)
    entry = union["node-a"]
    # model_cost_raw_usd is NOT stamped: the report carried no raw field, the
    # fold pops it, and a stamp rides only a SURVIVING value.
    assert entry["provenance"] == {
        "model_cost_usd": "reported",
        "reported_input_tokens": "reported",
        "reported_total_tokens": "reported",
    }


def test_enrich_union_propagates_from_legacy_inner_output() -> None:
    """Legacy (non-split) rows carry the map inside the envelope's inner
    output — the same propagation applies with no telemetry column."""
    outputs = {
        "node-a": {
            "status": "completed",
            "wall_clock_time_ms": 1500,
            "model_cost_usd": 0.04,
            "model_tokens_input": 1234,
            "provenance": {"model_cost_usd": "reported", "model_tokens_input": "reported"},
        }
    }
    union = _enrich_union(_PROV_BASE_USAGE, outputs, {"node-a": "sandbox_agent"})
    entry = union["node-a"]
    # No raw datum declared -> no raw fold -> no raw stamp.
    assert entry["provenance"] == {"model_cost_usd": "reported", "reported_input_tokens": "reported"}


def test_enrich_union_refused_report_is_not_stamped() -> None:
    """An unproven-zero model-cost report is REFUSED by the fold — and the
    refused value's key is NOT stamped as the agent's claim (the stamp
    follows the SURVIVING value, checked post-fold)."""
    telemetry = {
        "node-a": {
            "status": "completed",
            "model_cost_usd": 0.0,
            "provenance": {"model_cost_usd": "reported", "model_tokens_input": "reported"},
        }
    }
    union = _enrich_union(_PROV_BASE_USAGE, {}, {"node-a": "sandbox_agent"}, merged_telemetry=telemetry)
    entry = union["node-a"]
    assert "model_cost_usd" not in entry
    assert "provenance" not in entry


def test_enrich_union_hostile_class_value_is_not_stamped() -> None:
    """A producer cannot inject a provenance class map with a foreign value —
    only the EXACT ``reported`` datum value propagates (engine-assigned,
    never self-asserted)."""
    telemetry = {
        "node-a": {
            "status": "completed",
            "model_cost_usd": 0.04,
            "provenance": {"model_cost_usd": "attested"},
        }
    }
    union = _enrich_union(_PROV_BASE_USAGE, {}, {"node-a": "sandbox_agent"}, merged_telemetry=telemetry)
    assert "provenance" not in union["node-a"]


def test_enrich_union_stale_stored_provenance_is_popped() -> None:
    """The stored union NEVER carries a provenance class across enriches —
    replace semantics: a stored map is popped and rebuilt only from THIS
    enrich's source."""
    usage = {
        "node-a": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_input_tokens": 4,
            "provenance": {"reported_input_tokens": "reported"},
        }
    }
    telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 900}}
    union = _enrich_union(usage, {}, {"node-a": "sandbox_agent"}, merged_telemetry=telemetry)
    assert "provenance" not in union["node-a"]


def test_enrich_union_stamp_absent_when_no_actual_datum_survives() -> None:
    """A map stamping only engine-set datums (clamped/out-of-band booleans
    are NOT mapped — engine-authored) with no surviving mapped value
    produces NO provenance key at all."""
    telemetry = {
        "node-a": {
            "status": "completed",
            "wall_clock_time_ms": 900,
            "provenance": {"model_cost_clamped": "reported", "summary": "reported"},
        }
    }
    union = _enrich_union(_PROV_BASE_USAGE, {}, {"node-a": "sandbox_agent"}, merged_telemetry=telemetry)
    assert "provenance" not in union["node-a"]


def test_fold_token_usage_output_absent_keeps_stored_reported() -> None:
    """Branch 3 (output ABSENT): the stored-union reported_* values are the
    fallback authority — left untouched when valid, mirroring
    ``_fold_model_cost``."""
    node_dict = {"reported_input_tokens": 5, "reported_total_tokens": 5}
    _fold_token_usage(node_dict, None)
    assert node_dict["reported_input_tokens"] == 5
    assert node_dict["reported_total_tokens"] == 5


def test_fold_token_usage_output_absent_revalidates_stored_reported() -> None:
    """Branch 3 defence (FAR-532 wave-2): when the output is ABSENT the
    stored-union reported_* values are RE-VALIDATED tri-state (the mirror of
    ``_fold_stored_clamped``'s re-clamp) — an invalid stored value is popped,
    never carried into analytics; a valid 0 is a real report and stays."""
    node_dict: dict[str, Any] = {
        "reported_input_tokens": True,
        "reported_output_tokens": "many",
        "reported_total_tokens": -3,
        "reported_cache_read_tokens": 0,
        "reported_cache_write_tokens": 4,
    }
    _fold_token_usage(node_dict, None)
    assert "reported_input_tokens" not in node_dict
    assert "reported_output_tokens" not in node_dict
    assert "reported_total_tokens" not in node_dict
    assert node_dict["reported_cache_read_tokens"] == 0
    assert node_dict["reported_cache_write_tokens"] == 4


def test_fold_token_usage_rejects_above_ceiling_values() -> None:
    """FAR-532 wave-2: the plausibility ceiling applies at the fold — a
    pathological 10**18 stored value is popped on re-validation and an
    above-ceiling output value is omitted tri-state (not clamped)."""
    stored: dict[str, Any] = {"reported_total_tokens": 10**18}
    _fold_token_usage(stored, None)
    assert not stored
    folded: dict[str, Any] = {}
    _fold_token_usage(folded, {"model_tokens_total": 10**18})
    assert not folded


# ---------------------------------------------------------------------------
# FAR-1033 — the agent-reported -> canonical token fallback
# ---------------------------------------------------------------------------


def test_reported_token_fallback_populates_sandbox_canonical_counters() -> None:
    """FAR-1033 regression: a sandbox node the server never measured (LLM
    calls ran inside the agent) contributes 0 to the SERVER tokens — after
    ``_fold_reported_token_fallback`` its canonical counters carry the
    agent-reported NON-ZERO values, and ``_derive_total_tokens`` returns the
    non-zero total (this is the assertion that FAILED before the fix: the
    canonical counters and ``Run.total_tokens`` used to stay 0 while
    ``cost_breakdown.tokens_*_reported`` carried the truth)."""
    enriched = {
        "node-a": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_input_tokens": 1234,
            "reported_output_tokens": 567,
            "reported_total_tokens": 1801,
            "reported_cache_read_tokens": 100,
            "reported_cache_write_tokens": 8,
        }
    }
    _fold_reported_token_fallback(enriched)
    assert enriched["node-a"]["input_tokens"] == 1234
    assert enriched["node-a"]["output_tokens"] == 567
    assert enriched["node-a"]["total_tokens"] == 1801
    assert _derive_total_tokens(enriched) == 1801


def test_reported_token_fallback_never_overwrites_server_measured() -> None:
    """FAR-1033 trust boundary: a node the server DID measure (any canonical
    counter non-zero) keeps its server values — the reported_* keys stay
    display-only and are never folded over real measurements."""
    enriched = {
        "node-a": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "reported_input_tokens": 100,
            "reported_output_tokens": 50,
            "reported_total_tokens": 150,
        }
    }
    _fold_reported_token_fallback(enriched)
    entry = enriched["node-a"]
    assert entry["input_tokens"] == 10
    assert entry["output_tokens"] == 5
    assert entry["total_tokens"] == 15
    assert _derive_total_tokens(enriched) == 15


def test_reported_token_fallback_revalidates_reported_values() -> None:
    """FAR-1033: the fallback re-validates reported values tri-state (the
    ``coerce_reported_token`` rule) — bool / non-numeric / negative /
    above-ceiling values are NOT folded into the canonical counters."""
    enriched = {
        "node-a": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_input_tokens": True,
            "reported_output_tokens": "many",
            "reported_total_tokens": -3,
        },
        "node-b": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_input_tokens": 1,
            "reported_output_tokens": 2,
            "reported_total_tokens": 3,
        },
        "node-c": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_input_tokens": MAX_REPORTABLE_TOKEN_COUNT + 1,
            "reported_output_tokens": MAX_REPORTABLE_TOKEN_COUNT + 1,
            "reported_total_tokens": MAX_REPORTABLE_TOKEN_COUNT + 1,
        },
    }
    _fold_reported_token_fallback(enriched)
    entry_a = enriched["node-a"]
    assert entry_a["input_tokens"] == 0
    assert entry_a["output_tokens"] == 0
    assert entry_a["total_tokens"] == 0
    entry_b = enriched["node-b"]
    assert entry_b["input_tokens"] == 1
    assert entry_b["output_tokens"] == 2
    assert entry_b["total_tokens"] == 3
    entry_c = enriched["node-c"]
    assert entry_c["input_tokens"] == 0
    assert entry_c["output_tokens"] == 0
    assert entry_c["total_tokens"] == 0
    assert _derive_total_tokens(enriched) == 3


def test_reported_token_fallback_keeps_proven_zero_report() -> None:
    """FAR-1033: a valid agent-reported 0 is a REAL report (proven-zero) and
    stays — the fallback must not turn it into a phantom non-zero total."""
    enriched = {
        "node-a": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_input_tokens": 0,
            "reported_output_tokens": 0,
            "reported_total_tokens": 0,
        }
    }
    _fold_reported_token_fallback(enriched)
    assert _derive_total_tokens(enriched) == 0


def test_reported_token_field_map_derives_from_shared_chain() -> None:
    """FAR-532 wave-2: the fold map is DERIVED from the shared
    ``REPORTED_TOKEN_CHAIN`` in (src, dst) reading order — matching
    node_runner's extraction map, so the layers cannot drift apart."""
    assert tuple((b.node_field, b.union_key) for b in REPORTED_TOKEN_CHAIN) == _REPORTED_TOKEN_FIELD_MAP


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


def _sandbox_attempt_markers() -> dict[str, Any]:
    """One retained per-attempt SANDBOX marker — the billable-cost gate's proof.

    Defined as a callable (not a module constant) because ``_step_finish_marker``
    is declared further down this module: Python resolves the name at call
    time, so test bodies can use this regardless of position.
    """
    return {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker(cost=None)}


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
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=stored_outputs, telemetry=stored_telemetry, markers=None)),
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
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=stored_outputs, telemetry=stored_telemetry, markers=None)),
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


async def test_finalize_cost_sandbox_reported_tokens_populate_run_counters() -> None:
    """FAR-1033 end-to-end regression: a terminal run whose ONLY token data is
    the agent-reported usage (a sandbox node — the server measured nothing,
    ``node_token_usage`` has no usage entries) must finalize with NON-ZERO
    canonical node counters AND a non-zero ``total_tokens`` persisted through
    ``update_run_status``. Before the fix both were ``0`` while the
    enrichment carried the correct ``reported_*`` values."""
    stored_outputs = {"node-a": {"summary": "agent summary"}}
    reported = {
        "status": "completed",
        "model_tokens_input": 1234,
        "model_tokens_output": 567,
        "model_tokens_total": 1801,
    }
    stored_telemetry = {"node-a": dict(reported)}
    run = _make_run(
        node_token_usage=None,
        outputs_json=stored_outputs,
        node_telemetry_json=stored_telemetry,
        snapshot_id=uuid.uuid4(),
    )
    run.cancellation_requested = False
    run.pipeline_id = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=stored_outputs, telemetry=stored_telemetry, markers=None)),
        ),
        patch("modulo.core.cost_controller.finalize.load_live_components", new=AsyncMock(return_value=[])),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
        patch("modulo.core.cost_controller.finalize._enforce_agent_token_budgets", new=AsyncMock(return_value=None)),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._advance_journeys_on_terminal", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize.record_run_facts", new=AsyncMock()),
    ):
        await finalize_cost(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=stored_outputs,
            node_type_map={"node-a": "sandbox_agent"},
            is_terminal=True,
        )
    kwargs = mock_urs.await_args.kwargs
    node_a = kwargs["node_token_usage"]["node-a"]
    assert node_a["input_tokens"] == 1234
    assert node_a["output_tokens"] == 567
    assert node_a["total_tokens"] == 1801
    assert kwargs["total_tokens"] == 1801


# ---------------------------------------------------------------------------
# finalize_cancelled_run — the cancellation classes (§4.2)
# ---------------------------------------------------------------------------


async def test_finalize_cancelled_run_never_paused_defers_salvage_not_forfeits() -> None:
    """A never-paused in-flight run cancelled cross-process has NO stored sets.

    Round-2 minor 1: this is a NORMAL deferral, not a forfeiture — the request
    path cannot see the executor's marker yet, so it records
    ``cost_components.cancel_salvage_deferred`` and leaves the attribution to
    the post-settle salvage. The forfeiture diagnostic
    (``cost_components_partial_spend_lost``) is reserved for a terminal path
    that genuinely cannot attribute anything, so a real forfeiture stays
    distinguishable in the log stream.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with patch("modulo.core.cost_controller.finalize._log") as mock_log:
        await finalize_cancelled_run(session, run_id=run.id, org_id=_ORG_ID)
    logged = [str(c) for c in mock_log.info.call_args_list]
    assert any("cost_components.cancel_salvage_deferred" in line for line in logged)
    warned = [str(c) for c in mock_log.warning.call_args_list]
    assert not any("cost_components_partial_spend_lost" in line for line in warned)


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
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=stored_outputs, telemetry=stored_telemetry, markers=None)),
        ),
        patch("modulo.core.cost_controller.finalize.finalize_cost", new=AsyncMock()) as mock_finalize,
    ):
        await finalize_cancelled_run(session, run_id=run.id, org_id=_ORG_ID)
    mock_finalize.assert_awaited_once()
    kwargs = mock_finalize.await_args.kwargs
    assert kwargs["status"] == "cancelled"
    assert kwargs["segment_node_token_usage"] == stored_usage
    assert kwargs["segment_completed_node_outputs"] == stored_outputs
    assert kwargs["is_terminal"] is True


# ---------------------------------------------------------------------------
# finalize_failed_run — the node-deadline DERIVED cost (FAR-1993)
# ---------------------------------------------------------------------------


async def test_finalize_failed_run_derived_hours_book_run_level_cost() -> None:
    """An empty segment + derived wall-clock books DERIVED sandbox spend.

    A deadline-killed sandbox node never wrote ``output.json``, so nothing is
    in the stored sets. With ``derived_sandbox_wall_hours`` the run must still
    carry a non-zero cost, explicitly marked ``source="derived"``. Without the
    derived branch the write would be total 0 (or nothing at all).

    The derived spend must ALSO reach the terminal ledger block (spend ceiling
    + ``org_daily_run_counts`` + circuit breaker) — not just the run row and the
    analytics fact — otherwise the controls the ticket names stay blind.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=_sandbox_attempt_markers())),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()) as mock_facts,
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()) as mock_block,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )
    mock_urs.assert_awaited_once()
    kwargs = mock_urs.await_args.kwargs
    assert mock_urs.await_args.args[2] == "failed"
    assert kwargs["error_code"] == "node_deadline_exceeded"
    assert kwargs["error_detail"] == "node 'n1' exceeded its deadline"
    assert kwargs["total_cost_usd"] == Decimal("0.065")
    entries = kwargs["cost_breakdown"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["component"] == "sandbox_infra"
    assert entry["source"] == "derived"
    assert entry["basis"]["derived_from"] == "node_deadline_watchdog"
    assert entry["basis"]["wall_clock_hours"] == 0.5
    mock_facts.assert_awaited_once()
    # The ledger block receives the SAME persisted total, org and run date.
    mock_block.assert_awaited_once()
    ledger_kwargs = mock_block.await_args.kwargs
    assert ledger_kwargs["total"] == Decimal("0.065")
    assert ledger_kwargs["org_id"] == _ORG_ID
    assert ledger_kwargs["status"] == "failed"
    assert ledger_kwargs["owner_team_id"] == run.owner_team_id
    assert ledger_kwargs["run_date"] == run.started_at.astimezone(UTC).date()
    assert ledger_kwargs["finalize_fields"]["total_cost_usd"] == Decimal("0.065")


async def test_finalize_failed_run_empty_segment_without_derived_hours_invents_no_cost() -> None:
    """No stored sets AND no derived duration: nothing is attributed to the run.

    Pins the no-regression case — the pre-fix partial-spend-lost outcome is
    unchanged and no synthetic cost is invented.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=None)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=None,
        )
    mock_urs.assert_not_awaited()
    logged = [str(c) for c in mock_log.warning.call_args_list]
    assert any("cost_components_partial_spend_lost" in line for line in logged)


async def test_finalize_failed_run_with_real_segment_ignores_derived_hours() -> None:
    """A run WITH stored node data routes through the normal component build.

    The derived shortcut must not run (the node's own server-verified
    wall-clock is the authority) and the failed status + error fields are
    forwarded to ``finalize_cost``.
    """
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
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=stored_outputs, telemetry=stored_telemetry, markers=None)),
        ),
        patch("modulo.core.cost_controller.finalize.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch(
            "modulo.core.cost_controller.finalize._write_derived_deadline_terminal",
            new=AsyncMock(),
        ) as mock_derived,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )
    mock_derived.assert_not_awaited()
    mock_finalize.assert_awaited_once()
    kwargs = mock_finalize.await_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == "node_deadline_exceeded"
    assert kwargs["error_detail"] == "node 'n1' exceeded its deadline"
    assert kwargs["segment_node_token_usage"] == stored_usage
    assert kwargs["segment_completed_node_outputs"] == stored_outputs
    assert kwargs["is_terminal"] is True


def test_finalize_failed_run_derived_from_default_is_deadline_watchdog() -> None:
    """Regression pin: the default provenance label is the deadline watchdog.

    PR #2044's deadline path calls ``finalize_failed_run`` without
    ``derived_from``; its recorded breakdown basis must stay
    ``"node_deadline_watchdog"``. A changed default silently relabels every
    deadline-derived cost, so the default is pinned directly.
    """
    default = inspect.signature(finalize_failed_run).parameters["derived_from"].default
    assert default == "node_deadline_watchdog"


async def test_finalize_failed_run_threads_supplied_derived_from() -> None:
    """A caller-supplied ``derived_from`` is what lands in the breakdown basis.

    The reconcile age gate attributes from the heartbeat window, not the node
    deadline, so the label must follow the caller — hardcoding the deadline
    label would misattribute that spend.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=_sandbox_attempt_markers())),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="no_progress",
            error_detail="No node progress within the 135m age bound",
            derived_sandbox_wall_hours=1.0,
            derived_from="reconcile_age_gate_heartbeat",
        )
    entry = mock_urs.await_args.kwargs["cost_breakdown"][0]
    assert entry["basis"]["derived_from"] == "reconcile_age_gate_heartbeat"
    assert entry["basis"]["wall_clock_hours"] == 1.0


class _SqlstateError(Exception):
    """A stand-in DBAPI error carrying a SQLSTATE (no SQLAlchemy class)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate={sqlstate}")
        self.sqlstate = sqlstate


def _empty_derived_session(run: MagicMock | None) -> AsyncMock:
    """A session whose single execute returns *run* (or None for a missing row)."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    return session


async def test_finalize_failed_run_clamps_derived_cost_to_the_column_cap() -> None:
    """An implausibly long derived duration is clamped to the Numeric(14,6) cap.

    The clamp marker must lead the breakdown and the CLAMPED total — not the
    unbounded product — is what reaches the run row and the spend ledger.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = _empty_derived_session(run)
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=_sandbox_attempt_markers())),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()) as mock_block,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=1e18,
        )
    kwargs = mock_urs.await_args.kwargs
    assert kwargs["total_cost_usd"] == COST_COLUMN_CAP
    breakdown = kwargs["cost_breakdown"]
    assert breakdown[0] == TOTAL_CLAMPED_MARKER
    assert breakdown[1]["amount_usd"] == format(COST_COLUMN_CAP, "f")
    assert mock_block.await_args.kwargs["total"] == COST_COLUMN_CAP


async def test_finalize_failed_run_without_started_at_writes_no_ledger_row() -> None:
    """A derived total with no ``started_at`` has no run date, so no ledger row
    is written — the run row still records the derived spend."""
    run = _make_run(node_token_usage=None, outputs_json=None, started_at=None)
    session = _empty_derived_session(run)
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=_sandbox_attempt_markers())),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()) as mock_block,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )
    mock_urs.assert_awaited_once()
    mock_block.assert_not_awaited()


async def test_finalize_failed_run_rereises_cancelled_from_the_ledger_block() -> None:
    """Worker shutdown is not ours to swallow: a CancelledError raised by the
    ledger block propagates out of both envelopes."""
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = _empty_derived_session(run)
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=_sandbox_attempt_markers())),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch(
            "modulo.core.cost_controller.finalize._ledger_block",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )


async def test_finalize_failed_run_rereises_lock_abort_from_the_ledger_block() -> None:
    """A genuine lock abort aborts the whole transaction, so BOTH envelopes must
    re-raise it to the ownership layer rather than log it as a lost write."""
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = _empty_derived_session(run)
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=_sandbox_attempt_markers())),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch(
            "modulo.core.cost_controller.finalize._ledger_block",
            new=AsyncMock(side_effect=_SqlstateError("40P01")),
        ),
        pytest.raises(_SqlstateError),
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )


async def test_finalize_failed_run_swallows_non_lock_ledger_error() -> None:
    """A non-lock ledger failure is logged and swallowed: the run row and the
    analytics fact still land, and nothing is raised to the caller."""
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = _empty_derived_session(run)
    ledger = AsyncMock(side_effect=RuntimeError("ledger boom"))
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=_sandbox_attempt_markers())),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()) as mock_facts,
        patch("modulo.core.cost_controller.finalize._ledger_block", new=ledger),
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )
    ledger.assert_awaited_once()
    mock_facts.assert_awaited_once()
    assert mock_log.exception.call_args.args[0] == "cost_ledger.derived_deadline_block_failed"


async def test_finalize_failed_run_missing_run_writes_nothing() -> None:
    """A run row that no longer exists is logged and skipped — no cost write."""
    session = _empty_derived_session(None)
    with (
        patch("modulo.core.cost_controller.finalize.read_run_blobs", new=AsyncMock()) as mock_blobs,
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_failed_run(
            session,
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )
    mock_blobs.assert_not_awaited()
    mock_urs.assert_not_awaited()
    assert mock_log.warning.call_args.args[0] == "cost_finalize.failed_run_not_found"


async def test_finalize_failed_run_envelope_swallows_non_lock_failure() -> None:
    """The fail-open envelope logs an ordinary failure instead of raising it."""
    failure = AsyncMock(side_effect=RuntimeError("finalize boom"))
    with (
        patch("modulo.core.cost_controller.finalize._finalize_failed_run", new=failure),
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_failed_run(
            AsyncMock(),
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )
    failure.assert_awaited_once()
    assert mock_log.warning.call_args.args[0] == "cost_finalize.failed_run_finalize_failed"


# ---------------------------------------------------------------------------
# _is_exact_zero — FAR-653 zero detection
# ---------------------------------------------------------------------------


def test_is_exact_zero_true_for_zero() -> None:
    from modulo.core.cost_controller.finalize import _is_exact_zero

    assert _is_exact_zero(0)
    assert _is_exact_zero(0.0)
    assert _is_exact_zero(Decimal(0))
    assert _is_exact_zero("-0.0")


def test_is_exact_zero_false_for_nonzero() -> None:
    from modulo.core.cost_controller.finalize import _is_exact_zero

    assert not _is_exact_zero(1)
    assert not _is_exact_zero(-0.01)
    assert not _is_exact_zero(True)
    assert not _is_exact_zero(False)
    assert not _is_exact_zero("abc")
    assert not _is_exact_zero(None)
    assert not _is_exact_zero(float("nan"))
    assert not _is_exact_zero(float("inf"))
    assert not _is_exact_zero(float("-inf"))


# ---------------------------------------------------------------------------
# _is_sub_floor — FAR-1308 sub-floor report detection
# ---------------------------------------------------------------------------


def test_is_sub_floor_true_for_positive_below_floor() -> None:
    from modulo.core.cost_controller.finalize import _is_sub_floor

    assert _is_sub_floor(0.0000005)
    assert _is_sub_floor("0.0000005")


def test_is_sub_floor_false_for_non_sub_floor_values() -> None:
    from modulo.core.cost_controller.finalize import _is_sub_floor

    # A bool is never a numeric report (exercises the isinstance guard).
    assert not _is_sub_floor(True)
    assert not _is_sub_floor(False)
    # Zero and at/above the floor are NOT sub-floor; they are handled elsewhere.
    assert not _is_sub_floor(0)
    assert not _is_sub_floor(0.000001)
    assert not _is_sub_floor(1)
    # Negative or non-finite values are refused for a different reason.
    assert not _is_sub_floor(-0.0000005)
    assert not _is_sub_floor(float("nan"))
    assert not _is_sub_floor(float("inf"))
    # Non-numeric input is not a report at all.
    assert not _is_sub_floor("abc")
    assert not _is_sub_floor(None)


# ---------------------------------------------------------------------------
# _is_abort_error — whole-tx abort detection
# ---------------------------------------------------------------------------


def test_is_abort_error_false_for_non_dbapi() -> None:
    from modulo.core.cost_controller.finalize import _is_abort_error

    assert not _is_abort_error(RuntimeError("boom"))
    assert not _is_abort_error(ValueError("no"))


def test_is_abort_error_false_for_dbapi_none_orig() -> None:
    from sqlalchemy.exc import DBAPIError

    from modulo.core.cost_controller.finalize import _is_abort_error

    exc = DBAPIError("stmt", {}, Exception("orig"))
    # Override orig to None
    exc.orig = None
    assert not _is_abort_error(exc)


def test_is_abort_error_false_for_unnamed_orig() -> None:
    from sqlalchemy.exc import DBAPIError

    from modulo.core.cost_controller.finalize import _is_abort_error

    class CustomError(Exception):
        pass

    exc = DBAPIError("stmt", {}, CustomError("boom"))
    assert not _is_abort_error(exc)


# ---------------------------------------------------------------------------
# _is_limit_refused — daily-limit refusal detection
# ---------------------------------------------------------------------------


def test_is_limit_refused_true() -> None:
    from modulo.core.cost_controller.finalize import _is_limit_refused

    assert _is_limit_refused(False, "daily_limit_exceeded_team_x")


def test_is_limit_refused_false_ok() -> None:
    from modulo.core.cost_controller.finalize import _is_limit_refused

    assert not _is_limit_refused(True, "daily_limit_exceeded")


def test_is_limit_refused_false_other_reason() -> None:
    from modulo.core.cost_controller.finalize import _is_limit_refused

    assert not _is_limit_refused(False, "write_failure")


def test_is_limit_refused_false_none_reason() -> None:
    from modulo.core.cost_controller.finalize import _is_limit_refused

    assert not _is_limit_refused(False, None)


# ---------------------------------------------------------------------------
# _e2b_rate — E2B hourly rate fallback
# ---------------------------------------------------------------------------


def test_e2b_rate_returns_settings_value() -> None:
    from modulo.core.cost_controller.finalize import _e2b_rate

    mock_settings = MagicMock()
    mock_settings.e2b_sandbox_usd_per_hour = 0.5
    with patch("modulo.settings.get_settings", return_value=mock_settings):
        assert _e2b_rate() == Decimal("0.5")


def test_e2b_rate_fallback_on_exception() -> None:
    from modulo.core.cost_controller.finalize import _LEGACY_E2B_RATE_DEFAULT, _e2b_rate

    with patch(
        "modulo.settings.get_settings",
        side_effect=RuntimeError("settings broken"),
    ):
        assert _e2b_rate() == _LEGACY_E2B_RATE_DEFAULT


# ---------------------------------------------------------------------------
# derive_node_agent_map
# ---------------------------------------------------------------------------


def test_derive_node_agent_map_reads_agent_ids() -> None:
    from modulo.core.cost_controller.finalize import derive_node_agent_map

    graph = {
        "nodes": [
            {"id": "a", "agent_id": "agent-1"},
            {"id": "b", "agent_id": "agent-2"},
            {"id": "c"},  # no agent_id
        ]
    }
    result = derive_node_agent_map(graph)
    assert result == {"a": "agent-1", "b": "agent-2"}
    assert "c" not in result


def test_derive_node_agent_map_not_dict() -> None:
    from modulo.core.cost_controller.finalize import derive_node_agent_map

    assert not derive_node_agent_map(None)
    assert not derive_node_agent_map("bad")
    assert not derive_node_agent_map({"nodes": "not-list"})


def test_derive_node_agent_map_empty_nodes() -> None:
    from modulo.core.cost_controller.finalize import derive_node_agent_map

    assert not derive_node_agent_map({"nodes": []})


# ---------------------------------------------------------------------------
# _accumulate_agent_tokens
# ---------------------------------------------------------------------------


def test_accumulate_agent_tokens_basic() -> None:
    from modulo.core.cost_controller.finalize import _accumulate_agent_tokens

    usage = {
        "node-a": {"total_tokens": 100},
        "node-b": {"total_tokens": 200},
    }
    agent_map = {"node-a": "agent-1", "node-b": "agent-1", "node-c": "agent-2"}
    result = _accumulate_agent_tokens(usage, agent_map)
    assert result["agent-1"] == 300
    assert "agent-2" not in result


def test_accumulate_agent_tokens_fallback_to_input_plus_output() -> None:
    from modulo.core.cost_controller.finalize import _accumulate_agent_tokens

    usage = {"node-a": {"input_tokens": 10, "output_tokens": 5}}
    agent_map = {"node-a": "agent-1"}
    result = _accumulate_agent_tokens(usage, agent_map)
    assert result["agent-1"] == 15


def test_accumulate_agent_tokens_skips_non_dict_entries() -> None:
    from modulo.core.cost_controller.finalize import _accumulate_agent_tokens

    usage = {"node-a": "not-a-dict"}
    agent_map = {"node-a": "agent-1"}
    result = _accumulate_agent_tokens(usage, agent_map)
    assert not result


def test_accumulate_agent_tokens_empty_usage() -> None:
    from modulo.core.cost_controller.finalize import _accumulate_agent_tokens

    assert not _accumulate_agent_tokens(None, {})


# ---------------------------------------------------------------------------
# _trim_duplicate_events — event pruning
# ---------------------------------------------------------------------------


def test_trim_duplicate_events_empty() -> None:
    from modulo.core.cost_controller.finalize import _trim_duplicate_events

    assert not _trim_duplicate_events([])


def test_trim_duplicate_events_keeps_recent() -> None:
    from datetime import timedelta

    from modulo.core.cost_controller.finalize import _trim_duplicate_events

    now = datetime.now(UTC)
    events = [{"run_id": str(uuid.uuid4()), "ts": (now - timedelta(seconds=30)).isoformat()}]
    kept = _trim_duplicate_events(events)
    assert len(kept) == 1


def test_trim_duplicate_events_drops_stale() -> None:
    from datetime import timedelta

    from modulo.core.cost_controller.finalize import _trim_duplicate_events

    now = datetime.now(UTC)
    stale = {"run_id": "old", "ts": (now - timedelta(minutes=30)).isoformat()}
    recent = {"run_id": "new", "ts": (now - timedelta(seconds=10)).isoformat()}
    kept = _trim_duplicate_events([stale, recent])
    assert len(kept) == 1
    assert kept[0]["run_id"] == "new"


def test_trim_duplicate_events_keeps_unparseable_timestamp() -> None:
    from modulo.core.cost_controller.finalize import _trim_duplicate_events

    events = [{"run_id": "x", "ts": "not-a-date"}]
    kept = _trim_duplicate_events(events)
    assert len(kept) == 1


def test_trim_duplicate_events_limits_to_100() -> None:

    from modulo.core.cost_controller.finalize import _trim_duplicate_events

    now = datetime.now(UTC)
    events = [{"run_id": str(i), "ts": now.isoformat()} for i in range(150)]
    kept = _trim_duplicate_events(events)
    assert len(kept) == 100


# ---------------------------------------------------------------------------
# _apply_cancel_wins — B6 CANCEL-WINS
# ---------------------------------------------------------------------------


def test_apply_cancel_wins_overrides_awaiting_human() -> None:
    from modulo.core.cost_controller.finalize import _apply_cancel_wins

    run = MagicMock()
    run.cancellation_requested = True
    run.id = uuid.uuid4()
    assert _apply_cancel_wins(run, "awaiting_human") == "cancelled"


def test_apply_cancel_wins_overrides_complete() -> None:
    from modulo.core.cost_controller.finalize import _apply_cancel_wins

    run = MagicMock()
    run.cancellation_requested = True
    run.id = uuid.uuid4()
    assert _apply_cancel_wins(run, "complete") == "cancelled"


def test_apply_cancel_wins_preserves_running() -> None:
    from modulo.core.cost_controller.finalize import _apply_cancel_wins

    run = MagicMock()
    run.cancellation_requested = True
    run.id = uuid.uuid4()
    assert _apply_cancel_wins(run, "running") == "running"


def test_apply_cancel_wins_no_cancel_flag() -> None:
    from modulo.core.cost_controller.finalize import _apply_cancel_wins

    run = MagicMock()
    run.cancellation_requested = False
    run.id = uuid.uuid4()
    assert _apply_cancel_wins(run, "awaiting_human") == "awaiting_human"


# ---------------------------------------------------------------------------
# _is_empty_finalize_segment
# ---------------------------------------------------------------------------


def test_is_empty_finalize_segment_true() -> None:
    from modulo.core.cost_controller.finalize import _is_empty_finalize_segment

    assert _is_empty_finalize_segment({}, {}, {})


def test_is_empty_finalize_segment_false_with_usage() -> None:
    from modulo.core.cost_controller.finalize import _is_empty_finalize_segment

    assert not _is_empty_finalize_segment({"a": {"input_tokens": 1}}, {}, {})


def test_is_empty_finalize_segment_false_with_outputs() -> None:
    from modulo.core.cost_controller.finalize import _is_empty_finalize_segment

    assert not _is_empty_finalize_segment({}, {"a": {}}, {})


# ---------------------------------------------------------------------------
# _ledger_run_date
# ---------------------------------------------------------------------------


def test_ledger_run_date_terminal_positive_total() -> None:
    from modulo.core.cost_controller.finalize import _ledger_run_date

    run = MagicMock()
    run.started_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = _ledger_run_date(True, Decimal("0.13"), run)
    assert result is not None
    assert result.year == 2026
    assert result.month == 1
    assert result.day == 15


def test_ledger_run_date_not_terminal() -> None:
    from modulo.core.cost_controller.finalize import _ledger_run_date

    run = MagicMock()
    run.started_at = datetime(2026, 1, 15, tzinfo=UTC)
    assert _ledger_run_date(False, Decimal("0.13"), run) is None


def test_ledger_run_date_zero_total() -> None:
    from modulo.core.cost_controller.finalize import _ledger_run_date

    run = MagicMock()
    run.started_at = datetime(2026, 1, 15, tzinfo=UTC)
    assert _ledger_run_date(True, Decimal(0), run) is None


def test_ledger_run_date_no_started_at() -> None:
    from modulo.core.cost_controller.finalize import _ledger_run_date

    run = MagicMock()
    run.started_at = None
    assert _ledger_run_date(True, Decimal("0.13"), run) is None


# ---------------------------------------------------------------------------
# _log_union_size_guardrail
# ---------------------------------------------------------------------------


def test_log_union_size_guardrail_small_does_not_log() -> None:
    from modulo.core.cost_controller.finalize import _log_union_size_guardrail

    enriched = {"a": {"input_tokens": 10}}
    with patch("modulo.core.cost_controller.finalize._log") as mock_log:
        _log_union_size_guardrail(enriched, uuid.uuid4())
    mock_log.warning.assert_not_called()


def test_log_union_size_guardrail_large_logs_warning() -> None:
    from modulo.core.cost_controller.finalize import _UNION_SIZE_GUARDRAIL_BYTES, _log_union_size_guardrail

    big_dict = {f"x{i:04d}": {"data": "y" * 10000} for i in range(1000)}
    # Verify it's large enough
    size_bytes = len(str(big_dict).encode("utf-8"))
    if size_bytes > _UNION_SIZE_GUARDRAIL_BYTES:
        with patch("modulo.core.cost_controller.finalize._log") as mock_log:
            _log_union_size_guardrail(big_dict, uuid.uuid4())
        mock_log.warning.assert_called_once()


# ---------------------------------------------------------------------------
# _entry_amount
# ---------------------------------------------------------------------------


def test_entry_amount_basic() -> None:
    from modulo.core.cost_controller.finalize import _entry_amount

    result = _entry_amount(Decimal("0.123456789"))
    assert isinstance(result, str)
    # 6dp format
    assert "." in result


# ---------------------------------------------------------------------------
# _usage_token_sum
# ---------------------------------------------------------------------------


def test_usage_token_sum_basic() -> None:
    from modulo.core.cost_controller.finalize import _usage_token_sum

    usage = {
        "a": {"input_tokens": 10, "output_tokens": 5},
        "b": {"input_tokens": 20},
    }
    assert _usage_token_sum(usage, "input_tokens") == 30
    assert _usage_token_sum(usage, "output_tokens") == 5


def test_usage_token_sum_empty() -> None:
    from modulo.core.cost_controller.finalize import _usage_token_sum

    assert _usage_token_sum({}, "input_tokens") == 0
    assert _usage_token_sum(None, "input_tokens") == 0  # type: ignore[arg-type]


def test_usage_token_sum_skips_non_dict() -> None:
    from modulo.core.cost_controller.finalize import _usage_token_sum

    usage = {"a": "not-a-dict", "b": {"input_tokens": 10}}
    assert _usage_token_sum(usage, "input_tokens") == 10


# ---------------------------------------------------------------------------
# _fallback_wall_hours
# ---------------------------------------------------------------------------


def test_fallback_wall_hours_basic() -> None:
    from modulo.core.cost_controller.finalize import _fallback_wall_hours

    outputs = {"node-a": {}}
    telemetry = {"node-a": {"wall_clock_time_ms": 3_600_000}}
    assert _fallback_wall_hours(outputs, telemetry) == pytest.approx(1.0)


def test_fallback_wall_hours_empty() -> None:
    from modulo.core.cost_controller.finalize import _fallback_wall_hours

    assert _fallback_wall_hours({}, {}) == 0.0
    assert _fallback_wall_hours(None, None) == 0.0


def test_fallback_wall_hours_skips_non_dict_entry() -> None:
    from modulo.core.cost_controller.finalize import _fallback_wall_hours

    outputs = {"node-a": "not-a-dict"}
    assert _fallback_wall_hours(outputs, None) == 0.0


# ---------------------------------------------------------------------------
# _collect_node_emission_sources
# ---------------------------------------------------------------------------


def test_collect_node_emission_sources_basic() -> None:
    from modulo.core.cost_controller.finalize import _collect_node_emission_sources

    outputs = {
        "node-a": {
            "output": {
                "work_item_refs": [
                    {"kind": "issue", "ref": "FAR-100"},
                ]
            }
        }
    }
    result = _collect_node_emission_sources(outputs)
    assert len(result) == 1
    assert result[0][2] == "node-a"


def test_collect_node_emission_sources_skips_non_dict_nodes() -> None:
    from modulo.core.cost_controller.finalize import _collect_node_emission_sources

    outputs = {"node-a": "not-a-dict", "node-b": 42}
    assert not _collect_node_emission_sources(outputs)


def test_collect_node_emission_sources_skips_non_dict_output() -> None:
    from modulo.core.cost_controller.finalize import _collect_node_emission_sources

    outputs = {"node-a": {"output": "not-a-dict"}}
    assert not _collect_node_emission_sources(outputs)


def test_collect_node_emission_sources_skips_non_list_refs() -> None:
    from modulo.core.cost_controller.finalize import _collect_node_emission_sources

    outputs = {"node-a": {"output": {"work_item_refs": "not-a-list"}}}
    assert not _collect_node_emission_sources(outputs)


def test_collect_node_emission_sources_skips_malformed_ref() -> None:
    from modulo.core.cost_controller.finalize import _collect_node_emission_sources

    outputs = {"node-a": {"output": {"work_item_refs": ["garbage", 42, {"kind": "x", "ref": "y"}]}}}
    result = _collect_node_emission_sources(outputs)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# _canonical_emission
# ---------------------------------------------------------------------------


def test_canonical_emission_missing_kind_or_ref_returns_none() -> None:
    from modulo.core.cost_controller.finalize import _canonical_emission

    assert _canonical_emission({"kind": None, "ref": "FAR-100"}) is None
    assert _canonical_emission({"kind": "linear"}) is None


def test_canonical_emission_uncanonicalisable_value_returns_none() -> None:
    from modulo.core.cost_controller.finalize import _canonical_emission

    assert _canonical_emission({"kind": "   ", "ref": "FAR-100"}) is None


def test_canonical_emission_valid_returns_tuple() -> None:
    from modulo.core.cost_controller.finalize import _canonical_emission

    emission = _canonical_emission({"kind": "issue", "ref": "FAR-100"})
    assert emission is not None
    assert emission[0] == "issue"


# ---------------------------------------------------------------------------
# _fold_stored_clamped branches
# ---------------------------------------------------------------------------


def test_fold_stored_clamped_none_stored() -> None:

    node_dict: dict[str, Any] = {}
    _fold_stored_clamped(node_dict)
    assert "model_cost_usd" not in node_dict


def test_fold_stored_clamped_valid_fold() -> None:

    node_dict: dict[str, Any] = {"model_cost_usd": 0.05}
    with patch("modulo.core.cost_controller.finalize.clamp_reported", return_value=(Decimal("0.05"), False, False)):
        _fold_stored_clamped(node_dict)
    assert node_dict["model_cost_usd"] == pytest.approx(0.05)


def test_fold_stored_clamped_rejects_invalid_fold() -> None:

    node_dict: dict[str, Any] = {
        "model_cost_usd": -5.0,
        "model_cost_clamped": True,
        "model_cost_out_of_band_high": True,
    }
    with (
        patch("modulo.core.cost_controller.finalize.clamp_reported", return_value=None),
        patch("modulo.core.cost_controller.finalize._is_exact_zero", return_value=False),
    ):
        _fold_stored_clamped(node_dict)
    assert "model_cost_usd" not in node_dict


def test_fold_stored_clamped_keeps_exact_zero() -> None:

    node_dict: dict[str, Any] = {
        "model_cost_usd": 0.0,
        "model_cost_clamped": False,
        "model_cost_out_of_band_high": False,
    }
    with (
        patch("modulo.core.cost_controller.finalize.clamp_reported", return_value=None),
        patch("modulo.core.cost_controller.finalize._is_exact_zero", return_value=True),
    ):
        _fold_stored_clamped(node_dict)
    assert node_dict["model_cost_usd"] == 0.0


def test_fold_stored_clamped_sub_floor_stamps_rejection_before_pop() -> None:
    """FAR-1308 third stamp site: outputs-pruned stored-union row.

    The node's ``outputs`` were pruned (``output_obj is None`` → branch 3), so
    the stored union value is the only surviving candidate. When it is a
    positive sub-floor report ``clamp_reported`` refuses it (real, unpatched
    boundary), the fold must stamp ``sub_floor_rejected`` BEFORE popping the
    cost fields — otherwise the legacy row resurfaces as a false
    ``agent_not_reported`` with no way to tell a refused report from silence.
    """
    node_dict: dict[str, Any] = {
        "model_cost_usd": 0.0000005,
        "sandbox_by_map": True,
    }
    _fold_stored_clamped(node_dict)
    assert node_dict.get("model_cost_rejected") is True
    assert node_dict.get("model_cost_rejection_reason") == "sub_floor_rejected"
    # Boundary unchanged: the refused value is still popped, never counted.
    assert "model_cost_usd" not in node_dict


# ---------------------------------------------------------------------------
# _record_node_schema_drift
# ---------------------------------------------------------------------------


def test_record_node_schema_drift_increments_when_sandbox_and_drift() -> None:

    output_obj = {"schema_drift": True, "pin_failed": False}
    with patch("modulo.core.cost_controller.finalize.record_schema_drift") as mock_rec:
        _record_node_schema_drift(output_obj, "sandbox_agent")
    mock_rec.assert_called_once()


def test_record_node_schema_drift_no_increment_when_pin_failed() -> None:

    output_obj = {"schema_drift": True, "pin_failed": True}
    with patch("modulo.core.cost_controller.finalize.record_schema_drift") as mock_rec:
        _record_node_schema_drift(output_obj, "sandbox_agent")
    mock_rec.assert_not_called()


def test_record_node_schema_drift_no_increment_when_not_sandbox() -> None:

    output_obj = {"schema_drift": True}
    with patch("modulo.core.cost_controller.finalize.record_schema_drift") as mock_rec:
        _record_node_schema_drift(output_obj, "agent")
    mock_rec.assert_not_called()


def test_record_node_schema_drift_no_increment_when_no_drift() -> None:

    output_obj = {}
    with patch("modulo.core.cost_controller.finalize.record_schema_drift") as mock_rec:
        _record_node_schema_drift(output_obj, "sandbox_agent")
    mock_rec.assert_not_called()


# ---------------------------------------------------------------------------
# _wall_clock_ms
# ---------------------------------------------------------------------------


def test_wall_clock_ms_valid() -> None:

    assert _wall_clock_ms({"wall_clock_time_ms": 1234}) == 1234


def test_wall_clock_ms_none_for_non_dict() -> None:

    assert _wall_clock_ms(None) is None
    assert _wall_clock_ms("bad") is None


def test_wall_clock_ms_none_for_missing_key() -> None:

    assert _wall_clock_ms({"other": True}) is None


def test_wall_clock_ms_none_for_non_numeric() -> None:

    assert _wall_clock_ms({"wall_clock_time_ms": "fast"}) is None


# ---------------------------------------------------------------------------
# _node_output_dict
# ---------------------------------------------------------------------------


def test_node_output_dict_returns_telemetry_entry() -> None:

    telemetry = {"node-a": {"status": "completed", "wall_clock_time_ms": 100}}
    outputs = {"node-a": {"summary": "done"}}
    result = _node_output_dict(outputs, "node-a", telemetry)
    assert result == {"status": "completed", "wall_clock_time_ms": 100}


def test_node_output_dict_none_when_no_match() -> None:

    assert _node_output_dict({}, "node-a", {}) is None


# ---------------------------------------------------------------------------
# _seed_union
# ---------------------------------------------------------------------------


def test_seed_union_with_usage_dict() -> None:

    usage = {"a": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    outputs = {"b": {"summary": "done"}}
    union = _seed_union(usage, outputs)
    assert union["a"]["input_tokens"] == 10
    assert union["b"]["input_tokens"] == 0


def test_seed_union_non_dict_usage_value() -> None:

    usage = {"a": "not-a-dict"}
    union = _seed_union(usage, {})
    assert union["a"]["input_tokens"] == 0


def test_seed_union_empty() -> None:

    union = _seed_union({}, {})
    assert not union


# ---------------------------------------------------------------------------
# _fold_model_cost branches
# ---------------------------------------------------------------------------


def test_fold_model_cost_output_none_falls_stored() -> None:

    node_dict: dict[str, Any] = {"model_cost_usd": 0.05}
    with patch("modulo.core.cost_controller.finalize.clamp_reported", return_value=(Decimal("0.05"), False, False)):
        _fold_model_cost(node_dict, None)
    assert node_dict["model_cost_usd"] == pytest.approx(0.05)


def test_fold_model_cost_output_without_model_cost_pops() -> None:

    node_dict: dict[str, Any] = {"model_cost_usd": 0.05, "model_cost_raw_usd": 0.06}
    _fold_model_cost(node_dict, {"wall_clock_time_ms": 100})
    assert "model_cost_usd" not in node_dict
    assert "model_cost_raw_usd" not in node_dict


def test_fold_model_cost_output_with_raw_usd_folded() -> None:

    node_dict: dict[str, Any] = {}
    output_obj = {"model_cost_usd": 0.05, "model_cost_raw_usd": 0.06}
    with patch("modulo.core.cost_controller.finalize.clamp_reported", return_value=(Decimal("0.06"), False, False)):
        _fold_model_cost(node_dict, output_obj)
    assert node_dict["model_cost_usd"] == pytest.approx(0.06)
    assert node_dict["model_cost_raw_usd"] == pytest.approx(0.06)


def test_fold_model_cost_output_without_raw_pops_raw() -> None:

    node_dict: dict[str, Any] = {"model_cost_raw_usd": 0.06}
    output_obj = {"model_cost_usd": 0.05}
    with patch("modulo.core.cost_controller.finalize.clamp_reported", return_value=(Decimal("0.05"), False, False)):
        _fold_model_cost(node_dict, output_obj)
    assert node_dict["model_cost_usd"] == pytest.approx(0.05)
    assert "model_cost_raw_usd" not in node_dict


# ---------------------------------------------------------------------------
# _derive_total_tokens edge cases
# ---------------------------------------------------------------------------


def test_derive_total_tokens_falls_back_to_input_plus_output() -> None:
    enriched = {
        "node-a": {"input_tokens": 10, "output_tokens": 5},  # no total_tokens
    }
    assert _derive_total_tokens(enriched) == 15


def test_derive_total_tokens_skips_bool_total() -> None:
    enriched = {"node-a": {"total_tokens": True}}
    assert _derive_total_tokens(enriched) == 0


def test_derive_total_tokens_empty() -> None:
    assert _derive_total_tokens({}) == 0
    assert _derive_total_tokens(None) == 0


def test_derive_total_tokens_skips_non_dict_entry() -> None:
    enriched = {"node-a": "not-a-dict"}
    assert _derive_total_tokens(enriched) == 0


# ---------------------------------------------------------------------------
# _derive_total_tokens
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _token_cost edge cases
# ---------------------------------------------------------------------------


def test_token_cost_empty() -> None:
    assert _token_cost({}) == Decimal(0)


def test_token_cost_non_dict_entry_skipped() -> None:
    usage = {"a": "not-a-dict"}
    assert _token_cost(usage) == Decimal(0)


# ---------------------------------------------------------------------------
# _legacy_sandbox_cost edge cases
# ---------------------------------------------------------------------------


def test_legacy_sandbox_cost_non_dict_outputs() -> None:
    assert _legacy_sandbox_cost(None) == Decimal(0)  # type: ignore[arg-type]
    assert _legacy_sandbox_cost("bad") == Decimal(0)  # type: ignore[arg-type]


def test_legacy_sandbox_cost_skips_non_dict_entry() -> None:
    from modulo.core.cost_controller.finalize import _legacy_sandbox_cost

    with patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")):
        result = _legacy_sandbox_cost({"a": "not-a-dict"})
    assert result == Decimal(0)


def test_legacy_sandbox_cost_skips_zero_wall_clock() -> None:
    from modulo.core.cost_controller.finalize import _legacy_sandbox_cost

    outputs = {"node-a": {}}
    telemetry = {"node-a": {"wall_clock_time_ms": 0}}
    with patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")):
        result = _legacy_sandbox_cost(outputs, telemetry)
    assert result == Decimal(0)


def test_legacy_sandbox_cost_skips_nan_wall_clock() -> None:
    from modulo.core.cost_controller.finalize import _legacy_sandbox_cost

    outputs = {"node-a": {}}
    telemetry = {"node-a": {"wall_clock_time_ms": float("nan")}}
    with patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")):
        result = _legacy_sandbox_cost(outputs, telemetry)
    assert result == Decimal(0)


async def test_finalize_cost_run_not_found() -> None:
    """finalize_cost returns early when the run row is missing."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    with patch("modulo.core.cost_controller.finalize._log") as mock_log:
        await finalize_cost(
            session,
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
            status="complete",
            segment_node_token_usage=None,
            segment_completed_node_outputs=None,
            node_type_map={},
            is_terminal=True,
        )
    # Should log a warning and return without calling update_run_status
    assert any("run_not_found" in str(c) for c in mock_log.warning.call_args_list)


# ---------------------------------------------------------------------------
# FAR-1914 B1 — salvage the killed attempt's own spend from retained markers
# ---------------------------------------------------------------------------


def _step_finish_marker(
    *,
    cost: float | None = 0.000478,
    total_tokens: int = 78_000,
    node_id: str | None = "n1",
    pr_url: str | None = None,
    delivery_done: bool = False,
    raw_output: str | None = None,
) -> dict[str, Any]:
    """A retained cancellation marker shaped like the live ``c4222006`` one.

    The stored ``raw_output`` is the opencode JSONL stream the cancelled
    sandbox left behind: a ``step_finish`` event carrying the agent's own
    ``tokens`` and ``cost`` NESTED under ``part`` — the shape the shipped CLI
    emits and the ONLY shape the shared leaf parser reads (FAR-1914 M2).

    MODELLED, not captured: no real retained-marker blob is checked into the
    repo, so this fixture is built from the CLI's documented emitted shape and
    the incident's marker fields. ``node_id=None`` reproduces a run-level
    synthetic triage marker (FAR-1886 B1).
    """
    event: dict[str, Any] = {
        "type": "step_finish",
        "part": {
            "tokens": {"total": total_tokens, "input": total_tokens - 10_000, "output": 10_000},
        },
    }
    if cost is not None:
        event["part"]["cost"] = cost
    return {
        "_modulo_marker": True,
        "status": "failed",
        "summary": "Sandbox agent cancelled — triage evidence retained (FAR-704)",
        "raw_output": json.dumps(event) if raw_output is None else raw_output,
        "parse_error": "",
        "pr_url": pr_url,
        "delivery_done": delivery_done,
        "exit_code": -1,
        "stdout_length": 0,
        "stderr_length": 0,
        "attempt_key": "run:00000000-0000-0000-0000-000000000002:node:n1:1",
        "node_id": node_id,
        "deadline_triage": True,
        "no_output_reason": "cancelled before a terminal node envelope",
        "sandbox_log_tail": "",
    }


def _run_level_triage_marker() -> dict[str, Any]:
    """A synthetic RUN-LEVEL triage marker (FAR-1886 B1): no node identity.

    The age gate / stranded-pending / worker-lost terminalisers write this
    shape. Its four output fields are empty placeholders, so it can never
    prove a SANDBOX attempt ran.
    """
    return {
        "_modulo_marker": True,
        "status": "failed",
        "summary": "Run terminalised by run.no_progress — run-level triage evidence (FAR-1886)",
        "raw_output": "",
        "parse_error": "",
        "pr_url": None,
        "exit_code": -1,
        "stdout_length": 0,
        "stderr_length": 0,
        "attempt_key": "terminal-triage:00000000-0000-0000-0000-000000000002",
        "node_id": None,
        "deadline_triage": True,
        "no_output_reason": "no node progress within the age bound",
        "sandbox_log_tail": "",
    }


def _connector_delivered_marker() -> dict[str, Any]:
    """A connector idempotency delivery marker (no sandbox output at all).

    ``node_runner._mark_connector_write_delivered`` writes exactly this shape:
    ``_modulo_marker`` + a node id + ``delivery_done`` — and none of the
    sandbox output fields. It must never be read as sandbox-attempt proof.
    """
    return {
        "_modulo_marker": True,
        "status": "completed",
        "summary": "connector write delivered (delivery_done)",
        "node_id": "c1",
        "attempt_key": "connector:c1",
        "delivery_done": True,
    }


async def test_finalize_failed_run_salvages_agent_cost_from_retained_marker() -> None:
    """A deadline-killed attempt reports the spend its own tail proves.

    ``c4222006``-shaped: no stored node data (no ``output.json`` ever
    written), but the retained marker carries per-step token cost. Without the
    salvage the run's breakdown holds only the FAR-1993 derived sandbox line;
    with it the agent-reported cost is visible with ``reported`` provenance.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker()}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()) as mock_block,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )

    kwargs = mock_urs.await_args.kwargs
    # FAR-1993 derived sandbox spend still drives the billable total.
    assert kwargs["total_cost_usd"] == Decimal("0.065")
    assert mock_block.await_args.kwargs["total"] == Decimal("0.065")
    breakdown = kwargs["cost_breakdown"]
    assert len(breakdown) == 2
    assert breakdown[0]["component"] == "sandbox_infra"
    assert breakdown[0]["source"] == "derived"
    salvaged = breakdown[1]
    assert salvaged["component"] == "model_tokens"
    assert salvaged["source"] == "self_reported"
    assert salvaged["billable"] is False
    assert salvaged["provenance"] == "reported"
    assert salvaged["amount_usd"] == "0.000478"
    assert salvaged["basis"]["salvaged_from"] == "retained_raw_output_step_finish"
    assert salvaged["basis"]["node_count"] == 1
    # FAR-1914 M4: token counters are never collected (collect-and-drop is
    # not provenance) — only the cost travels.


def test_salvage_from_markers_sums_attempts_and_records_delivery() -> None:
    """Costs sum across attempts; a delivered attempt arms ``delivery_done``."""
    markers = {
        "attempt-1": _step_finish_marker(cost=0.0003, total_tokens=40_000, node_id="n1"),
        "attempt-2": _step_finish_marker(
            cost=0.000178,
            total_tokens=38_000,
            node_id="n1",
            pr_url="https://github.com/farnalabs/modulo/pull/1837",
            delivery_done=True,
        ),
    }

    salvage = _salvage_from_markers(markers)

    assert salvage.agent_cost_usd == Decimal("0.000478")
    assert salvage.node_ids == ("n1",)
    assert salvage.delivery_done is True
    assert salvage.has_evidence is True
    # FAR-1914 C1/M4: the PR url is NOT collected on the salvage side at all —
    # the marker's ``delivery_done`` (sentinel-armed) is the delivery fact.
    assert not hasattr(salvage, "pr_url")
    assert not hasattr(salvage, "token_usage")


def test_salvage_from_markers_without_evidence_has_none() -> None:
    """Markers that prove no work and no delivery must yield no evidence."""
    markers = {"attempt-1": _step_finish_marker(raw_output="working\n")}

    salvage = _salvage_from_markers(markers)

    assert salvage.agent_cost_usd is None
    assert salvage.delivery_done is False
    assert salvage.has_evidence is False
    # The node id still travels — it is identity evidence, not spend proof.
    assert salvage.node_ids == ("n1",)


def test_salvage_from_markers_tokens_only_has_no_evidence() -> None:
    """Round-2 minor 4: token counters alone are NOT salvage evidence.

    FAR-1914 M4 deliberately does not collect tokens (they are collect-and-
    drop), so a ``step_finish`` stream carrying tokens but no usable ``cost``
    proves no attributable spend. The node id still travels as identity
    evidence.
    """
    markers = {"attempt-1": _step_finish_marker(cost=None)}

    salvage = _salvage_from_markers(markers)

    assert salvage.agent_cost_usd is None
    assert salvage.delivery_done is False
    assert salvage.has_evidence is False
    assert salvage.node_ids == ("n1",)


async def test_salvage_cancelled_run_cost_does_not_write_for_tokens_only_markers() -> None:
    """Round-2 minor 4: a tokens-only settled marker writes NOTHING.

    The run has a proven liveness window AND sandbox-attempt evidence, so it
    would be tempting to bill the sandbox line — but there is no attributable
    agent spend and the salvage exists to compensate MARKER-PROVABLE spend.
    ``has_evidence is False`` short-circuits before the writer.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run.heartbeat_at = run.completed_at
    run.total_cost_usd = None
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker(cost=None)}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    mock_urs.assert_not_awaited()


def test_salvage_from_markers_ignores_top_level_only_event_shapes() -> None:
    """FAR-1914 M2: the parser reads the ``part``-nested shape ONLY.

    A marker whose event carries top-level ``tokens``/``cost`` (a shape the
    shipped CLI never emitted) proves no spend — accepting it there would
    keep the wrong parser alive and let a hand-written shape attribute cost.
    """
    event = {
        "type": "step_finish",
        "tokens": {"total": 78_000, "input": 68_000, "output": 10_000},
        "cost": 0.000478,
    }
    markers = {"attempt-1": _step_finish_marker(raw_output=json.dumps(event))}

    salvage = _salvage_from_markers(markers)

    assert salvage.agent_cost_usd is None
    assert salvage.has_evidence is False


def test_salvage_from_markers_delivery_sentinel_gates_without_cost() -> None:
    """A sentinel-fired marker gates the salvage on its own (FAR-1914 M4).

    ``has_evidence`` is cost OR delivery — a delivered-but-costless attempt
    still counts as evidence (the derived sandbox line may then be attributed
    from the run's proven liveness window; the reported line stays silent
    because there is no reported cost)."""
    markers = {"attempt-1": _step_finish_marker(raw_output="agent working\n", delivery_done=True)}

    salvage = _salvage_from_markers(markers)

    assert salvage.agent_cost_usd is None
    assert salvage.delivery_done is True
    assert salvage.has_evidence is True


async def test_finalize_failed_run_marker_without_cost_evidence_keeps_one_entry() -> None:
    """A retained marker with NO ``step_finish`` event proves no spend.

    The tail may exist (triage evidence) but with no per-step cost there is
    nothing to attribute — the run keeps the single FAR-1993 derived entry and
    nothing is fabricated from the marker's mere presence.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker(raw_output="no events")}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
        )

    breakdown = mock_urs.await_args.kwargs["cost_breakdown"]
    assert len(breakdown) == 1
    assert breakdown[0]["component"] == "sandbox_infra"


@pytest.mark.parametrize(
    "derived_from",
    ["node_deadline_watchdog", "reconcile_age_gate_heartbeat"],
)
async def test_finalize_failed_run_withholds_sandbox_cost_without_sandbox_evidence(derived_from: str) -> None:
    """Round-2 B (prove-the-fix): a proven duration is not a sandbox proof.

    Both derived paths — the in-process node-deadline watchdog and the
    reconcile age gate — bill ``sandbox_infra * window`` for ANY run. A
    non-sandbox node killed at its deadline (or a pipeline that never
    dispatched a sandbox node) leaves only the run-level synthetic triage
    marker, so the sandbox line must be withheld. Without the gate both paths
    price and write a sandbox line.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"terminal-triage:00000000-0000-0000-0000-000000000002": _run_level_triage_marker()}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")) as mock_rate,
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
            derived_from=derived_from,
        )

    mock_rate.assert_not_called()
    mock_urs.assert_not_awaited()
    logged = [str(c) for c in mock_log.warning.call_args_list]
    assert any("cost_finalize.derived_sandbox_cost_withheld" in line for line in logged)


async def test_finalize_failed_run_withholds_sandbox_cost_when_the_marker_is_for_another_node() -> None:
    """Round-3 M1 (prove-the-fix): run-scoped evidence cannot license a
    node-scoped deadline duration.

    The run retains a sandbox marker for node ``n1``, but the deadline watchdog
    killed node ``n2`` (a connector/HTTP/agent node). Crediting ``n2``'s
    configured timeout because SOME sandbox node ran earlier would bill a
    non-sandbox node as sandbox spend. With ``killed_node_id`` the marker must
    belong to the killed node, so no sandbox line is written.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    # A real per-attempt SANDBOX marker — but for node n1, not the killed n2.
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker(cost=None, node_id="n1")}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")) as mock_rate,
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n2' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
            killed_node_id="n2",
        )

    mock_rate.assert_not_called()
    mock_urs.assert_not_awaited()
    logged = [str(c) for c in mock_log.warning.call_args_list]
    assert any("cost_finalize.derived_sandbox_cost_withheld" in line for line in logged)


async def test_finalize_failed_run_still_attributes_sandbox_cost_with_attempt_marker() -> None:
    """Round-2 B: the gate keeps the legitimate sandbox line (no regression).

    A retained per-attempt sandbox marker (node identity + triage signature)
    is the proof the derived duration needs, so the sandbox line is still
    written with its provenance label.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    # Tokens only (no agent-reported cost): the ONLY entry is the sandbox line.
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker(cost=None)}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await finalize_failed_run(
            session,
            run_id=run.id,
            org_id=_ORG_ID,
            error_code="node_deadline_exceeded",
            error_detail="node 'n1' exceeded its deadline",
            derived_sandbox_wall_hours=0.5,
            derived_from="node_deadline_watchdog",
            # Round-3 M1 positive control: the marker belongs to the killed
            # node, so the node-scoped proof still licenses the sandbox line.
            killed_node_id="n1",
        )

    breakdown = mock_urs.await_args.kwargs["cost_breakdown"]
    assert [entry["component"] for entry in breakdown] == ["sandbox_infra"]
    assert breakdown[0]["basis"]["derived_from"] == "node_deadline_watchdog"
    assert mock_urs.await_args.kwargs["total_cost_usd"] == Decimal("0.065")


async def test_finalize_cancelled_run_does_not_salvage_on_the_request_path() -> None:
    """FAR-1914 C2: the API/MCP cancel REQUEST never attributes the cost.

    The executor has not observed the cancellation yet (it notices on its next
    heartbeat), so its attempt marker does not exist at this point — reading
    markers here finds nothing and would record $0 as though no work
    happened. The request path keeps the pre-existing diagnostic; the salvage
    lives with the executor AFTER it settles.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run.heartbeat_at = run.completed_at
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {
        "run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker(
            pr_url="https://github.com/farnalabs/modulo/pull/1837"
        )
    }
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()) as mock_block,
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_cancelled_run(session, run_id=run.id, org_id=_ORG_ID)

    mock_urs.assert_not_awaited()
    mock_block.assert_not_awaited()
    # Round-2 minor 1: a normal cancel DEFERS the salvage — it must not emit
    # the forfeiture diagnostic (that one is reserved for terminal paths that
    # genuinely cannot attribute anything).
    logged_info = [str(c) for c in mock_log.info.call_args_list]
    assert any("cost_components.cancel_salvage_deferred" in line for line in logged_info)
    logged_warnings = [str(c) for c in mock_log.warning.call_args_list]
    assert not any("cost_components_partial_spend_lost" in line for line in logged_warnings)


async def test_finalize_cancelled_run_without_marker_evidence_forfeits() -> None:
    """A cancelled run with no stored sets keeps the pre-existing path.

    No stored sets means no proof of attributed work on the request path — the
    run is not given a fabricated cost, and the DEFERRAL diagnostic still
    fires (never the forfeiture one: the executor's post-settle salvage owns
    the marker-provable spend).
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=None)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await finalize_cancelled_run(session, run_id=run.id, org_id=_ORG_ID)

    mock_urs.assert_not_awaited()
    logged_info = [str(c) for c in mock_log.info.call_args_list]
    assert any("cost_components.cancel_salvage_deferred" in line for line in logged_info)
    logged_warnings = [str(c) for c in mock_log.warning.call_args_list]
    assert not any("cost_components_partial_spend_lost" in line for line in logged_warnings)


async def test_salvage_cancelled_run_cost_attributes_settled_marker_spend() -> None:
    """FAR-1914 C2: the post-settle salvage attributes the settled marker.

    The executor has settled, so its persisted attempt marker now exists and
    is honest spend evidence. The attributed total is the FAILED deadline
    rate-proven sandbox line (0.5h provenance at $0.13/h) plus the salvaged
    reported cost, and the ledger block carries both.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run.heartbeat_at = run.completed_at
    run.total_cost_usd = None
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker()}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()) as mock_block,
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    kwargs = mock_urs.await_args.kwargs
    assert mock_urs.await_args.args[1] == run.id
    assert mock_block.await_args.kwargs["status"] == "cancelled"
    # 1 hour of proven liveness at $0.13/h — derived, not fabricated.
    assert kwargs["total_cost_usd"] == Decimal("0.13")
    breakdown = kwargs["cost_breakdown"]
    assert [entry["component"] for entry in breakdown] == ["sandbox_infra", "model_tokens"]
    assert breakdown[0]["basis"]["derived_from"] == "cancel_liveness_window"
    assert breakdown[1]["amount_usd"] == "0.000478"
    assert breakdown[1]["billable"] is False


async def test_salvage_cancelled_run_cost_skips_when_status_is_not_cancelled() -> None:
    """A watchdog/successor outcome is downstream of this one salvage (C2).

    A terminal-failed run's spend is ``_record_derived_deadline_cost``'s job;
    a superseded run belongs to its successor; a running row found after a
    settle race must not be attributed speculatively. Only terminal
    ``cancelled`` rows are salvageable.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "failed"
    run.total_cost_usd = None
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch("modulo.core.cost_controller.finalize.read_run_blobs", new=AsyncMock()) as mock_blobs,
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    mock_blobs.assert_not_awaited()
    mock_urs.assert_not_awaited()


async def test_salvage_cancelled_run_cost_attributes_a_zero_total_empty_segment_run() -> None:
    """An EMPTY-SEGMENT cancelled run ($0 total, no breakdown) IS salvageable.

    The pre-FAR-1914 "$0" outcome (finalised as a zero-total empty segment) is
    exactly the shape the salvage exists to compensate — it must NOT be
    confused with an already-attributed cost.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run.heartbeat_at = run.completed_at
    run.total_cost_usd = Decimal("0.0")
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker()}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    mock_urs.assert_awaited_once()


async def test_salvage_cancelled_run_cost_skips_when_cost_already_attributed() -> None:
    """A real finalize's (or earlier salvage's) spend is never re-written."""
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.total_cost_usd = Decimal("0.065")
    run.cost_breakdown = [{"component": "sandbox_infra"}]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch("modulo.core.cost_controller.finalize.read_run_blobs", new=AsyncMock()) as mock_blobs,
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    mock_blobs.assert_not_awaited()
    mock_urs.assert_not_awaited()


async def test_salvage_cancelled_run_cost_skips_without_marker_evidence() -> None:
    """A settled cancelled run with NO retained evidence is NOT attributed."""
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.total_cost_usd = None
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=None)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._log") as mock_log,
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    mock_urs.assert_not_awaited()
    # The salvage is a quiet no-op on a non-attributable shaped run — the
    # FORFEIT diagnostic is the request path's (it already logged there);
    # salvaging must never complain about honest absence.
    logged = [str(c) for c in mock_log.warning.call_args_list]
    assert not any("cost_components_partial_spend_lost" in line for line in logged)


async def test_salvage_cancelled_run_cost_attributes_cost_without_a_liveness_proof() -> None:
    """The REPORTED line is independent of the sandbox duration (FAR-1914 M3).

    A missing ``heartbeat_at`` means NO proven sandbox window — the derived
    sandbox line must not be written — but the marker's own reported cost is
    still attributed. Conversely, a proven window with NO sandbox-attempt
    marker writes neither (the sandbox duration is billable money and needs
    its own attempt proof).
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run.heartbeat_at = None  # no liveness proof
    run.total_cost_usd = None
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker()}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    breakdown = mock_urs.await_args.kwargs["cost_breakdown"]
    assert [entry["component"] for entry in breakdown] == ["model_tokens"]
    assert mock_urs.await_args.kwargs["total_cost_usd"] == Decimal(0)


def test_salvage_sandbox_line_never_attributed_without_an_attempt_marker() -> None:
    """FAR-1914 M3 + round-2 A: wall-clock money needs SANDBOX-attempt proof.

    The derived sandbox line is BILLABLE; it may only ride a run that proves a
    sandbox actually existed (a retained per-attempt marker naming a node).
    A generic ``_modulo_marker`` — e.g. a connector idempotency marker — and a
    run-level synthetic triage marker (``node_id: None``) are NOT that proof.
    """
    # A per-attempt sandbox marker: node identity + the triage signature.
    assert _has_sandbox_attempt_evidence({"a": _step_finish_marker(), "b": {"other": "shape"}}) is True
    # Sandbox output fields are sufficient even without the triage flag.
    assert (
        _has_sandbox_attempt_evidence(
            {
                "a": {
                    "node_id": "n1",
                    "raw_output": "",
                    "stdout_length": 0,
                    "sandbox_log_tail": "",
                    "no_output_reason": "no parseable output",
                }
            }
        )
        is True
    )
    # Round-2 A: a connector delivery marker carries ``_modulo_marker`` and a
    # node id but NO sandbox output — it must not bill sandbox time.
    assert _has_sandbox_attempt_evidence({"a": _connector_delivered_marker()}) is False
    # Round-2 B: a run-level synthetic triage marker has no node identity.
    assert _has_sandbox_attempt_evidence({"a": _run_level_triage_marker()}) is False
    assert _has_sandbox_attempt_evidence({"a": {"deadline_triage": True}}) is False
    assert _has_sandbox_attempt_evidence({"a": {"node_id": "n1"}}) is False
    assert _has_sandbox_attempt_evidence(None) is False


async def test_salvage_cancelled_run_cost_does_not_bill_a_connector_only_run() -> None:
    """Round-2 A (prove-the-fix): a cancelled CONNECTOR-only run is not billed.

    ``_mark_connector_write_delivered`` writes ``_modulo_marker: True`` +
    ``delivery_done: True`` and no sandbox output. The pre-fix
    ``_has_sandbox_attempt_evidence`` accepted that flag as sandbox proof, so
    a cancelled connector-only run was billed E2B sandbox time for its whole
    heartbeat window. With the sandbox-specific gate the run has evidence to
    salvage (the delivery fact) but no sandbox proof, so no sandbox line — and
    with no agent-reported cost there is nothing to write at all.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run.heartbeat_at = run.completed_at
    run.total_cost_usd = None
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))
    markers = {"connector:c1": _connector_delivered_marker()}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch("modulo.core.cost_controller.finalize.update_run_status", new=AsyncMock()) as mock_urs,
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")) as mock_rate,
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    # No sandbox line was even priced, and no terminal write happened.
    mock_rate.assert_not_called()
    mock_urs.assert_not_awaited()


async def test_salvage_cancelled_run_cost_bounds_the_row_lock_first() -> None:
    """Round-2 C (prove-the-fix): the salvage issues the lock bound FIRST.

    The salvage's terminal write takes the hot ``runs`` row lock inside the
    caller's transaction. Without the entry-point bound the wait is unbounded;
    this asserts the bound is issued on the session before the row read.
    """
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run.heartbeat_at = run.completed_at
    run.total_cost_usd = None
    run.cost_breakdown = None
    order: list[str] = []
    session = AsyncMock()

    async def _execute(*_args: Any, **_kwargs: Any) -> MagicMock:
        order.append("select")
        return MagicMock(scalar_one_or_none=MagicMock(return_value=run))

    async def _bound(_session: Any) -> None:
        order.append("bound")

    session.execute = AsyncMock(side_effect=_execute)
    with (
        patch(
            "modulo.core.cost_controller.finalize.set_mutation_row_lock_timeout",
            new=AsyncMock(side_effect=_bound),
        ),
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=None)),
        ),
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    # The bound runs before the first statement the salvage issues.
    assert order[0] == "bound"
    assert "select" in order


async def test_salvage_cancelled_run_cost_preserves_the_recorded_terminal_time() -> None:
    """Round-2 minor 2 (prove-the-fix): salvage never re-stamps ``completed_at``.

    ``update_run_status`` stamps ``completed_at = now()`` for every terminal
    status. The salvage runs on an ALREADY-cancelled run, so that would move
    the terminal time forward (and inflate the recorded duration). The writer
    restores the recorded value; the double below simulates the re-stamp.
    """
    recorded = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    run = _make_run(node_token_usage=None, outputs_json=None)
    run.status = "cancelled"
    run.started_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    run.completed_at = recorded
    run.heartbeat_at = recorded
    run.total_cost_usd = None
    run.cost_breakdown = None
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run)))

    async def _restamping_update(*_args: Any, **_kwargs: Any) -> Any:
        # Mirrors ``_apply_run_claim_fields`` on a terminal write.
        run.completed_at = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
        return run

    markers = {"run:00000000-0000-0000-0000-000000000002:node:n1:1": _step_finish_marker()}
    with (
        patch(
            "modulo.core.cost_controller.finalize.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs=None, telemetry=None, markers=markers)),
        ),
        patch(
            "modulo.core.cost_controller.finalize.update_run_status",
            new=AsyncMock(side_effect=_restamping_update),
        ),
        patch("modulo.core.cost_controller.finalize._e2b_rate", return_value=Decimal("0.13")),
        patch("modulo.core.cost_controller.finalize._record_terminal_analytics", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=AsyncMock()),
    ):
        await salvage_cancelled_run_cost(session, run_id=run.id, org_id=_ORG_ID)

    assert run.completed_at == recorded


def test_attributed_cost_present_run_shapes() -> None:
    """Zero-total-with-no-breakdown is salvageable; anything attributed is not."""
    run = _make_run()
    run.total_cost_usd = None
    run.cost_breakdown = None
    assert _attributed_cost_present(run) is False

    run.total_cost_usd = Decimal("0.0")
    assert _attributed_cost_present(run) is False

    run.total_cost_usd = Decimal("0.065")
    assert _attributed_cost_present(run) is True

    run.total_cost_usd = None
    run.cost_breakdown = [{"component": "sandbox_infra", "amount_usd": "0.065"}]
    assert _attributed_cost_present(run) is True
