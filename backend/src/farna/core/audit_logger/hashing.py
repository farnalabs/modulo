"""Pure audit-event hash computation (FAR-1697).

The audit chain hashes each event's canonical serialisation and links it to the
prior event via ``previous_hash``. The offline-verifiable evidence pack
(:mod:`modulo.core.evidence_pack`) must re-derive that same hash from an
exported event WITHOUT importing the DB / SQLAlchemy / RLS layer, so the
computation lives here — a dependency-free module — and
``modulo.core.audit_logger`` re-exports it.

This is a MOVE, not a format change: the serialisation is byte-identical to the
implementation previously inlined in ``audit_logger/__init__`` (the audit-chain
format is frozen). It is the single implementation of the hash; both the live
append path and the offline verifier call it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["compute_event_hash"]


def _uuid_or_none(val: object) -> str | None:
    """Convert a UUID (or str) to its string form, or return None."""
    if val is None:
        return None
    return str(val)


def compute_event_hash(
    event_type: str,
    actor_user_id: object,
    resource_type: str | None,
    resource_id: object,
    payload_json: dict[str, Any],
    request_id: str | None,
    previous_hash: str | None,
    event_id: object,
    organisation_id: object,
    created_at: str,
) -> str:
    """Compute the SHA-256 hash of canonical event JSON.

    UUID parameters (actor_user_id, resource_id, event_id, organisation_id)
    are converted to strings internally so callers may pass UUID objects
    or strings interchangeably. The signature is positional-compatible with the
    former ``audit_logger._compute_event_hash`` so every existing call site
    (and the unit tests that pin it) keeps working unchanged.
    """
    canonical = json.dumps(
        {
            "event_type": event_type,
            "actor_user_id": _uuid_or_none(actor_user_id),
            "resource_type": resource_type,
            "resource_id": _uuid_or_none(resource_id),
            "payload_json": payload_json,
            "request_id": request_id,
            "previous_hash": previous_hash,
            "event_id": _uuid_or_none(event_id),
            "organisation_id": _uuid_or_none(organisation_id),
            "created_at": created_at,
        },
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
