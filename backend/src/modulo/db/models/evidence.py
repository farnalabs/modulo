"""Generic Evidence table — append-only fact store (FAR-966 chunk 7, §3.1).

Stores key/value evidence facts scoped to a subject (composite
``{subject_type, subject_id}``) within an organisation.  Rows are
append-only: no UPDATE or DELETE operations are exposed.

The ``value`` column is JSONB and carries boolean, number, or null
(indeterminate) values.  ``producer_type`` is constrained by a CHECK
to the five-value vocabulary (``eval``, ``policy_gate``, ``run``,
``system_state``, ``derived``).

RLS org-isolation is enforced by migration 0263_evidence_layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, TimestampMixin

# Authoritative producer-type vocabulary — must match the CHECK constraint.
PRODUCER_TYPES: frozenset[str] = frozenset({"eval", "policy_gate", "run", "system_state", "derived"})


class Evidence(Base, TimestampMixin):
    """Append-only evidence fact store (§3.1).

    The ORM exposes no ``update()`` or ``delete()`` methods; the table
    is append-only by convention and enforced at the application layer.
    """

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            "producer_type IN ('eval','policy_gate','run','system_state','derived')",
            name="ck_evidence_producer_type",
        ),
        UniqueConstraint(
            "organisation_id",
            "subject_type",
            "subject_id",
            "key",
            "created_at",
            "id",
            name="uq_evidence_distinct_on",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
    )

    key: Mapped[str] = mapped_column(Text(), nullable=False)
    subject_type: Mapped[str] = mapped_column(Text(), nullable=False)
    subject_id: Mapped[str] = mapped_column(Text(), nullable=False)
    value: Mapped[Any] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    producer_type: Mapped[str] = mapped_column(Text(), nullable=False)
    producer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
