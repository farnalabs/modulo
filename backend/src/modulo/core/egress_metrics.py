"""OpenTelemetry metrics for the shared egress layer (FAR-526D).

Mirrors the ``modulo.core.error_tracking.metrics`` /
``modulo.connectors.rest.rest_metrics`` pattern: module-level handles
initialised lazily from OTel's ``get_meter_provider()``, every operation no-ops
when no meter provider is configured or the SDK does not support an instrument,
and every call is exception-swallowed so a metric can never surface as an egress
failure. The shared egress layer (``core/ssrf.py``) emits these so pinning / SSRF
rejection is observable for ALL connectors and model backends — not just the REST
connector's own ``modulo_rest_ssrf_blocked_total``.

METRIC NAMES (stable, closed — never rename, adding a value is semver-visible)
----------------
``modulo_egress_pinned_total``    — one per pinned client/transport built, labelled
    ``host`` + ``connector_type``.
``modulo_egress_rejected_total``  — one per SSRF/egress rejection, labelled
    ``host`` + ``connector_type`` + ``reason``.

REASON TAXONOMY (stable, closed)
----------------
``blocked``    — the target/its resolution is private/internal (resolve/
    validate fail-closed; also malformed host / embedded userinfo / non-canonical
    IP literal).
``unpinned``   — the pinned transport refused a host outside its pin map
    (:class:`modulo.core.ssrf.UnpinnedHostError`) — e.g. a redirect hop.
``dns-timeout``— DNS resolution exceeded ``SSRF_DNS_TIMEOUT`` (fail-closed).
``dns-failed`` — DNS resolution failed with an ``OSError`` (fail-closed).
``bad-scheme`` — the URL used a non-http(s) scheme.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# ── Stable reason taxonomy (closed set) ─────────────────────────────────────
# Never rename a shipped value. Add a new value only when a genuinely new egress
# reject path lands.

REASON_BLOCKED = "blocked"
REASON_UNPINNED = "unpinned"
REASON_DNS_TIMEOUT = "dns-timeout"
REASON_DNS_FAILED = "dns-failed"
REASON_BAD_SCHEME = "bad-scheme"

# All reasons a metric consumer may see; kept for validation/documentation.
_REASONS = frozenset(
    {
        REASON_BLOCKED,
        REASON_UNPINNED,
        REASON_DNS_TIMEOUT,
        REASON_DNS_FAILED,
        REASON_BAD_SCHEME,
    }
)

# Default connector_type label for a call site that has not yet been given an
# explicit type (see the per-connector staged rollout in docs/architecture.md).
DEFAULT_CONNECTOR_TYPE = "unknown"

# Module-level handles — initialised once by _init().
_pinned_total: Any = None
_rejected_total: Any = None


def _get_meter() -> Any:
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.egress", version="0.1.0")
    except Exception:
        return None


def _init() -> None:
    global _pinned_total, _rejected_total

    if _pinned_total is not None and _rejected_total is not None:
        return

    meter = _get_meter()
    if meter is None:
        return

    try:
        _pinned_total = meter.create_counter(
            name="modulo_egress_pinned_total",
            description="Total pinned egress clients/transports built, labelled by destination host and connector type",
            unit="1",
        )
    except Exception:
        _log.warning("egress_metrics.pinned_counter_failed")

    try:
        _rejected_total = meter.create_counter(
            name="modulo_egress_rejected_total",
            description="Total egress requests rejected by the SSRF guard, labelled by host, connector type and reason",
            unit="1",
        )
    except Exception:
        _log.warning("egress_metrics.rejected_counter_failed")

    _log.info("egress_metrics.registered")


def record_pinned(connector_type: str, host: str) -> None:
    """Record one pinned egress client/transport build for ``host``.

    ``connector_type`` defaults to :data:`DEFAULT_CONNECTOR_TYPE` at the call
    site when a connector has not yet been labelled (see the staged rollout).
    """
    if _pinned_total is None:
        _init()
    if _pinned_total is not None:
        _pinned_total.add(
            1,
            attributes={"host": host, "connector_type": connector_type or DEFAULT_CONNECTOR_TYPE},
        )


def record_rejected(connector_type: str, host: str, reason: str) -> None:
    """Record one egress request rejected by the SSRF guard.

    ``reason`` must be one of :data:`_REASONS`; callers should pass the matching
    module constant. Labels make the rejection attributable to the destination
    host, the connector (or ``unknown`` pre-rollout) and the reason.
    """
    if _rejected_total is None:
        _init()
    if _rejected_total is not None:
        _rejected_total.add(
            1,
            attributes={
                "host": host,
                "connector_type": connector_type or DEFAULT_CONNECTOR_TYPE,
                "reason": reason,
            },
        )
