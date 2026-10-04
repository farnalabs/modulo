"""Unit tests for run-level cost warnings (missing self-report surfacing).

Pins the fixes for the phantom ``$0.000000`` "Model cost (self-reported)" row:
* ``build_cost_breakdown`` stamps a CLEAR ``missing_self_report_reason`` on a
  zero-amount missing self-report (a non-billing state, not a normal money line);
* ``compute_run_warnings`` / ``compute_run_warnings_count`` derive a run-level
  warning surface from a stored ``cost_breakdown``.

FAR-1305 adds the second, truthful reason: a node that PRESENTED an explicit
``0.0`` which the trust boundary rejected as unproven is not "the agent never
reported" — it is ``zero_report_unproven``.

FAR-1308 adds the third: a node that PRESENTED a positive value BELOW the
reportable floor (e.g. ``0.0000005``) was also refused — it is
``sub_floor_rejected``, not ``agent_not_reported``. The trust boundary is
unchanged in every case: which reports are accepted is identical before and
after; only the LABEL on a missing self-report differs.
"""

from __future__ import annotations

from decimal import Decimal

from modulo.core.cost_controller.breakdown.aggregate import build_cost_breakdown
from modulo.core.cost_controller.breakdown.params import (
    CostComponentConfig,
    RunCostTelemetry,
    build_telemetry,
    compute_run_warnings,
    compute_run_warnings_count,
)
from modulo.core.cost_controller.finalize import (
    _enrich_union,
    _rejected_zero_nodes,
    _rejection_reasons,
)


def _self_reported_comp() -> CostComponentConfig:
    return CostComponentConfig(
        name="model_tokens",
        display_name="Model cost (self-reported)",
        kind="self_reported",
        report_key="model_cost_usd",
    )


def _missing_breakdown() -> list[dict[str, object]]:
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=1,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, _ = build_cost_breakdown(tele, [_self_reported_comp()])
    return breakdown


def test_missing_self_report_zero_amount_is_clear_non_billing_state() -> None:
    """The zero-amount missing entry signals 'no self-reported cost', not a $0 line."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=1,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, total = build_cost_breakdown(tele, [_self_reported_comp()])
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["missing_self_report"] is True
    assert entry["missing_self_report_reason"] == "agent_not_reported"
    assert entry["amount_usd"] == "0.000000"
    assert total == Decimal(0)


def test_reported_self_report_has_no_missing_reason() -> None:
    """A real self-report never carries the missing-reason stamp."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.04")},
        raw_reported={"node1": 0.0412},
        eligible_sandbox_node_count=1,
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()])
    entry = breakdown[0]
    assert entry["missing_self_report"] is False
    assert "missing_self_report_reason" not in entry


def test_no_eligible_nodes_omits_missing_reason() -> None:
    """No eligible sandbox nodes means no missing-report stamp at all."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=0,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()])
    entry = breakdown[0]
    assert "missing_self_report" not in entry
    assert "missing_self_report_reason" not in entry


def test_compute_run_warnings_emits_missing_self_report() -> None:
    warnings = compute_run_warnings(_missing_breakdown())
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["code"] == "missing_self_report"
    assert warning["severity"] == "warning"
    assert isinstance(warning["message"], str)
    assert warning["message"]


def test_compute_run_warnings_empty_for_normal_report() -> None:
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={"model_cost_usd": Decimal("0.04")},
        raw_reported={"node1": 0.0412},
        eligible_sandbox_node_count=1,
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()])
    assert not compute_run_warnings(breakdown)


def test_genuine_zero_report_renders_zero_without_warning() -> None:
    """FAR-653: a PROVEN genuine-zero self-report (classification records the
    report key with amount 0) renders a $0.000000 REAL line with NO
    missing_self_report flag and NO run warning — the phantom-warning class
    for a legitimately-$0.00 run is gone."""
    comps = [_self_reported_comp()]
    entries = {
        "node1": {
            "sandbox_by_map": True,
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "reported_input_tokens": 0,
            "reported_output_tokens": 0,
            "reported_total_tokens": 0,
        }
    }
    tele, _per_node_cost = build_telemetry(entries, comps)
    breakdown, total = build_cost_breakdown(tele, comps)
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["missing_self_report"] is False
    assert "missing_self_report_reason" not in entry
    assert entry["amount_usd"] == "0.000000"
    assert total == Decimal(0)
    assert not compute_run_warnings(breakdown)
    assert compute_run_warnings_count(breakdown) == 0


def test_compute_run_warnings_handles_non_list() -> None:
    assert not compute_run_warnings(None)
    assert not compute_run_warnings("not-a-list")
    assert not compute_run_warnings({"component": "x"})


def test_compute_run_warnings_skips_non_dict_entries() -> None:
    assert not compute_run_warnings(["garbage", 42, None])


def test_compute_run_warnings_count_matches_length() -> None:
    assert compute_run_warnings_count(_missing_breakdown()) == 1
    assert compute_run_warnings_count(None) == 0


# --- FAR-1305: a rejected EXPLICIT zero is not "the agent did not report" ---


def _free_model_node_shapes() -> tuple[dict, dict, dict]:
    """The observed free-model shape (prod run e927db10).

    The producer wrote ``model_cost_usd: 0.0`` into ``output.json`` — the PURE
    RETURN — alongside REAL token usage. Producer-stage extraction refuses the
    unproven zero, so the telemetry envelope carries NO cost key at all; the
    raw ``0.0`` survives only in the pure return.
    """
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {
        "node1": {
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "token_usage": {"input": 35170, "output": 3669, "total": 38839},
        }
    }
    telemetry = {
        "node1": {
            "status": "completed",
            "wall_clock_time_ms": 12_050,
            "model_tokens_input": 35170,
            "model_tokens_output": 3669,
            "model_tokens_total": 38839,
        }
    }
    return usage, outputs, telemetry


def test_rejected_explicit_zero_stamps_rejection_marker() -> None:
    """The enrichment records THAT an explicit zero was presented and rejected.

    The trust boundary is unchanged: the unproven zero is still NOT folded in
    as a report — only the diagnostic marker is added so the reason can be
    truthful downstream.
    """
    usage, outputs, telemetry = _free_model_node_shapes()
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    entry = union["node1"]
    assert entry.get("model_cost_rejected") is True
    # Acceptance criterion 3 — which reports are accepted is UNCHANGED.
    assert "model_cost_usd" not in entry
    assert _rejected_zero_nodes(union) == {"node1"}


def test_rejected_explicit_zero_reason_is_zero_report_unproven() -> None:
    """Acceptance criterion 1: explicit 0.0 + NON-zero tokens -> the truthful reason.

    The breakdown must not claim the agent stayed silent when it demonstrably
    reported ``$0.00`` and the trust boundary refused the report as unproven.
    """
    usage, outputs, telemetry = _free_model_node_shapes()
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele, [_self_reported_comp()], rejected_zero_nodes=_rejected_zero_nodes(union)
    )
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["missing_self_report"] is True
    assert entry["missing_self_report_reason"] == "zero_report_unproven"
    assert entry["amount_usd"] == "0.000000"


def test_absent_cost_key_still_reports_agent_not_reported() -> None:
    """Acceptance criterion 2 — a node with NO cost key keeps the old reason."""
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {"node1": {"status": "completed", "summary": "no cost key at all"}}
    telemetry = {"node1": {"status": "completed", "wall_clock_time_ms": 500}}
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    assert "model_cost_rejected" not in union["node1"]
    assert not _rejected_zero_nodes(union)
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele, [_self_reported_comp()], rejected_zero_nodes=_rejected_zero_nodes(union)
    )
    entry = breakdown[0]
    assert entry["missing_self_report"] is True
    assert entry["missing_self_report_reason"] == "agent_not_reported"


def test_output_carried_unproven_zero_stamps_rejection_marker() -> None:
    """Second stamp site: a row whose OUTPUT (not just its pure return) carries
    the unproven zero — the legacy / no-split-telemetry shape, where
    ``_fold_from_output_obj``'s reject branch fires."""
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {
        "node1": {
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "model_tokens_input": 5,
            "model_tokens_output": 5,
            "model_tokens_total": 10,
        }
    }
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True)
    entry = union["node1"]
    assert entry.get("model_cost_rejected") is True
    assert "model_cost_usd" not in entry


def test_rejected_zero_reason_is_not_emitted_without_eligible_node() -> None:
    """No eligible sandbox node -> no ``missing_self_report`` block at all, so
    neither reason is emitted (unchanged FAR-653 gating)."""
    tele = RunCostTelemetry(
        wall_clock_elapsed_s=Decimal(0),
        reported={},
        eligible_sandbox_node_count=0,
        missing_report_keys={"model_cost_usd"},
    )
    breakdown, _total = build_cost_breakdown(tele, [_self_reported_comp()], rejected_zero_nodes={"node1"})
    assert "missing_self_report" not in breakdown[0]
    assert "missing_self_report_reason" not in breakdown[0]


def _rejected_zero_breakdown() -> list[dict[str, object]]:
    """The stored breakdown of a run whose explicit ``$0.00`` was refused."""
    usage, outputs, telemetry = _free_model_node_shapes()
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele, [_self_reported_comp()], rejected_zero_nodes=_rejected_zero_nodes(union)
    )
    return breakdown


def test_run_warning_message_for_rejected_zero_is_truthful() -> None:
    """FAR-1305 option A, second surface: the API/MCP fallback message.

    ``compute_run_warnings``'s plain-text fallback used to say "The agent did
    not report a model cost for this run." for BOTH missing states. For a
    rejected explicit zero the agent DID report $0.00 - the message must say
    the zero was refused, not that nothing was reported.
    """
    warnings = compute_run_warnings(_rejected_zero_breakdown())
    assert len(warnings) == 1
    message = warnings[0]["message"]
    assert message != "The agent did not report a model cost for this run."
    # The truthful claim: a $0.00 report was made AND rejected as unproven.
    assert "$0.00" in message
    assert "rejected" in message
    assert "did not report" not in message


def test_run_warning_message_keeps_old_text_for_absent_cost_key() -> None:
    """Acceptance criterion 2 at this surface too - a genuinely absent report
    keeps the original "did not report" message (a deliberate, unchanged
    string; the spec pins it)."""
    warnings = compute_run_warnings(_missing_breakdown())
    assert len(warnings) == 1
    assert warnings[0]["message"] == "The agent did not report a model cost for this run."


def test_run_warning_message_without_reason_stamp_keeps_old_text() -> None:
    """A pre-FAR-1305 stored breakdown carries no reason stamp at all - it must
    not be misreported as a rejected zero, so it keeps the original message."""
    warnings = compute_run_warnings(
        [
            {
                "source": "self_reported",
                "missing_self_report": True,
                # no ``missing_self_report_reason`` key, as stored before FAR-1305
            }
        ]
    )
    assert len(warnings) == 1
    assert warnings[0]["message"] == "The agent did not report a model cost for this run."


# ---------------------------------------------------------------------------
# FAR-1308: a positive-but-SUB-FLOOR report is presented and refused too
# ---------------------------------------------------------------------------

#: A positive value BELOW ``MAX_REPORTABLE_USD_MIN`` (0.000001) - the FAR-1308
#: defect shape: the agent DID report, the trust boundary correctly refused it
#: as implausibly small, but nothing recorded WHY.
SUB_FLOOR_COST = 0.0000005


def _sub_floor_node_shapes() -> tuple[dict, dict, dict]:
    """The FAR-1308 shape: a sandbox node presenting a positive sub-floor cost.

    Mirrors ``_free_model_node_shapes`` exactly, except the presented value is
    ``0.0000005`` (positive, finite, below the floor) instead of ``0.0``.
    """
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {
        "node1": {
            "model_cost_usd": SUB_FLOOR_COST,
            "model_cost_raw_usd": SUB_FLOOR_COST,
            "token_usage": {"input": 35170, "output": 3669, "total": 38839},
        }
    }
    telemetry = {
        "node1": {
            "status": "completed",
            "wall_clock_time_ms": 12_050,
            "model_tokens_input": 35170,
            "model_tokens_output": 3669,
            "model_tokens_total": 38839,
        }
    }
    return usage, outputs, telemetry


def _sub_floor_union() -> dict[str, dict]:
    usage, outputs, telemetry = _sub_floor_node_shapes()
    return _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)


def test_sub_floor_report_stamps_sub_floor_rejection_marker() -> None:
    """A positive sub-floor report is REFUSED (unchanged) but now MARKED.

    Acceptance criterion: the enrichment records THAT a sub-floor value was
    presented and refused, with the reason ``sub_floor_rejected`` - while the
    trust boundary is untouched (the value is still not folded in).
    """
    union = _sub_floor_union()
    entry = union["node1"]
    assert entry.get("model_cost_rejected") is True
    assert entry.get("model_cost_rejection_reason") == "sub_floor_rejected"
    # The boundary is UNCHANGED: still not counted.
    assert "model_cost_usd" not in entry
    assert _rejection_reasons(union) == {"node1": "sub_floor_rejected"}


def test_sub_floor_report_reason_is_sub_floor_rejected_in_breakdown() -> None:
    """The breakdown must not claim ``agent_not_reported`` for a sub-floor report.

    The agent demonstrably reported a positive model cost; the value was
    refused for being below the countable floor, not for being absent.
    """
    union = _sub_floor_union()
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele,
        [_self_reported_comp()],
        rejected_zero_nodes=_rejected_zero_nodes(union),
        rejection_reasons=_rejection_reasons(union),
    )
    entry = breakdown[0]
    assert entry["source"] == "self_reported"
    assert entry["missing_self_report"] is True
    assert entry["missing_self_report_reason"] == "sub_floor_rejected"
    assert entry["amount_usd"] == "0.000000"


def test_rejected_zero_nodes_excludes_sub_floor_nodes() -> None:
    """``_rejected_zero_nodes`` stays truthful to its name (zeros only).

    The sub-floor node is carried by the NEW ``_rejection_reasons`` map
    instead; keeping it out of the zero set means a legacy boolean-only
    caller can never mislabel a sub-floor report as ``zero_report_unproven``.
    """
    union = _sub_floor_union()
    assert not _rejected_zero_nodes(union)
    assert _rejection_reasons(union) == {"node1": "sub_floor_rejected"}


def test_run_warning_message_for_sub_floor_is_truthful() -> None:
    """``compute_run_warnings`` must NOT say "did not report" for a sub-floor report.

    The whole FAR-1308 defect: the false claim
    "The agent did not report a model cost for this run." was rendered for a
    value the agent DID report.
    """
    usage, outputs, telemetry = _sub_floor_node_shapes()
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele,
        [_self_reported_comp()],
        rejected_zero_nodes=_rejected_zero_nodes(union),
        rejection_reasons=_rejection_reasons(union),
    )
    warnings = compute_run_warnings(breakdown)
    assert len(warnings) == 1
    message = warnings[0]["message"]
    assert message != "The agent did not report a model cost for this run."
    assert "did not report" not in message
    # Truthful: a report was made but was too small to count.
    assert "below" in message


def test_absent_cost_key_still_reports_agent_not_reported_with_reason_map() -> None:
    """An absent cost key with an EMPTY reason map stays ``agent_not_reported``."""
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {"node1": {"status": "completed", "summary": "no cost key at all"}}
    telemetry = {"node1": {"status": "completed", "wall_clock_time_ms": 500}}
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    assert not _rejection_reasons(union)
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele,
        [_self_reported_comp()],
        rejected_zero_nodes=_rejected_zero_nodes(union),
        rejection_reasons=_rejection_reasons(union),
    )
    assert breakdown[0]["missing_self_report_reason"] == "agent_not_reported"


def test_zero_report_still_stamps_zero_report_unproven_with_reason_map() -> None:
    """FAR-1305 behaviour is preserved when the reason map is threaded (regression)."""
    usage, outputs, telemetry = _free_model_node_shapes()
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    assert _rejection_reasons(union) == {"node1": "zero_report_unproven"}
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele,
        [_self_reported_comp()],
        rejected_zero_nodes=_rejected_zero_nodes(union),
        rejection_reasons=_rejection_reasons(union),
    )
    assert breakdown[0]["missing_self_report_reason"] == "zero_report_unproven"


def test_legacy_output_carried_sub_floor_stamps_marker() -> None:
    """Second stamp site: the legacy / no-split-telemetry shape where the
    OUTPUT itself carries the sub-floor value (``_fold_from_output_obj``'s
    reject branch) must stamp the marker too."""
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {
        "node1": {
            "model_cost_usd": SUB_FLOOR_COST,
            "model_cost_raw_usd": SUB_FLOOR_COST,
            "model_tokens_input": 5,
            "model_tokens_output": 5,
            "model_tokens_total": 10,
        }
    }
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True)
    entry = union["node1"]
    assert entry.get("model_cost_rejected") is True
    assert entry.get("model_cost_rejection_reason") == "sub_floor_rejected"
    # Boundary unchanged: not counted.
    assert "model_cost_usd" not in entry


def test_mixed_zero_and_sub_floor_nodes_prefer_no_false_silence() -> None:
    """A component where one node presented $0.00 and another a sub-floor value
    must not fall back to ``agent_not_reported`` (the pre-fix behaviour)."""
    usage = {
        "node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "node2": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }
    outputs = {
        "node1": {
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": 0.0,
            "token_usage": {"input": 10, "output": 5, "total": 15},
        },
        "node2": {
            "model_cost_usd": SUB_FLOOR_COST,
            "model_cost_raw_usd": SUB_FLOOR_COST,
            "token_usage": {"input": 10, "output": 5, "total": 15},
        },
    }
    telemetry = {
        "node1": {"status": "completed", "wall_clock_time_ms": 100},
        "node2": {"status": "completed", "wall_clock_time_ms": 100},
    }
    union = _enrich_union(
        usage,
        outputs,
        {"node1": "sandbox_agent", "node2": "sandbox_agent"},
        is_terminal=True,
        merged_telemetry=telemetry,
    )
    tele, _per_node_cost = build_telemetry(union, [_self_reported_comp()])
    breakdown, _total = build_cost_breakdown(
        tele,
        [_self_reported_comp()],
        rejected_zero_nodes=_rejected_zero_nodes(union),
        rejection_reasons=_rejection_reasons(union),
    )
    # At least one rejected node exists -> never the false "not reported".
    assert breakdown[0]["missing_self_report_reason"] != "agent_not_reported"


def test_zero_and_sub_floor_candidates_stamp_zero_report_unproven() -> None:
    """FAR-1308 PRECEDENCE: an exact zero wins over a sub-floor candidate.

    One sandbox-eligible node presents BOTH refusal candidates at once —
    ``model_cost_usd: 0.0`` (exact zero) and ``model_cost_raw_usd: 0.0000005``
    (positive, below ``MAX_REPORTABLE_USD_MIN``). ``_classify_refusal_reason``
    must return ``zero_report_unproven``: the exact zero is the older, more
    specific FAR-1305 claim, so it is checked FIRST. Reordering the two
    ``if`` checks makes this stamp ``sub_floor_rejected`` and fails here.

    The trust boundary is untouched either way — the report is refused and
    only the LABEL differs.
    """
    usage = {"node1": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    outputs = {
        "node1": {
            "model_cost_usd": 0.0,
            "model_cost_raw_usd": SUB_FLOOR_COST,
            "token_usage": {"input": 35170, "output": 3669, "total": 38839},
        }
    }
    telemetry = {
        "node1": {
            "status": "completed",
            "wall_clock_time_ms": 12_050,
            "model_tokens_input": 35170,
            "model_tokens_output": 3669,
            "model_tokens_total": 38839,
        }
    }
    union = _enrich_union(usage, outputs, {"node1": "sandbox_agent"}, is_terminal=True, merged_telemetry=telemetry)
    entry = union["node1"]
    assert entry.get("model_cost_rejected") is True
    assert entry.get("model_cost_rejection_reason") == "zero_report_unproven"
    # The boundary is UNCHANGED: the refused report is still not counted.
    assert "model_cost_usd" not in entry


def test_rejection_reasons_defaults_legacy_marker_to_zero_report_unproven() -> None:
    """A pre-FAR-1308 marker (rejected, NO reason string) reads as a zero report.

    ``model_cost_rejected: True`` without ``model_cost_rejection_reason`` could
    only ever have been written by pre-FAR-1308 code, whose single refusal class
    was the unproven explicit zero — so the default must be
    ``zero_report_unproven``, never a fabricated ``sub_floor_rejected`` and
    never an absent entry (which would fall back to the false
    ``agent_not_reported``).
    """
    enriched: dict[str, dict[str, object]] = {"n1": {"model_cost_rejected": True}}
    assert _rejection_reasons(enriched) == {"n1": "zero_report_unproven"}
