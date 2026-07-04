"""Cryptographic audit chaining — SHA-256 linked events per organisation.

Each AuditEvent records the SHA-256 hash of the canonical JSON of the
prior event in the same org, forming a tamper-evident chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.audit_event import AuditChainHead, AuditEvent

_log = logging.getLogger(__name__)

__all__ = [
    "append_audit_event",
    "export_chain",
    "get_audit_events_batch",
    "get_chain_head",
    "list_audit_events",
    "verify_chain",
]


def _uuid_or_none(val: object) -> str | None:
    """Convert a UUID (or str) to its string form, or return None."""
    if val is None:
        return None
    return str(val)


def _compute_event_hash(
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
    or strings interchangeably.
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
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def get_chain_head(session: AsyncSession, org_id: uuid.UUID) -> AuditChainHead | None:
    """Return the current chain head for an org."""
    result = await session.execute(select(AuditChainHead).where(AuditChainHead.organisation_id == org_id))
    return result.scalar_one_or_none()


async def append_audit_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    event_type: str,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    payload_json: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    """Append a new event to the audit chain, computing the previous hash.

    Uses SELECT ... FOR UPDATE on the chain head to prevent forks from
    concurrent appends within the same organisation.
    """
    head = await _get_chain_head_locked(session, org_id)
    prev_hash = head.last_event_hash if head else None

    event = AuditEvent(
        organisation_id=org_id,
        event_type=event_type,
        account_id=actor_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        payload_json=payload_json or {},
        request_id=request_id,
        previous_hash=prev_hash,
    )
    if event.created_at is None:
        event.created_at = datetime.now(UTC)
    session.add(event)
    await session.flush()

    event_hash = _compute_event_hash(
        event_type=event_type,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        payload_json=payload_json or {},
        request_id=request_id,
        previous_hash=prev_hash,
        event_id=event.id,
        organisation_id=org_id,
        created_at=event.created_at.isoformat(),
    )

    # Upsert chain head
    if head:
        head.last_event_hash = event_hash
        head.last_event_id = event.id
        head.event_count += 1
    else:
        head = AuditChainHead(
            organisation_id=org_id,
            last_event_hash=event_hash,
            last_event_id=event.id,
            event_count=1,
        )
        session.add(head)

    await session.flush()
    return event


async def _get_chain_head_locked(session: AsyncSession, org_id: uuid.UUID) -> AuditChainHead | None:
    """Return the current chain head for an org, with a row-level lock.

    Prevents concurrent appends from reading the same head and creating a fork.
    """
    result = await session.execute(
        select(AuditChainHead)
        .where(AuditChainHead.organisation_id == org_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def verify_chain(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    max_events: int = 10000,
) -> dict[str, Any]:
    """Recompute the entire audit chain and report gaps or tampering.

    Returns a dict with:
      - valid: bool — True if the chain is intact
      - total_events: int
      - checked_events: int
      - first_gap_index: int | None
      - first_tampered_id: str | None
      - chain_head_match: bool | None
    """
    result = await session.execute(
        select(AuditEvent)
        .where(AuditEvent.organisation_id == org_id)
        .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        .limit(max_events)
    )
    events = list(result.scalars())

    if not events:
        return {
            "valid": True,
            "total_events": 0,
            "checked_events": 0,
            "event_count": 0,
        }

    expected_prev: str | None = None
    for idx, event in enumerate(events):
        canonical_hash = _compute_event_hash(
            event_type=event.event_type,
            actor_user_id=event.account_id,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            payload_json=event.payload_json,
            request_id=event.request_id,
            previous_hash=expected_prev,
            event_id=event.id,
            organisation_id=event.organisation_id,
            created_at=event.created_at.isoformat() if event.created_at else "",
        )
        if event.previous_hash != expected_prev:
            detail = (
                f"Chain break at event {idx}: expected previous_hash {expected_prev!r}, got {event.previous_hash!r}"
            )
            return {
                "valid": False,
                "total_events": len(events),
                "checked_events": idx + 1,
                "first_gap_index": idx,
                "first_tampered_id": str(event.id),
                "detail": detail,
                "event_count": len(events),
                "error": detail,
            }
        expected_prev = canonical_hash

    # Validate against chain head
    head = await get_chain_head(session, org_id)
    chain_head_match = head.last_event_hash == expected_prev if head else None

    return {
        "valid": True,
        "total_events": len(events),
        "checked_events": len(events),
        "first_gap_index": None,
        "first_tampered_id": None,
        "chain_head_match": chain_head_match,
        "event_count": len(events),
    }


async def export_chain(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 100,
    event_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Export audit events as paginated JSON lines with optional filters."""
    query = select(AuditEvent).where(AuditEvent.organisation_id == org_id)

    if event_type:
        query = query.where(AuditEvent.event_type == event_type)
    if actor_user_id:
        query = query.where(AuditEvent.account_id == actor_user_id)
    if resource_type:
        query = query.where(AuditEvent.resource_type == resource_type)
    if from_date:
        query = query.where(AuditEvent.created_at >= from_date)
    if to_date:
        query = query.where(AuditEvent.created_at <= to_date)

    offset = (page - 1) * page_size
    query = query.order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc()).offset(offset).limit(page_size)
    result = await session.execute(query)
    events = list(result.scalars())

    count_query = select(func.count(AuditEvent.id)).where(AuditEvent.organisation_id == org_id)
    if event_type:
        count_query = count_query.where(AuditEvent.event_type == event_type)
    if actor_user_id:
        count_query = count_query.where(AuditEvent.account_id == actor_user_id)
    if resource_type:
        count_query = count_query.where(AuditEvent.resource_type == resource_type)
    if from_date:
        count_query = count_query.where(AuditEvent.created_at >= from_date)
    if to_date:
        count_query = count_query.where(AuditEvent.created_at <= to_date)
    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    items = []
    for e in events:
        items.append(
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "actor_user_id": str(e.account_id) if e.account_id else None,
                "resource_type": e.resource_type,
                "resource_id": str(e.resource_id) if e.resource_id else None,
                "payload_json": e.payload_json,
                "request_id": e.request_id,
                "previous_hash": e.previous_hash,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def list_audit_events(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = 50,
    event_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """List audit events with cursor-based pagination and filtering.

    Cursor is a JSON object ``{"c":"<created_at_iso>","i":"<event_id>"}``
    that encodes both sort columns so pagination is correct across
    ``created_at DESC, id DESC``.

    Returns dict with items, next_cursor, prev_cursor, total.
    """
    query = select(AuditEvent).where(AuditEvent.organisation_id == org_id)

    if event_type:
        query = query.where(AuditEvent.event_type == event_type)
    if actor_user_id:
        query = query.where(AuditEvent.account_id == actor_user_id)
    if resource_type:
        query = query.where(AuditEvent.resource_type == resource_type)
    if from_date:
        query = query.where(AuditEvent.created_at >= from_date)
    if to_date:
        query = query.where(AuditEvent.created_at <= to_date)

    # Total count (before pagination)
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    # Composite cursor: decode created_at + id from JSON
    if cursor:
        try:
            cursor_data = json.loads(cursor)
            cursor_ts = datetime.fromisoformat(cursor_data["c"])
            cursor_id = uuid.UUID(cursor_data["i"])
            query = query.where(
                (AuditEvent.created_at < cursor_ts)
                | ((AuditEvent.created_at == cursor_ts) & (AuditEvent.id < cursor_id))
            )
        except (ValueError, KeyError, TypeError):
            _log.warning(
                "list_audit_events: failed to decode cursor %r — falling back to unfiltered query",
                cursor,
            )

    query = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)

    result = await session.execute(query)
    events = list(result.scalars())

    has_more = len(events) > limit
    if has_more:
        events = events[:limit]

    items = []
    for e in events:
        items.append(
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "actor_user_id": str(e.account_id) if e.account_id else None,
                "resource_type": e.resource_type,
                "resource_id": str(e.resource_id) if e.resource_id else None,
                "payload_json": e.payload_json,
                "request_id": e.request_id,
                "previous_hash": e.previous_hash,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
        )

    last_event = events[-1] if events else None
    next_cursor = (
        json.dumps({"c": last_event.created_at.isoformat(), "i": str(last_event.id)}, separators=(",", ":"))
        if last_event and has_more
        else None
    )
    prev_cursor = (
        json.dumps({"c": events[0].created_at.isoformat(), "i": str(events[0].id)}, separators=(",", ":"))
        if events
        else None
    )

    return {
        "items": items,
        "total": total,
        "next_cursor": next_cursor,
        "prev_cursor": prev_cursor,
        "limit": limit,
    }


async def get_audit_events_batch(
    session: AsyncSession,
    org_id: uuid.UUID,
    event_ids: list[str],
) -> list[dict[str, Any]]:
    """Return full details for a batch of event IDs."""
    ids = []
    for eid in event_ids:
        try:
            ids.append(uuid.UUID(eid))
        except ValueError:
            _log.warning("get_audit_events_batch: received invalid UUID %r — skipping", eid)
            continue

    if not ids:
        return []

    result = await session.execute(
        select(AuditEvent).where(AuditEvent.organisation_id == org_id).where(AuditEvent.id.in_(ids))
    )
    events = list(result.scalars())

    items = []
    for e in events:
        items.append(
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "actor_user_id": str(e.account_id) if e.account_id else None,
                "resource_type": e.resource_type,
                "resource_id": str(e.resource_id) if e.resource_id else None,
                "payload_json": e.payload_json,
                "request_id": e.request_id,
                "previous_hash": e.previous_hash,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
        )

    return items
