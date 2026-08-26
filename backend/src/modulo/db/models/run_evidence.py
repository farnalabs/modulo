"""run_evidence — the tri-state evidence side table (FAR-152, §15.3/§15.12).

One row per ``(run_id, node_id)`` written by the post-commit async evidence
probe (and backfilled by the reconciliation sweep). ``evidence_state`` is one
of the ``EvidenceResult`` values: ``has_work`` | ``verified_empty`` |
``unverifiable``. ``unverifiable`` never fires a flag — downstream renders a
muted "work could not be verified" notice instead.

The table is tenant-isolated via ``organisation_id`` (mirrors the
``rls_org_isolation`` policy added by migration ``0133_run_evidence_rls``). The
composite ``(run_id, node_id)`` primary key remains the natural row anchor;
``organisation_id`` is added here so the ORM stays in sync with the migrated
schema and a future ``alembic revision --autogenerate`` will not propose
dropping the RLS-confined column.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class RunEvidence(Base):
    __tablename__ = "run_evidence"
    __table_args__ = (PrimaryKeyConstraint("run_id", "node_id", name="pk_run_evidence_run_node"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_state: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    evidence_written_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
