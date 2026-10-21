"""Langgraph-free evidence readers shared by the node runner and cost finalize.

This module is deliberately a LEAF (precedent: ``modulo/core/node_output_split.py``):
it is imported by both ``modulo.core.pipeline_engine.node_runner`` (which pulls
LangGraph) and ``modulo.core.cost_controller.finalize`` (which is reachable from
``modulo.api`` and therefore must not transit LangGraph). A single copy of each
reader lives here so the two call sites cannot drift apart.

Two readers live here:

* :func:`parse_step_finish_evidence` — the opencode CLI's per-step token/cost
  evidence. The shipped CLI emits the payload NESTED::

      {"type": "step_finish", ..., "part": {"cost": 0.000478,
                                            "tokens": {...}}}

  so ``tokens`` and ``cost`` are read from ``part`` ONLY. A top-level-only
  fixture is intentionally NOT parsed: that shape was never emitted by the CLI,
  and accepting it would keep a wrong parser alive.
* :func:`heartbeat_liveness_seconds` — the liveness-PROVEN wall-clock window
  of a run, from its ``started_at`` / ``heartbeat_at`` / ``completed_at``
  stamps. Shared with ``modulo.core.cron_helpers`` (which re-exports it) so the
  age gate and the cancel path use one definition.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from modulo.core.cost_controller.breakdown.params import coerce_reported_token

__all__ = ["heartbeat_liveness_seconds", "parse_step_finish_evidence"]

#: The opencode JSONL event type that closes one model step and carries the
#: per-step ``tokens`` and ``cost`` inside its ``part`` object.
_STEP_FINISH_EVENT = "step_finish"

#: ``part.tokens`` keys that map onto the producer ``token_usage`` shape
#: unchanged, and its ``cache`` sub-object keys that map onto ``cache_*``.
_STEP_FINISH_TOKEN_PAIRS: tuple[tuple[str, str], ...] = (
    ("input", "input"),
    ("output", "output"),
    ("total", "total"),
)
_STEP_FINISH_CACHE_PAIRS: tuple[tuple[str, str], ...] = (("read", "cache_read"), ("write", "cache_write"))

#: Every producer ``token_usage`` key the reader can emit, in canonical order.
#: Only keys an event actually carried are returned.
_STEP_FINISH_TOKEN_KEYS: tuple[str, ...] = ("input", "output", "total", "cache_read", "cache_write")


def _coerce_step_cost(value: Any) -> Decimal | None:
    """A plausible non-negative finite per-step cost, or ``None``.

    A present-but-unusable value (bool, non-numeric, negative, NaN/Inf) is
    REJECTED, never coerced to zero — a bad report must stay distinguishable
    from a genuine zero.
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _accumulate_counters(
    totals: dict[str, int],
    seen: set[str],
    source: dict[str, Any],
    key_pairs: tuple[tuple[str, str], ...],
) -> None:
    """Add every plausible *source* counter into *totals*, recording *seen* keys."""
    for source_key, producer_key in key_pairs:
        coerced = coerce_reported_token(source.get(source_key))
        if coerced is not None:
            totals[producer_key] += coerced
            seen.add(producer_key)


def _event_part(line: str) -> dict[str, Any] | None:
    """The ``part`` object of one ``step_finish`` JSONL line, or ``None``.

    Cheap pre-filter first: only a line that even mentions the event type can
    be a candidate, so a large blob of ordinary text is not JSON-parsed.
    Non-JSON lines, non-dict payloads, other event types and events whose
    ``part`` is absent/non-dict all yield ``None``.
    """
    if _STEP_FINISH_EVENT not in line:
        return None
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict) or event.get("type") != _STEP_FINISH_EVENT:
        return None
    part = event.get("part")
    return part if isinstance(part, dict) else None


def parse_step_finish_evidence(raw: str | None) -> tuple[dict[str, int], Decimal | None]:
    """Recover summed ``(tokens, cost)`` from a retained opencode JSONL stream.

    Returns the PRODUCER ``token_usage`` shape
    ``{"input", "output", "total", "cache_read"?, "cache_write"?}`` and the
    summed per-step ``cost`` (``None`` when no usable cost was found). Every
    ``step_finish`` event in the stream contributes; only keys an event
    actually carried appear in the token map. An EMPTY token map AND a ``None``
    cost mean "no usable ``step_finish`` event", distinct from a genuine zero
    report.

    BOUNDED and fail-open: one linear pass, line by line; non-JSON lines, other
    event types and invalid counters/costs are skipped; ANY failure degrades to
    ``({}, None)`` — this never raises and never changes the caller's failure
    semantics.
    """
    if not isinstance(raw, str) or not raw:
        return {}, None
    totals: dict[str, int] = dict.fromkeys(_STEP_FINISH_TOKEN_KEYS, 0)
    seen: set[str] = set()
    cost_total = Decimal(0)
    found_cost = False
    found_event = False
    for line in raw.splitlines():
        part = _event_part(line)
        if part is None:
            continue
        found_event = True
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            _accumulate_counters(totals, seen, tokens, _STEP_FINISH_TOKEN_PAIRS)
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                _accumulate_counters(totals, seen, cache, _STEP_FINISH_CACHE_PAIRS)
        step_cost = _coerce_step_cost(part.get("cost"))
        if step_cost is not None:
            cost_total += step_cost
            found_cost = True
    token_usage = {key: totals[key] for key in _STEP_FINISH_TOKEN_KEYS if key in seen} if found_event else {}
    return token_usage, (cost_total if found_cost else None)


def heartbeat_liveness_seconds(
    started_at: datetime | None,
    heartbeat_at: datetime | None,
    completed_at: datetime | None,
) -> float | None:
    """The liveness-PROVEN wall-clock window of a run — or ``None``.

    A run's bound (an age gate, a cancel's best guess) is NOT evidence its
    sandbox was alive: crediting the full bound would OVERSTATE spend and could
    falsely trip the spend ceiling. The run's ``heartbeat_at`` is the last
    liveness stamp written while the run was still alive, so the proven window
    ends at ``min(heartbeat_at, completed_at)`` (the cap guards a re-stamp or
    clock skew that pushed the heartbeat past the terminal write).

    Returns ``None`` — meaning DO NOT attribute — unless the window is
    provable: ``started_at`` and ``heartbeat_at`` are both present, the
    heartbeat is strictly past the start, and the resulting window is positive
    and finite. A caller must never substitute a bound or ``now()`` for a
    missing proof.
    """
    if started_at is None or heartbeat_at is None or heartbeat_at <= started_at:
        return None
    end = heartbeat_at if completed_at is None else min(heartbeat_at, completed_at)
    seconds = (end - started_at).total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds
