"""Metric counters for the cost breakdown engine — the single owning module.

Counters are ``modulo_``-prefixed and wired to the OTel meter provider (the
house pattern — see ``modulo.core.error_tracking.metrics``). All handles are
lazy-initialised so a missing meter provider never breaks the cost path.
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


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.cost_controller", version="0.1.0")
    except Exception:
        return None


def _ensure() -> None:
    global _eval_errors_total, _clamped_total, _out_of_band_high_total, _settings_warning_total
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
