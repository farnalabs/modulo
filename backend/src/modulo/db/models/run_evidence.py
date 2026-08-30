"""run_evidence — the tri-state evidence side table (FAR-152, §15.3/§15.12).

One row per ``(run_id, node_id)`` written by the post-commit async evidence
probe (and backfilled by the reconciliation sweep). ``evidence_state`` is one
of the ``EvidenceResult`` values: ``has_work`` | ``verified_empty`` |
``unverifiable``. ``unverifiable`` never fires a flag — downstream renders a
muted "work could not be verified" notice instead.

The table is org-scoped (migration 0133 added ``organisation_id`` NOT NULL,
FK -> organisations, plus ``ENABLE``/``FORCE ROW LEVEL SECURITY`` and the
``rls_org_isolation`` policy), sourced from the parent run's org. A surrogate
``id`` is omitted — the composite ``(run_id, node_id)`` PK IS the natural key,
matching the §15.12 schema diff exactly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, PrimaryKeyConstraint, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class RunEvidence(Base):
    __tablename__ = "run_evidence"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "node_id", name="pk_run_evidence_run_node"),
        CheckConstraint(
            "evidence_state IN ('has_work','verified_empty','unverifiable')",
            name="ck_run_evidence_state",
        ),
    )

    # Tenant anchor (matches migration 0133_run_evidence_rls which added this
    # NOT NULL FK + RLS). The ORM previously omitted it, so every INSERT failed
    # the NOT NULL / RLS FORCE guard on Postgres.
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_state: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    evidence_written_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
