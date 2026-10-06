"""Durable, org-independent audit records for org-lifecycle events (FAR-1517).

``modulo.core.audit_logger`` appends to ``audit_events``, which is org-scoped
by an ``ON DELETE CASCADE`` FK — a hard-deleted organisation takes that whole
chain with it, and a post-commit append can never satisfy the FK afterwards.
This module writes the same evidence to ``system_audit_events``, which carries
the org id as a PLAIN value (no FK, no ``organisation_id`` tenant column), so
the record survives the delete.

Contract for callers
--------------------
* **Call it INSIDE the deleting transaction, BEFORE the org row is removed.**
  The record then commits atomically with the delete (or rolls back with it),
  so there is no post-commit FK problem and no window where the audit says
  "deleted" but the delete has not happened.
* **Fail closed: there is deliberately NO try/except here.** An append failure
  raises out of the caller's ``async with session.begin()`` block, which rolls
  the whole transaction back — a destructive act that cannot be recorded must
  not happen. Route-level handlers already map the raised ``SQLAlchemyError``
  family to a 5xx.
* The insert is a Core statement rather than ``session.add`` so the write
  needs no flush ordering against the delete that follows it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.system_audit_event import SystemAuditEvent

__all__ = ["append_system_audit_event"]


async def append_system_audit_event(
    session: AsyncSession,
    *,
    event_type: str,
    org_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    payload_json: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Append one durable, org-independent audit record in the caller's transaction.

    ``org_id`` and ``actor_user_id`` are stored as plain values (columns AND,
    for the org, as ``organisation_id`` in the payload) — never as foreign
    keys, so no cascade can reach them.

    Raises whatever the driver raises on failure: callers get the fail-closed
    behaviour described in the module docstring by NOT catching it.
    """
    if not event_type.strip():
        raise ValueError("append_system_audit_event requires a non-empty event_type")

    payload = dict(payload_json or {})
    # The plain-value org id is the point of this table — always record it,
    # even when a caller passes only the column.
    if org_id is not None:
        payload.setdefault("organisation_id", str(org_id))
    if actor_user_id is not None:
        payload.setdefault("actor_user_id", str(actor_user_id))

    await session.execute(
        insert(SystemAuditEvent).values(
            id=uuid.uuid4(),
            event_type=event_type,
            org_id=org_id,
            actor_user_id=actor_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            payload_json=payload,
            request_id=request_id,
        )
    )
