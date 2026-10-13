"""Offline re-derivation of the embedded audit chain (FAR-1697).

This is the same linkage check ``audit_logger.verify_chain`` performs against
the live DB, but run over plain event dicts decoded from the pack's
``audit-events.ndjson`` file — no session, no RLS, no chain-head row. It reuses
the ONE hash implementation (``modulo.core.audit_logger.compute_event_hash``),
so it can never agree with a tampered claim by re-implementing the digest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from modulo.core.audit_logger.hashing import compute_event_hash

__all__ = ["recompute_chain"]


def _event_hash(event: Mapping[str, Any], previous_hash: str | None, organisation_id: str) -> str:
    """Re-derive one event's chain hash from its exported fields."""
    payload = event.get("payload_json")
    return compute_event_hash(
        str(event.get("event_type", "")),
        event.get("actor_user_id"),
        event.get("resource_type"),
        event.get("resource_id"),
        payload if isinstance(payload, dict) else {},
        event.get("request_id"),
        previous_hash,
        event.get("id"),
        organisation_id,
        str(event.get("created_at") or ""),
    )


def recompute_chain(
    events: Sequence[Mapping[str, Any]],
    organisation_id: str,
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    """Walk *events* and re-derive the hash chain.

    Returns the chain-evidence dict embedded in (and signed with) the manifest:

    * ``valid`` — the links are intact
    * ``event_count`` — number of events walked
    * ``head_hash`` — hash of the final event (``None`` for an empty chain)
    * ``first_gap_index`` / ``first_tampered_id`` — the first broken link
    * ``truncated`` — whether the exporter capped the event set

    An empty sequence is a valid empty chain (``valid=True``, ``head_hash=None``)
    — mirroring ``audit_logger.verify_chain``'s degenerate handling.
    """
    expected_prev: str | None = None
    for index, event in enumerate(events):
        if event.get("previous_hash") != expected_prev:
            return {
                "organisation_id": organisation_id,
                "event_count": len(events),
                "head_hash": None,
                "valid": False,
                "first_gap_index": index,
                "first_tampered_id": _as_optional_str(event.get("id")),
                "truncated": truncated,
            }
        expected_prev = _event_hash(event, expected_prev, organisation_id)
    return {
        "organisation_id": organisation_id,
        "event_count": len(events),
        "head_hash": expected_prev,
        "valid": True,
        "first_gap_index": None,
        "first_tampered_id": None,
        "truncated": truncated,
    }


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
