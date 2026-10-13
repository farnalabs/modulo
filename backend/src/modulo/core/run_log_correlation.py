"""FAR-1690 A4 — run-keyed, provenance-typed log correlation projection.

A READ-ONLY projection over data Modulo **already persists**. It adds NO new
store and NO migration: it correlates three existing, ``run_id``-keyed sources
and stamps each entry with its governed-run provenance class (FAR-1693 /
ADR-042 — the single vocabulary in :mod:`modulo.core.provenance`):

* **structured events** — ``audit_events`` rows for the run. Modulo wrote these
  natively, so they are :data:`PROVENANCE_ATTESTED`.
* **workspace log tail** — the retained tail captured through the runtime
  provider's ``read_log_tail`` primitive (persisted on the node's telemetry or
  on a retention marker). Modulo retrieved it through a credential it holds, so
  it is :data:`PROVENANCE_OBSERVED` — never the deadline-kill-only projection
  the FAR-704 ``deadline_triage`` surface serves.
* **dispatch-origin entries** — a dispatched node's folded-back telemetry,
  which the engine stamps with the :data:`PROVENANCE_REPORTED` class and a
  ``witnessed_via`` channel stamp. The substrate's report stays ``reported``
  (ADR-042 forecloses ``attested`` — attested never implies the substrate's
  data is true).

This module is a core leaf: it imports only :mod:`modulo.core.provenance` and
the stdlib, so both ``api`` and ``core`` may compose it (``core`` may not import
``api``; the reverse is allowed). The projection is pure — callers fetch the
raw rows and hand them here — so it is unit-testable without a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from modulo.core.provenance import (
    PROVENANCE_ATTESTED,
    PROVENANCE_KEY,
    PROVENANCE_OBSERVED,
    PROVENANCE_REPORTED,
    WITNESSED_VIA_KEY,
)

#: Display-side bound for a tail excerpt — parity with the FAR-704 triage tail
#: (``runs._TRIAGE_LOG_TAIL_CHARS``). The retained marker stays authoritative;
#: the projection serves only a bounded display slice.
LOG_TAIL_EXCERPT_CHARS: Final[int] = 6000

#: Display-side bound for a structured-event / dispatch excerpt.
STRUCTURED_EXCERPT_CHARS: Final[int] = 1000

SOURCE_WORKSPACE_LOG_TAIL: Final[str] = "workspace_log_tail"
SOURCE_STRUCTURED_EVENT: Final[str] = "structured_event"
SOURCE_NODE_TELEMETRY: Final[str] = "node_telemetry"

#: TZ-aware floor for ordering entries whose source carries no timestamp (a
#: retained marker has no recorded-at column). Timestamped entries sort first;
#: an un-timestamped entry sorts after every real instant but never errors on a
#: naive/aware comparison.
_ORDERING_FLOOR: Final[datetime] = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RunLogEntry:
    """One provenance-typed log-correlation entry.

    ``run_id`` is not stored on the entry — the response root carries it and
    every entry belongs to that one run (the whole point of a run-keyed read).
    """

    source: str
    provenance: str
    excerpt: str
    node_id: str | None = None
    attempt_key: str | None = None
    witnessed_via: str | None = None
    provider: str | None = None
    provider_ref: str | None = None
    recorded_at: datetime | None = None

    def dedup_key(self) -> tuple[str, str | None, str]:
        """Identity for suppression of a tail captured on two sources."""
        return (self.source, self.node_id, self.excerpt)


def _bounded(text: Any, limit: int) -> str:
    """Return the trailing *limit* characters of *text* as a string."""
    value = "" if text is None else str(text)
    return value[-limit:] if value else ""


def _telemetry_refs(telemetry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The (provider, provider_ref) attribution carried by node telemetry."""
    provider = telemetry.get("provider")
    provider_ref = telemetry.get("sandbox_id")
    return (provider if isinstance(provider, str) else None, provider_ref if isinstance(provider_ref, str) else None)


def marker_tail_entries(markers: Mapping[str, Any] | None) -> list[RunLogEntry]:
    """Project retained workspace log tails out of the raw-output markers.

    A marker (the FAR-704 retention shape) carries ``sandbox_log_tail`` — the
    tail ``read_log_tail`` captured before a cancel/deadline-kill. Surfacing it
    here (not only on a ``deadline_triage`` marker) is exactly the gap A4
    closes. Markers without a tail carry no log content and are skipped.
    """
    if not isinstance(markers, Mapping):
        return []
    entries: list[RunLogEntry] = []
    for attempt_key, marker in markers.items():
        if not isinstance(marker, Mapping):
            continue
        excerpt = _bounded(marker.get("sandbox_log_tail"), LOG_TAIL_EXCERPT_CHARS)
        if not excerpt:
            continue
        entries.append(
            RunLogEntry(
                source=SOURCE_WORKSPACE_LOG_TAIL,
                provenance=PROVENANCE_OBSERVED,
                excerpt=excerpt,
                node_id=marker.get("node_id") if isinstance(marker.get("node_id"), str) else None,
                attempt_key=(marker.get("attempt_key") if isinstance(marker.get("attempt_key"), str) else attempt_key),
            )
        )
    return entries


def telemetry_entries(telemetry: Mapping[str, Any] | None) -> list[RunLogEntry]:
    """Project node telemetry into observed tails and reported dispatch entries.

    A dispatched node's telemetry carries the engine-assigned ``reported`` class
    and a ``witnessed_via`` channel stamp — surfaced as ``reported``. Any other
    node whose telemetry carries an ``sandbox_log_tail`` is a workspace tail
    Modulo captured through ``read_log_tail`` — ``observed``. Telemetry with
    neither carries no log content.
    """
    if not isinstance(telemetry, Mapping):
        return []
    entries: list[RunLogEntry] = []
    for node_id, node in telemetry.items():
        if not isinstance(node, Mapping):
            continue
        provider, provider_ref = _telemetry_refs(node)
        witnessed = node.get(WITNESSED_VIA_KEY)
        provenance = node.get(PROVENANCE_KEY)
        if provenance == PROVENANCE_REPORTED or (isinstance(witnessed, str) and witnessed):
            excerpt = _bounded(
                node.get("summary") or node.get("status") or node.get("substrate_status"),
                STRUCTURED_EXCERPT_CHARS,
            )
            entries.append(
                RunLogEntry(
                    source=SOURCE_NODE_TELEMETRY,
                    provenance=PROVENANCE_REPORTED,
                    excerpt=excerpt,
                    node_id=node_id,
                    provider=provider,
                    provider_ref=provider_ref,
                    witnessed_via=witnessed if isinstance(witnessed, str) else None,
                )
            )
            continue
        excerpt = _bounded(node.get("sandbox_log_tail"), LOG_TAIL_EXCERPT_CHARS)
        if excerpt:
            entries.append(
                RunLogEntry(
                    source=SOURCE_WORKSPACE_LOG_TAIL,
                    provenance=PROVENANCE_OBSERVED,
                    excerpt=excerpt,
                    node_id=node_id,
                    provider=provider,
                    provider_ref=provider_ref,
                )
            )
    return entries


def structured_event_entries(events: Iterable[Mapping[str, Any]] | None) -> list[RunLogEntry]:
    """Project the run's Modulo-native structured events.

    Each prepared event dict carries ``event_type`` / ``detail`` /
    ``recorded_at`` — the caller sanitises ``detail`` before handing it in.
    Modulo wrote the row natively, so the class is ``attested``.
    """
    if not events:
        return []
    entries: list[RunLogEntry] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        excerpt = _bounded(event.get("detail") or event.get("event_type"), STRUCTURED_EXCERPT_CHARS)
        recorded_at = event.get("recorded_at")
        entries.append(
            RunLogEntry(
                source=SOURCE_STRUCTURED_EVENT,
                provenance=PROVENANCE_ATTESTED,
                excerpt=excerpt,
                node_id=event.get("node_id") if isinstance(event.get("node_id"), str) else None,
                recorded_at=recorded_at if isinstance(recorded_at, datetime) else None,
            )
        )
    return entries


def correlate_run_logs(
    *,
    events: Iterable[Mapping[str, Any]] | None = None,
    markers: Mapping[str, Any] | None = None,
    telemetry: Mapping[str, Any] | None = None,
) -> list[RunLogEntry]:
    """Join the three persisted sources into one time-ordered entry list.

    Duplicate tails (a node tail captured on BOTH a marker and its telemetry —
    the cancel/exception paths can overlap) collapse onto one entry, keeping
    the first sighting. Timestamped entries sort ascending; un-timestamped
    (retained tails) follow, preserving their source order.
    """
    combined = [
        *structured_event_entries(events),
        *marker_tail_entries(markers),
        *telemetry_entries(telemetry),
    ]
    seen: set[tuple[str, str | None, str]] = set()
    deduped: list[RunLogEntry] = []
    for entry in combined:
        key = entry.dedup_key()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    deduped.sort(key=lambda e: (e.recorded_at is None, e.recorded_at or _ORDERING_FLOOR))
    return deduped


__all__ = [
    "LOG_TAIL_EXCERPT_CHARS",
    "SOURCE_NODE_TELEMETRY",
    "SOURCE_STRUCTURED_EVENT",
    "SOURCE_WORKSPACE_LOG_TAIL",
    "STRUCTURED_EXCERPT_CHARS",
    "RunLogEntry",
    "correlate_run_logs",
    "marker_tail_entries",
    "structured_event_entries",
    "telemetry_entries",
]
