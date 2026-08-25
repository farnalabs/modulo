"""Metric counters for the cost breakdown engine — the single owning module.

Counters are ``modulo_``-prefixed and wired to the OTel meter provider (the
house pattern — see ``modulo.core.error_tracking.metrics``). All handles are
lazy-initialised so a missing meter provider never breaks the cost path.

The metric inventory (names + labels) is canonical in the distilled spec
§9.3; the semantics of the probe signals and the ledger counters are pinned in
§4.7 / §4.2. ``modulo_cost_probe_last_success_ts`` is the DELIBERATELY
RETAINED single gauge in the inventory.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# Module-level metric handles — initialised lazily.
_eval_errors_total: Any = None
_clamped_total: Any = None
_out_of_band_high_total: Any = None
_settings_warning_total: Any = None
_fallback_legacy_total: Any = None
_ledger_clamped_total: Any = None
_ledger_refused_clamped_total: Any = None
_finalize_deferred_total: Any = None
_limit_refused_total: Any = None
_duplicate_terminal_total: Any = None
_probe_mismatch_runs_total: Any = None
_probe_total_eq_mismatch_total: Any = None
_probe_clamped_skip_total: Any = None
_probe_missing_ledger_row_total: Any = None
_probe_last_success_ts: Any = None
_schema_drift_total: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.cost_controller", version="0.1.0")
    except Exception:
        _log.debug("cost_controller.metrics.meter_unavailable")
        return None


def _ensure() -> None:
    global \
        _eval_errors_total, \
        _clamped_total, \
        _out_of_band_high_total, \
        _settings_warning_total, \
        _fallback_legacy_total, \
        _ledger_clamped_total, \
        _ledger_refused_clamped_total, \
        _finalize_deferred_total, \
        _limit_refused_total, \
        _duplicate_terminal_total, \
        _probe_mismatch_runs_total, \
        _probe_total_eq_mismatch_total, \
        _probe_clamped_skip_total, \
        _probe_missing_ledger_row_total, \
        _probe_last_success_ts, \
        _schema_drift_total
    if _eval_errors_total is not None:
        return
    meter = _get_meter()
    if meter is None:
        return
    _eval_errors_total = meter.create_counter(
        name="modulo_cost_components_eval_errors_total",
        description="Formula evaluation errors by component",
        unit="1",
    )
    _clamped_total = meter.create_counter(
        name="modulo_cost_components_clamped_total",
        description="Cost values clamped, by kind (total_flat_clamp | band | per_node)",
        unit="1",
    )
    _out_of_band_high_total = meter.create_counter(
        name="modulo_cost_components_out_of_band_high",
        description="Self-reported model costs above the band ceiling, by direction",
        unit="1",
    )
    _settings_warning_total = meter.create_counter(
        name="modulo_cost_settings_warning_total",
        description="First-finalization near-ceiling settings warnings",
        unit="1",
    )
    _fallback_legacy_total = meter.create_counter(
        name="modulo_cost_components_fallback_legacy_total",
        description="Finalizations that degraded to the legacy wall-clock fallback",
        unit="1",
    )
    _ledger_clamped_total = meter.create_counter(
        name="modulo_cost_ledger_clamped_total",
        description="Daily ledger rows stored at the column ceiling (started-at day)",
        unit="1",
    )
    _ledger_refused_clamped_total = meter.create_counter(
        name="modulo_cost_ledger_refused_clamped_total",
        description="Refused-spend accumulation clamped to the column ceiling",
        unit="1",
    )
    _finalize_deferred_total = meter.create_counter(
        name="modulo_cost_ledger_finalize_deferred_total",
        description="Ledger write failures by reason and team (reason='write_failure' in v1)",
        unit="1",
    )
    _limit_refused_total = meter.create_counter(
        name="modulo_cost_ledger_limit_refused_total",
        description="Permanent daily-limit refusals by team ('none' for NULL-owner)",
        unit="1",
    )
    _duplicate_terminal_total = meter.create_counter(
        name="modulo_cost_ledger_duplicate_terminal_total",
        description="Duplicate-terminal guard firings (the flood trigger's counter)",
        unit="1",
    )
    _probe_mismatch_runs_total = meter.create_counter(
        name="modulo_cost_probe_mismatch_runs_total",
        description="Sampled runs where total_cost_usd != sum(component.amount_usd)",
        unit="1",
    )
    _probe_total_eq_mismatch_total = meter.create_counter(
        name="modulo_cost_probe_total_eq_mismatch_total",
        description="Samples that flagged at least one mismatching run (sample-level)",
        unit="1",
    )
    _probe_clamped_skip_total = meter.create_counter(
        name="modulo_cost_probe_clamped_skip_total",
        description="Marker-bearing (total-clamped) runs skipped by the probe comparison",
        unit="1",
    )
    _probe_missing_ledger_row_total = meter.create_counter(
        name="modulo_cost_probe_missing_ledger_row_total",
        description="Sampled runs whose org ledger row is absent or insufficient (WATCH, not a trigger)",
        unit="1",
    )
    _probe_last_success_ts = meter.create_gauge(
        name="modulo_cost_probe_last_success_ts",
        description="Heartbeat: last successful probe sample (epoch seconds)",
        unit="1",
    )
    _schema_drift_total = meter.create_counter(
        name="modulo_cost_opencode_schema_drift_total",
        description="opencode session-table schema drift detected in node output (terminal, pin OK, sandbox-by-map)",
        unit="1",
    )


def record_eval_error(component: str) -> None:
    if _eval_errors_total is None:
        _ensure()
    if _eval_errors_total is not None:
        _eval_errors_total.add(1, attributes={"component": component})


def record_clamped(kind: str) -> None:
    if _clamped_total is None:
        _ensure()
    if _clamped_total is not None:
        _clamped_total.add(1, attributes={"kind": kind})


def record_out_of_band(direction: str) -> None:
    if _out_of_band_high_total is None:
        _ensure()
    if _out_of_band_high_total is not None:
        _out_of_band_high_total.add(1, attributes={"direction": direction})


def record_settings_warning() -> None:
    if _settings_warning_total is None:
        _ensure()
    if _settings_warning_total is not None:
        _settings_warning_total.add(1)


def record_fallback_legacy() -> None:
    if _fallback_legacy_total is None:
        _ensure()
    if _fallback_legacy_total is not None:
        _fallback_legacy_total.add(1)


def record_ledger_clamped() -> None:
    if _ledger_clamped_total is None:
        _ensure()
    if _ledger_clamped_total is not None:
        _ledger_clamped_total.add(1)


def record_ledger_refused_clamped() -> None:
    if _ledger_refused_clamped_total is None:
        _ensure()
    if _ledger_refused_clamped_total is not None:
        _ledger_refused_clamped_total.add(1)


def record_finalize_deferred(reason: str, team: str) -> None:
    if _finalize_deferred_total is None:
        _ensure()
    if _finalize_deferred_total is not None:
        _finalize_deferred_total.add(1, attributes={"reason": reason, "team": team})


def record_limit_refused(team: str) -> None:
    if _limit_refused_total is None:
        _ensure()
    if _limit_refused_total is not None:
        _limit_refused_total.add(1, attributes={"team": team})


def record_duplicate_terminal() -> None:
    if _duplicate_terminal_total is None:
        _ensure()
    if _duplicate_terminal_total is not None:
        _duplicate_terminal_total.add(1)


def record_probe_mismatch_runs(count: int = 1) -> None:
    if _probe_mismatch_runs_total is None:
        _ensure()
    if _probe_mismatch_runs_total is not None:
        _probe_mismatch_runs_total.add(count)


def record_probe_total_eq_mismatch() -> None:
    if _probe_total_eq_mismatch_total is None:
        _ensure()
    if _probe_total_eq_mismatch_total is not None:
        _probe_total_eq_mismatch_total.add(1)


def record_probe_clamped_skip(count: int = 1) -> None:
    if _probe_clamped_skip_total is None:
        _ensure()
    if _probe_clamped_skip_total is not None:
        _probe_clamped_skip_total.add(count)


def record_probe_missing_ledger_row(count: int = 1) -> None:
    if _probe_missing_ledger_row_total is None:
        _ensure()
    if _probe_missing_ledger_row_total is not None:
        _probe_missing_ledger_row_total.add(count)


def set_probe_last_success_ts(epoch: float) -> None:
    if _probe_last_success_ts is None:
        _ensure()
    if _probe_last_success_ts is not None:
        _probe_last_success_ts.set(epoch)


def record_schema_drift() -> None:
    if _schema_drift_total is None:
        _ensure()
    if _schema_drift_total is not None:
        _schema_drift_total.add(1)
