"""Rollback threshold evaluator for REST connector outcome rates (FAR-413).

Mirrors the ``modulo.core.rollback_thresholds`` design contract: this module is
DETECTION + NOTIFICATION only. It never flips a feature flag or reverts a
version by itself — it computes whether the measured error/UNKNOWN rates over
a sliding window cross the configured threshold, emits a structured WARNING so
the on-call operator can decide, and returns a :class:`RestRollbackSignal`
describing the trigger.

The evaluator is deliberately pure: it takes aggregated counts (or per-outcome
rollups) for a window, so a caller can feed it data sampled from a metrics
store, the runs table, or an in-process rate-window without the module needing
a DB session. The existing rollback_thresholds.py samples the runs table and
calls a table-agnostic count helper; this module is the REST-facing analogue
that consumes pre-aggregated counts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

_LOG_TRIGGERED = "rest_rollback.threshold_triggered"

# A window is "complete enough to judge" only once it has a minimum sample
# volume; a remote that handles 2 requests and errors on 1 must not flip a
# rollout off. Mirrors the volume gate in rollback_thresholds (``min_runs``).
_DEFAULT_MIN_SAMPLES = 50

# Default thresholds: >5% error rate or >2% unknown rate over a 15-minute
# window triggers a signal. Conservative, single-digit percentages so genuine
# remote regressions surface while occasional 429/5xx noise does not.
_DEFAULT_ERROR_RATE = 0.05
_DEFAULT_UNKNOWN_RATE = 0.02
_DEFAULT_UNKNOWN_LIKE = frozenset({"unknown"})


@dataclass(frozen=True)
class RestRollbackSignal:
    """A decision record surfaced to the operator when a threshold is crossed."""

    url: str
    window_seconds: float
    error_rate: float
    unknown_rate: float
    total_requests: int
    error_requests: int
    unknown_requests: int
    threshold_error_rate: float
    threshold_unknown_rate: float
    action: str = "operator_review"
    created_at: float = field(default_factory=time.monotonic)


def _epsilon(value: float) -> bool:
    """True when *value* is within floating-point epsilon of a threshold."""
    return abs(value) <= 1e-9


def evaluate_rest_rollback(
    *,
    url: str,
    total_requests: int,
    error_requests: int,
    unknown_requests: int,
    window_seconds: float = 15 * 60,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
    error_rate: float = _DEFAULT_ERROR_RATE,
    unknown_rate: float = _DEFAULT_UNKNOWN_RATE,
) -> RestRollbackSignal | None:
    """Return a :class:`RestRollbackSignal` when error/unknown rates are crossed.

    Volume-gated: returns ``None`` (no trigger) when ``total_requests <
    min_samples`` — a noisy-but-small sample must not flip a rollout. Error
    rate is ``error_requests / total_requests``; UNKNOWN rate is
    ``unknown_requests / total_requests``. A request counted as UNKNOWN is
    *not* also counted in the error bucket (the two are disjoint windows into
    the same total), so the rates can be judged independently.

    On trigger it emits ``_LOG_TRIGGERED`` (``rest_rollback.threshold_triggered``)
    with the measured + threshold values.

    Note the module never auto-flips: the returned signal is evidence for the
    operator to review (disable via feature flag, or revert the connector's
    version) — exactly like ``rollback_thresholds``.
    """
    if total_requests <= 0 or total_requests < min_samples:
        return None

    err_rate = error_requests / total_requests
    unk_rate = unknown_requests / total_requests

    error_crossed = err_rate > error_rate and not _epsilon(err_rate - error_rate)
    unknown_crossed = unk_rate > unknown_rate and not _epsilon(unk_rate - unknown_rate)

    if not (error_crossed or unknown_crossed):
        return None

    signal = RestRollbackSignal(
        url=url,
        window_seconds=window_seconds,
        error_rate=err_rate,
        unknown_rate=unk_rate,
        total_requests=total_requests,
        error_requests=error_requests,
        unknown_requests=unknown_requests,
        threshold_error_rate=error_rate,
        threshold_unknown_rate=unknown_rate,
    )

    _log.warning(
        _LOG_TRIGGERED,
        extra={
            "url": url,
            "window_seconds": window_seconds,
            "error_rate": f"{err_rate:.4f}",
            "unknown_rate": f"{unk_rate:.4f}",
            "total_requests": total_requests,
            "threshold_error_rate": error_rate,
            "threshold_unknown_rate": unknown_rate,
            "action": signal.action,
        },
    )
    return signal


def is_unknown_like(cause_code: str, unknown_like: frozenset[str] = _DEFAULT_UNKNOWN_LIKE) -> bool:
    """Classify a cause code as part of the UNKNOWN-rate denominator bucket.

    The UNKNOWN rate tracks outcomes whose terminal state we could not
    determine (a write where the remote's final state is genuinely
    indeterminate). ``unknown`` is the canonical member. A transport timeout is
    NOT counted here — it is a deterministic failure, classified as an error by
    the connector (see ``rest_metrics._ERROR_CAUSE_CODES``), so the error and
    UNKNOWN buckets stay disjoint and a timeout is never double-counted. A
    caller plumbing a custom taxonomy passes its own set. This is the
    connector-side classifier a metrics sampler uses to choose the bucket.
    """
    return cause_code in unknown_like
