"""Org-independent durable audit records (FAR-1517).

``audit_events.organisation_id`` FKs ``organisations.id`` with ``ON DELETE
CASCADE``, so a hard-deleted organisation takes its ENTIRE audit trail with it
— including the ``org_deletion_requested`` row written moments earlier. A
post-commit append cannot help either: it could never satisfy the FK on an org
that no longer exists.

``system_audit_events`` is the durable counterpart for org-lifecycle events.
It is deliberately NOT org-scoped:

* **no ``organisation_id`` column** — the column name is the load-bearing key
  for the tenancy regime (the ``rls_org_isolation`` policies and the ORM
  tenant filter both match on exactly that name). The org id is carried as
  ``org_id``: a plain value with no FK, so nothing cascades into this table
  and nothing scopes it out after the org is gone;
* **no FK to ``organisations``** — a hard delete therefore cannot reach it;
* **append-only** — UPDATE/DELETE are rejected by database triggers
  (migration 0285), the same structural guard ``audit_events`` carries.

Because the table has no ``organisation_id`` column it sits outside the
RLS regime by design: a lifecycle record must stay readable for the operator
investigating a deletion, long after the org and its RLS context are gone.

Deliberately not hash-chained: the tamper-evident chain in ``audit_events``
is per-organisation (head rows cascade away with the org), so a durable record
cannot chain to it. Immutability here comes from the append-only triggers.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class SystemAuditEvent(Base):
    __tablename__ = "system_audit_events"
    __table_args__ = (
        Index(
            "ix_system_audit_events_org_id_created_at",
            "org_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
        # Server-side safety net: a raw-SQL INSERT that omits the id still
        # lands a random v4 uuid instead of a NOT NULL violation.
        server_default=text("gen_random_uuid()"),
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    #: The organisation the event is ABOUT — a plain value, never an FK. Kept
    #: queryable so ops can ask "what happened to org X" after the org is gone.
    org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    #: The acting account, as a plain value (evidence must outlive its actor).
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
