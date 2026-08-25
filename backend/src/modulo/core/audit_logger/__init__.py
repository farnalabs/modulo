"""Cryptographic audit chaining — SHA-256 linked events per organisation.

Each AuditEvent records the SHA-256 hash of the canonical JSON of the
prior event in the same org, forming a tamper-evident chain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import TenantPrincipal
from modulo.core.sanitize_log import sanitise_log_value
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

APPEND_MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 0.1
VERIFY_MAX_EVENTS = 10000
EXPORT_DEFAULT_PAGE_SIZE = 100
EXPORT_MAX_PAGE_SIZE = 1000
LIST_MIN_LIMIT = 1
LIST_MAX_LIMIT = 1000
BATCH_MAX_SIZE = 100

__all__ = [
    "append_audit_event",
    "append_audit_event_isolated",
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
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _audit_event_to_dict(e: AuditEvent) -> dict[str, Any]:
    return {
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


def _apply_filters(
    query: Any,
    org_id: uuid.UUID,
    *,
    event_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> Any:
    query = query.where(AuditEvent.organisation_id == org_id)
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
    return query


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

    Retries up to APPEND_MAX_RETRIES times if a concurrent transaction creates
    the chain head between our lock check and our insert (race on the first
    event for an org), with exponential backoff between attempts.
    """
    resolved_payload = payload_json or {}
    for attempt in range(APPEND_MAX_RETRIES):
        try:
            async with session.begin_nested():
                head = await _get_chain_head_locked(session, org_id)
                prev_hash = head.last_event_hash if head else None

                event = AuditEvent(
                    organisation_id=org_id,
                    event_type=event_type,
                    account_id=actor_user_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    payload_json=resolved_payload,
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
                    payload_json=resolved_payload,
                    request_id=request_id,
                    previous_hash=prev_hash,
                    event_id=event.id,
                    organisation_id=org_id,
                    created_at=event.created_at.isoformat(),
                )

                if head:
                    head.last_event_hash = event_hash
                    head.last_event_id = event.id
                    head.event_count = (head.event_count or 0) + 1
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
        except IntegrityError:
            _log.warning(
                "append_audit_event: IntegrityError on attempt %d/%d for org=%s event_type=%s",
                attempt + 1,
                APPEND_MAX_RETRIES,
                org_id,
                event_type,
                exc_info=True,
            )
            if attempt == APPEND_MAX_RETRIES - 1:
                _log.error(
                    "append_audit_event: exhausted %d retries for org=%s event_type=%s",
                    APPEND_MAX_RETRIES,
                    org_id,
                    event_type,
                )
                raise
            await asyncio.sleep(RETRY_BASE_DELAY_S * (attempt + 1))
        except ProgrammingError:
            _log.error(
                "append_audit_event: ProgrammingError (missing table) for org=%s event_type=%s",
                org_id,
                event_type,
            )
            raise
        except SQLAlchemyError:
            _log.exception(
                "append_audit_event: SQLAlchemyError for org=%s event_type=%s",
                org_id,
                event_type,
            )
            raise
    raise RuntimeError("append_audit_event: unexpected fallthrough")


async def append_audit_event_isolated(
    session: AsyncSession,
    principal: TenantPrincipal,
    *,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    event_type: str,
    payload: dict[str, Any],
    log_key: str,
) -> None:
    """Append an audit event in a fresh post-commit transaction, failure-isolated.

    Routes use this to emit PRD §8.12 audit events AFTER the primary write
    transaction has already committed. RLS context (SET LOCAL) reverts on COMMIT,
    so it must be re-established in this fresh transaction or the STRICT-RLS
    audit INSERT is rejected. A broken append is logged under ``log_key`` and
    never fails the completed operation (CancelledError always propagates).

    Shared by the api_keys, feedback and model_backends route modules, which
    previously each re-implemented this block.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await append_audit_event(
                session,
                org_id=principal.organisation_id,
                event_type=event_type,
                actor_user_id=principal.account_id,
                resource_type=resource_type,
                resource_id=resource_id,
                payload_json=payload,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            log_key,
            extra={
                "org_id": str(principal.organisation_id),
                "resource_id": str(resource_id),
                "event_type": event_type,
            },
        )


async def _get_chain_head_locked(session: AsyncSession, org_id: uuid.UUID) -> AuditChainHead | None:
    """Return the current chain head for an org, with a row-level lock.

    Prevents concurrent appends from reading the same head and creating a fork.
    """
    result = await session.execute(
        select(AuditChainHead).where(AuditChainHead.organisation_id == org_id).with_for_update()
    )
    return result.scalar_one_or_none()


def _make_verify_result(
    *,
    total_events: int,
    checked_events: int,
    truncated: bool,
    valid: bool,
    first_gap_index: int | None = None,
    first_tampered_id: str | None = None,
    chain_head_match: bool | None = None,
    chain_count_mismatch: bool | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "valid": valid,
        "total_events": total_events,
        "checked_events": checked_events,
        "truncated": truncated,
        "first_gap_index": first_gap_index,
        "first_tampered_id": first_tampered_id,
        "chain_head_match": chain_head_match,
        "chain_count_mismatch": chain_count_mismatch,
        "detail": detail,
    }


async def verify_chain(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    max_events: int = VERIFY_MAX_EVENTS,
) -> dict[str, Any]:
    """Recompute the entire audit chain and report gaps or tampering.

    Returns a dict with:
      - valid: bool — True if the chain is intact within the checked range
      - total_events: int — total event count in the DB for this org
      - checked_events: int — number of events actually verified
      - truncated: bool — True if total_events > max_events (partial check)
      - first_gap_index: int | None
      - first_tampered_id: str | None
      - chain_head_match: bool | None
      - chain_count_mismatch: bool | None
      - detail: str | None — human-readable tamper evidence when a break is
        found (expected vs actual previous_hash at the first gap), None otherwise
    """
    count_result = await session.execute(select(func.count(AuditEvent.id)).where(AuditEvent.organisation_id == org_id))
    total_events = count_result.scalar() or 0

    result = await session.execute(
        select(AuditEvent)
        .where(AuditEvent.organisation_id == org_id)
        .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        .limit(max_events)
    )
    events = list(result.scalars())

    if not events:
        # Degenerate case: count reports events but none were fetched (e.g. a
        # zero/negative max_events). Must not be reported as a valid chain.
        return _make_verify_result(
            total_events=total_events,
            checked_events=0,
            truncated=total_events > 0,
            valid=total_events == 0,
        )

    truncated = len(events) < total_events

    gap_index, tampered_id, expected_hash, stored_hash = _recompute_chain(events)
    detail = None
    if gap_index is not None and tampered_id is not None:
        detail = _describe_chain_break(gap_index, tampered_id, expected_hash, stored_hash)
    if gap_index is not None:
        return _make_verify_result(
            total_events=total_events,
            checked_events=gap_index + 1,
            truncated=truncated,
            valid=False,
            first_gap_index=gap_index,
            first_tampered_id=tampered_id,
            detail=detail,
        )

    expected_prev = _compute_event_hash(
        event_type=events[-1].event_type,
        actor_user_id=events[-1].account_id,
        resource_type=events[-1].resource_type,
        resource_id=events[-1].resource_id,
        payload_json=events[-1].payload_json,
        request_id=events[-1].request_id,
        previous_hash=events[-1].previous_hash,
        event_id=events[-1].id,
        organisation_id=events[-1].organisation_id,
        created_at=events[-1].created_at.isoformat() if events[-1].created_at else "",
    )

    head = await _get_chain_head_locked(session, org_id)

    if head:
        chain_head_match = head.last_event_hash == expected_prev
        count_mismatch = head.event_count is not None and head.event_count != total_events
    else:
        chain_head_match = None
        count_mismatch = None

    no_head_corruption = head is not None or total_events == 0
    valid = not truncated and (chain_head_match is not False) and no_head_corruption and not count_mismatch

    return _make_verify_result(
        total_events=total_events,
        checked_events=len(events),
        truncated=truncated,
        valid=valid,
        chain_head_match=chain_head_match,
        chain_count_mismatch=count_mismatch,
    )


def _recompute_chain(events: list[AuditEvent]) -> tuple[int | None, str | None, str | None, str | None]:
    """Walk the event list and verify hash chain integrity.

    Returns (first_gap_index, first_tampered_id, expected_hash, stored_hash)
    or (None, None, None, None) if intact. ``expected_hash`` is the hash the
    broken event *should* have pointed at (the recomputed hash of the prior
    event), and ``stored_hash`` is the value actually recorded — together they
    provide actionable tamper evidence.
    """
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
            return (idx, str(event.id), expected_prev, event.previous_hash)
        expected_prev = canonical_hash
    return (None, None, None, None)


def _describe_chain_break(
    gap_index: int,
    tampered_id: str,
    expected_hash: str | None,
    stored_hash: str | None,
) -> str:
    """Build a human-readable tamper-evidence message for a chain break."""
    if expected_hash is None:
        return (
            f"Audit chain break at event {gap_index} (id {tampered_id}): stored previous_hash "
            f"({stored_hash}) does not match the recomputed hash of the prior event (None). "
            "This is the first event in the org's chain, so a prior-event hash is not expected; "
            "the stored previous_hash indicates tampering."
        )
    return (
        f"Audit chain break at event {gap_index} (id {tampered_id}): stored previous_hash "
        f"({stored_hash}) does not match the recomputed hash of the prior event ({expected_hash}). "
        "The event or one before it has been tampered with."
    )


async def export_chain(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = EXPORT_DEFAULT_PAGE_SIZE,
    event_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> dict[str, Any]:
    """Export audit events as paginated JSON lines with optional filters."""
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, EXPORT_MAX_PAGE_SIZE))

    query = _apply_filters(
        select(AuditEvent),
        org_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        from_date=from_date,
        to_date=to_date,
    )

    offset = (safe_page - 1) * safe_page_size
    query = query.order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc()).offset(offset).limit(safe_page_size)
    result = await session.execute(query)
    events = list(result.scalars())

    count_query = _apply_filters(
        select(func.count(AuditEvent.id)),
        org_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        from_date=from_date,
        to_date=to_date,
    )
    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    items = [_audit_event_to_dict(e) for e in events]

    return {
        "items": items,
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
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
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> dict[str, Any]:
    """List audit events with cursor-based pagination and filtering.

    Cursor is a JSON object ``{"c":"<created_at_iso>","i":"<event_id>"}``
    that encodes both sort columns so pagination is correct across
    ``created_at DESC, id DESC``.

    Returns dict with items, next_cursor, total. Previous-page cursor
    is not provided — this is a forward-only cursor pattern. Callers
    should reset cursor to None to go back to the first page.
    """
    resolved_limit = max(LIST_MIN_LIMIT, min(limit, LIST_MAX_LIMIT))

    query = _apply_filters(
        select(AuditEvent),
        org_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        from_date=from_date,
        to_date=to_date,
    )

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
                "list_audit_events: failed to decode cursor %r — falling back to first page",
                sanitise_log_value(cursor),
                exc_info=True,
            )

    query = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(resolved_limit + 1)

    result = await session.execute(query)
    events = list(result.scalars())

    has_more = len(events) > resolved_limit
    if has_more:
        events = events[:resolved_limit]

    items = [_audit_event_to_dict(e) for e in events]

    last_event = events[-1] if events else None
    next_cursor = None
    if last_event and has_more:
        if last_event.created_at is not None:
            next_cursor = json.dumps(
                {"c": last_event.created_at.isoformat(), "i": str(last_event.id)},
                separators=(",", ":"),
            )
        else:
            _log.warning(
                "list_audit_events: last event %s has null created_at — cannot produce next cursor",
                last_event.id,
            )

    return {
        "items": items,
        "total": total,
        "next_cursor": next_cursor,
        "limit": resolved_limit,
    }


async def get_audit_events_batch(
    session: AsyncSession,
    org_id: uuid.UUID,
    event_ids: list[str],
) -> list[dict[str, Any]]:
    """Return full details for a batch of event IDs (max BATCH_MAX_SIZE)."""
    if len(event_ids) > BATCH_MAX_SIZE:
        _log.warning("get_audit_events_batch: truncating %d IDs to %d", len(event_ids), BATCH_MAX_SIZE)
    capped = event_ids[:BATCH_MAX_SIZE]
    ids = []
    for eid in capped:
        try:
            ids.append(uuid.UUID(eid))
        except ValueError:
            _log.warning("get_audit_events_batch: received invalid UUID %r — skipping", eid)

    if not ids:
        return []

    result = await session.execute(
        select(AuditEvent).where(AuditEvent.organisation_id == org_id).where(AuditEvent.id.in_(ids))
    )
    events = list(result.scalars())

    return [_audit_event_to_dict(e) for e in events]
