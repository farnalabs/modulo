"""Release-channel configuration for pipeline snapshots (FAR-402 P6).

The snapshot machinery already pins the full graph + bindings immutably and
provides diff/rollback. Release channels build on top of that WITHOUT a full
promotion/rollback dashboard: this module owns the *contract* — the valid
channel set, the promotion/rollback thresholds as pure data, and the two
decision functions (``should_rollback`` and ``resolve_channel_binding``) that
are deterministic and unit-testable.

Out of scope (tracked as follow-up in the FAR-402 design): the metrics pipeline,
the promotion/rollback dashboard, and the actual channel-promotion reducer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Valid release channels a snapshot / trigger binding can carry. ``none`` is the
# default and means "no channel routing" — current behaviour, where a run pins
# the live graph at run-start. ``stable`` and ``canary`` are the supported
# routable channels.
VALID_RELEASE_CHANNELS: frozenset[str] = frozenset({"none", "stable", "canary"})

# Channels a trigger can actually bind to for channel-resolution (``none`` is
# excluded — a ``none`` binding means "resolve the live graph", which is handled
# by the caller, not the resolver).
ROUTABLE_RELEASE_CHANNELS: frozenset[str] = frozenset({"stable", "canary"})

DEFAULT_RELEASE_CHANNEL: str = "none"

# Column-length bound used by the PipelineSnapshot.channel field
# (``String(10)``). Exposed here so the contract is single-sourced.
RELEASE_CHANNEL_MAX_LEN: int = 10


@dataclass(frozen=True)
class ReleaseChannelThresholds:
    """Prompt/rollback thresholds as pure data (the contract).

    A promotion/rollback decision is a pure function of these thresholds and the
    observed channel metrics; no metrics pipeline is wired here. Defaults chosen
    as a conservative starting point for the eventual metrics consumer.
    """

    # Roll back the channel when its error rate reaches this percentage.
    rollback_threshold_error_rate_pct: float = 5.0
    # Minimum observed runs before an error-rate rollback is even considered
    # (insufficient signal below this — a single failed run on a 2-run channel is
    # not a rollback signal).
    rollback_min_observed_runs: int = 5
    # Minimum observed runs before a candidate channel may be promoted to
    # ``stable``.
    promotion_min_observed_runs: int = 10


DEFAULT_RELEASE_CHANNEL_THRESHOLDS: ReleaseChannelThresholds = ReleaseChannelThresholds()


@dataclass(frozen=True)
class ChannelMetrics:
    """Observed per-channel metrics a rollback decision consumes."""

    observed_runs: int = 0
    error_runs: int = 0

    @property
    def error_rate_pct(self) -> float:
        if self.observed_runs <= 0:
            return 0.0
        return (self.error_runs / self.observed_runs) * 100.0


def should_rollback(
    channel_metrics: ChannelMetrics,
    thresholds: ReleaseChannelThresholds = DEFAULT_RELEASE_CHANNEL_THRESHOLDS,
) -> bool:
    """Deterministic rollback oracle.

    Roll back a channel only when there is enough signal (``observed_runs >=
    rollback_min_observed_runs``) AND the observed error rate meets/exceeds the
    configured threshold. An empty channel (``observed_runs == 0``) never
    rolls back — there is no evidence either way.
    """
    if channel_metrics.observed_runs < thresholds.rollback_min_observed_runs:
        return False
    return channel_metrics.error_rate_pct >= thresholds.rollback_threshold_error_rate_pct


def resolve_channel_binding(
    trigger_config: dict[str, Any] | None,
    default: str = DEFAULT_RELEASE_CHANNEL,
) -> str:
    """Resolve a trigger's channel binding from its ``config_json``.

    Reads ``release_channel``; accepts only the valid channel set. Missing,
    invalid, or non-``stable``/``canary`` values fall back to ``default``
    (``none``) so an unbound trigger keeps current behaviour.
    """
    if not trigger_config:
        return default
    raw = trigger_config.get("release_channel")
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in VALID_RELEASE_CHANNELS:
        return value
    return default


def is_routable_channel(channel: str) -> bool:
    return channel in ROUTABLE_RELEASE_CHANNELS
