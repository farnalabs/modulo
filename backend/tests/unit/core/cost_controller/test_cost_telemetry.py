"""Unit tests for the telemetry builder + param builder (breakdown/params).

Covers the node-classification authority (``build_telemetry``) — self-report
matching, orphan/missing/clamped detection, server-measured token summing, the
map-absent defaults — and ``build_params`` (rate resolution incl. the e2b
fallback). The classification defaults are pinned in params.py: a map-absent
node is sandbox for wall-clock summing and non-sandbox for self-report.
"""

from __future__ import annotations

from decimal import Decimal

from modulo.core.cost_controller.breakdown.params import (
    CostComponentConfig,
    RunCostTelemetry,
    build_params,
    build_telemetry,
)


def _comp(
    *,
    name: str = "model_tokens",
    kind: str = "self_reported",
    report_key: str = "model_cost_usd",
    enabled: bool = True,
) -> CostComponentConfig:
    return CostComponentConfig(
        name=name,
        display_name=name,
        kind=kind,
        report_key=report_key,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# build_telemetry — empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_build_telemetry_empty_inputs() -> None:
    tele, per_node_cost = build_telemetry(None, None)
    assert tele.node_count == 0
    assert tele.nodes_estimated == 0
    assert tele.wall_clock_elapsed_s == Decimal(0)
    assert not tele.reported
    assert not tele.missing_report_keys
    assert per_node_cost == {}

    tele2, per_node_cost2 = build_telemetry({}, [])
    assert tele2.node_count == 0
    assert per_node_cost2 == {}


def test_build_telemetry_skips_non_dict_entries() -> None:
    entries = {"n1": "not-a-dict", "n2": None, "n3": {"input_tokens": 3}}
    tele, per_node_cost = build_telemetry(entries, [_comp()])
    assert tele.node_count == 1
    assert tele.tokens_input == 3
    assert set(per_node_cost) == {"n3"}


# ---------------------------------------------------------------------------
# build_telemetry — wall clock + sandbox classification
# ---------------------------------------------------------------------------


def test_wall_clock_accumulates_int_and_float_ms() -> None:
    entries = {
        "n1": {"wall_clock_time_ms": 1000},
        "n2": {"wall_clock_time_ms": 2500.5},
        "n3": {"wall_clock_time_ms": 0},
        "n4": {"wall_clock_time_ms": "500"},
    }
    tele, _per_node_cost = build_telemetry(entries, [_comp()])
    assert tele.wall_clock_elapsed_s == Decimal("3.5005")


def test_sandbox_by_map_counts_eligible_nodes() -> None:
    entries = {
        "n1": {"sandbox_by_map": True},
        "n2": {"sandbox_by_map": False},
        "n3": {"sandbox_by_map": "yes"},
        "n4": {"sandbox_by_map": None},
    }
    tele, _per_node_cost = build_telemetry(entries, [_comp()])
    # Only the strict-True value increments the eligible count.
    assert tele.eligible_sandbox_node_count == 1


def test_map_absent_node_defaults_pinned() -> None:
    """A map-absent node is sandbox-for-wall-clock and non-eligible for self-report."""
    entries = {"n1": {"wall_clock_time_ms": 7200}}
    tele, per_node_cost = build_telemetry(entries, [_comp()])
    assert tele.wall_clock_elapsed_s == Decimal("7.2")
    assert tele.eligible_sandbox_node_count == 0
    # Routes to estimate (non-sandbox), no self-report.
    assert tele.nodes_estimated == 1
    assert per_node_cost["n1"] == Decimal(0)


# ---------------------------------------------------------------------------
# build_telemetry — self-report classification
# ---------------------------------------------------------------------------


def test_self_report_by_report_key() -> None:
    comps = [_comp(report_key="model_cost_usd")]
    entries = {
        "n1": {
            "sandbox_by_map": True,
            "model_cost_usd": 0.05,
            "model_cost_raw_usd": 0.049,
            "report_key": "model_cost_usd",
        }
    }
    tele, per_node_cost = build_telemetry(entries, comps)
    assert tele.reported == {"model_cost_usd": Decimal("0.05")}
    assert tele.raw_reported == {"n1": 0.049}
    assert per_node_cost == {"n1": Decimal("0.05")}
    assert tele.node_count == 1
    assert tele.nodes_estimated == 0
    assert not tele.missing_report_keys


def test_self_report_model_cost_usd_alias() -> None:
    comps = [_comp(report_key="model_cost_usd")]
    entries = {"n1": {"sandbox_by_map": True, "model_cost_usd": 0.04}}
    tele, per_node_cost = build_telemetry(entries, comps)
    assert tele.reported == {"model_cost_usd": Decimal("0.04")}
    assert per_node_cost["n1"] == Decimal("0.04")


def test_self_report_custom_report_key_without_node_key() -> None:
    comps = [_comp(report_key="custom_key")]
    entries = {"n1": {"sandbox_by_map": True, "model_cost_usd": 0.04}}
    tele, per_node_cost = build_telemetry(entries, comps)
    assert not tele.reported
    assert tele.nodes_estimated == 1
    assert per_node_cost["n1"] == Decimal(0)


def test_self_report_sums_multiple_nodes_and_marks_clamped() -> None:
    comps = [_comp(report_key="model_cost_usd")]
    entries = {
        "n1": {"sandbox_by_map": True, "model_cost_usd": 0.01, "report_key": "model_cost_usd"},
        "n2": {
            "sandbox_by_map": True,
            "model_cost_usd": 0.02,
            "model_cost_clamped": True,
            "report_key": "model_cost_usd",
        },
    }
    tele, per_node_cost = build_telemetry(entries, comps)
    assert tele.reported == {"model_cost_usd": Decimal("0.03")}
    assert tele.clamped_nodes == ["n2"]
    assert per_node_cost == {"n1": Decimal("0.01"), "n2": Decimal("0.02")}


def test_below_floor_routes_to_estimate_not_self_report() -> None:
    comps = [_comp(report_key="model_cost_usd")]
    entries = {"n1": {"sandbox_by_map": True, "model_cost_usd": 0.0000000001, "input_tokens": 5}}
    tele, per_node_cost = build_telemetry(entries, comps)
    assert not tele.reported
    assert tele.nodes_estimated == 1
    assert tele.tokens_input == 5
    assert tele.missing_report_keys == {"model_cost_usd"}
    assert per_node_cost["n1"] == Decimal("0.00005")


def test_sandbox_with_report_but_no_consuming_component_is_orphan() -> None:
    comps = [_comp(report_key="custom_key")]
    entries = {"n1": {"sandbox_by_map": True, "model_cost_usd": 0.05, "model_cost_raw_usd": 0.05}}
    tele, per_node_cost = build_telemetry(entries, comps)
    assert not tele.reported
    assert tele.orphan_report_nodes == ["n1"]
    assert tele.nodes_estimated == 1
    assert tele.missing_report_keys == {"custom_key"}
    assert per_node_cost["n1"] == Decimal(0)


def test_disabled_self_reported_component_is_not_consuming() -> None:
    comps = [_comp(report_key="model_cost_usd", enabled=False)]
    entries = {"n1": {"sandbox_by_map": True, "model_cost_usd": 0.05}}
    tele, _per_node_cost = build_telemetry(entries, comps)
    assert not tele.reported
    assert tele.orphan_report_nodes == ["n1"]
    assert not tele.missing_report_keys


def test_estimated_node_token_summing_is_server_measured() -> None:
    entries = {"n1": {"input_tokens": 100, "output_tokens": 50}}
    tele, per_node_cost = build_telemetry(entries, [_comp()])
    assert tele.tokens_input == 100
    assert tele.tokens_output == 50
    assert tele.tokens_estimated == 150
    assert tele.nodes_estimated == 1
    assert tele.node_count == 1
    # Estimated cost = server-measured token rates (agent-supplied tokens only
    # when they ARE the server count — no raw usd folded in).
    assert per_node_cost["n1"] == Decimal("0.00250")


def test_non_numeric_report_routes_to_estimate() -> None:
    comps = [_comp(report_key="model_cost_usd")]
    entries = {"n1": {"sandbox_by_map": True, "model_cost_usd": "garbage"}}
    tele, per_node_cost = build_telemetry(entries, comps)
    assert not tele.reported
    assert tele.nodes_estimated == 1
    assert tele.missing_report_keys == {"model_cost_usd"}
    assert per_node_cost["n1"] == Decimal(0)


def test_non_finite_report_routes_to_estimate() -> None:
    comps = [_comp(report_key="model_cost_usd")]
    entries = {"n1": {"sandbox_by_map": True, "model_cost_usd": Decimal("Infinity")}}
    tele, per_node_cost = build_telemetry(entries, comps)
    assert not tele.reported
    assert tele.nodes_estimated == 1
    assert tele.missing_report_keys == {"model_cost_usd"}
    assert per_node_cost["n1"] == Decimal(0)


def test_explicit_is_sandbox_for_wallclock_flag_does_not_gate_accumulation() -> None:
    """Wall-clock summing keys on positive ``wall_clock_time_ms`` only.

    The ``is_sandbox_for_wallclock`` flag is surfaced on the enriched union but
    is NOT a gate for the wall-clock sum in the current implementation: any
    node carrying a positive ``wall_clock_time_ms`` contributes regardless of
    the flag value.
    """
    entries = {
        "n1": {"is_sandbox_for_wallclock": False, "wall_clock_time_ms": 1000},
        "n2": {"is_sandbox_for_wallclock": True, "wall_clock_time_ms": 2000},
    }
    tele, _per_node_cost = build_telemetry(entries, [_comp()])
    assert tele.wall_clock_elapsed_s == Decimal(3)


def test_missing_report_keys_surfaced_without_eligible_nodes() -> None:
    """A consuming report_key absent from reported outputs is surfaced even with
    zero eligible sandbox nodes; the eligible-node gate is applied at breakdown
    render (see test_self_reported_omits_missing_flag_without_eligible_nodes)."""
    entries = {"n1": {"input_tokens": 3}}
    tele, _per_node_cost = build_telemetry(entries, [_comp(report_key="model_cost_usd")])
    assert tele.missing_report_keys == {"model_cost_usd"}


# ---------------------------------------------------------------------------
# build_telemetry — agent-reported token sums (FAR-491, DISPLAY-ONLY)
# ---------------------------------------------------------------------------


def test_build_telemetry_sums_reported_tokens_across_nodes() -> None:
    entries = {
        "n1": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_input_tokens": 100,
            "reported_output_tokens": 50,
            "reported_total_tokens": 150,
            "reported_cache_read_tokens": 10,
            "reported_cache_write_tokens": 2,
        },
        "n2": {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "reported_input_tokens": 23,
            "reported_output_tokens": 11,
            "reported_total_tokens": 34,
        },
    }
    tele, _per_node_cost = build_telemetry(entries, [_comp()])
    assert tele.tokens_input_reported == 123
    assert tele.tokens_output_reported == 61
    assert tele.tokens_total_reported == 184
    assert tele.tokens_cache_read_reported == 10
    assert tele.tokens_cache_write_reported == 2
    # Server-measured sums are unaffected by the reported fields.
    assert tele.tokens_input == 7
    assert tele.tokens_output == 3
    assert tele.tokens_estimated == 10


def test_build_telemetry_reported_sums_skip_invalid_values() -> None:
    """Tri-state at the sum: bool / non-int / negative reported values are
    skipped; a valid 0 contributes 0; absent keys contribute nothing."""
    entries = {
        "n1": {
            "reported_input_tokens": True,
            "reported_output_tokens": "many",
            "reported_total_tokens": -5,
            "reported_cache_read_tokens": 0,
        },
        "n2": {"reported_total_tokens": 9},
    }
    tele, _per_node_cost = build_telemetry(entries, [_comp()])
    assert tele.tokens_input_reported == 0
    assert tele.tokens_output_reported == 0
    assert tele.tokens_total_reported == 9
    assert tele.tokens_cache_read_reported == 0
    assert tele.tokens_cache_write_reported == 0


def test_build_telemetry_reported_sums_default_zero_without_union_keys() -> None:
    tele, _per_node_cost = build_telemetry({"n1": {"input_tokens": 3}}, [_comp()])
    assert tele.tokens_input_reported == 0
    assert tele.tokens_output_reported == 0
    assert tele.tokens_total_reported == 0
    assert tele.tokens_cache_read_reported == 0
    assert tele.tokens_cache_write_reported == 0


def test_build_params_exposes_reported_token_params() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        formula="tokens_total_reported",
    )
    tele = _tel(
        tokens_input_reported=10,
        tokens_output_reported=5,
        tokens_total_reported=15,
        tokens_cache_read_reported=2,
        tokens_cache_write_reported=1,
    )
    params = build_params(tele, comp)
    assert params["tokens_input_reported"] == Decimal(10)
    assert params["tokens_output_reported"] == Decimal(5)
    assert params["tokens_total_reported"] == Decimal(15)
    assert params["tokens_cache_read_reported"] == Decimal(2)
    assert params["tokens_cache_write_reported"] == Decimal(1)


# ---------------------------------------------------------------------------
# build_params — rate resolution + fallback
# ---------------------------------------------------------------------------


def _tel(**kw: object) -> RunCostTelemetry:
    defaults = {"wall_clock_elapsed_s": Decimal(0)}
    defaults.update(kw)
    return RunCostTelemetry(**defaults)


class _Settings:
    def __init__(self, e2b_rate: object) -> None:
        self.e2b_sandbox_usd_per_hour = e2b_rate


def test_build_params_rate_from_component() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        rate_usd=Decimal("1.5"),
        formula="rate * wall_clock_hours",
    )
    params = build_params(_tel(wall_clock_elapsed_s=Decimal(3600)), comp)
    assert params["rate"] == Decimal("1.5")
    assert params["wall_clock_hours"] == Decimal(1)
    assert "reported" not in params


def test_build_params_wall_clock_seconds_to_hours() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra", display_name="Sandbox", kind="calculated", formula="wall_clock_hours"
    )
    params = build_params(_tel(wall_clock_elapsed_s=Decimal(5400)), comp)
    assert params["wall_clock_hours"] == Decimal("1.5")


def test_build_params_e2b_fallback_from_settings() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        rate_fallback="e2b_rate",
        formula="rate * wall_clock_hours",
    )
    params = build_params(_tel(), comp, settings=_Settings("0.5"))
    assert params["rate"] == Decimal("0.5")


def test_build_params_rate_usd_takes_precedence_over_fallback() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        rate_usd=Decimal("2.0"),
        rate_fallback="e2b_rate",
        formula="rate * wall_clock_hours",
    )
    params = build_params(_tel(), comp, settings=_Settings("0.5"))
    assert params["rate"] == Decimal("2.0")


def test_build_params_unregistered_fallback_is_ignored() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        rate_fallback="typo_rate",
        formula="rate * wall_clock_hours",
    )
    params = build_params(_tel(), comp, settings=_Settings("0.5"))
    assert "rate" not in params


def test_build_params_broken_fallback_value_omits_rate() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        rate_fallback="e2b_rate",
        formula="rate * wall_clock_hours",
    )
    params = build_params(_tel(), comp, settings=_Settings("not-a-decimal"))
    assert "rate" not in params


def test_build_params_non_numeric_rate_omitted() -> None:
    comp = CostComponentConfig(
        name="sandbox_infra",
        display_name="Sandbox",
        kind="calculated",
        rate_usd=True,
        formula="rate * wall_clock_hours",
    )
    params = build_params(_tel(), comp)
    assert "rate" not in params


def test_build_params_self_reported_report_key_param() -> None:
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    tele = _tel(reported={"model_cost_usd": Decimal(2)})
    params = build_params(tele, comp)
    assert params["reported"] == Decimal(2)


def test_build_params_self_reported_missing_key_zero() -> None:
    comp = CostComponentConfig(
        name="model_tokens",
        display_name="Model cost",
        kind="self_reported",
        report_key="model_cost_usd",
    )
    params = build_params(_tel(reported={}), comp)
    assert params["reported"] == Decimal(0)
