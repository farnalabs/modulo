"""run_evidence — the tri-state evidence side table (FAR-152, §15.3/§15.12).

One row per ``(run_id, node_id)`` written by the post-commit async evidence
probe (and backfilled by the reconciliation sweep). ``evidence_state`` is one
of the ``EvidenceResult`` values: ``has_work`` | ``verified_empty`` |
``unverifiable``. ``unverifiable`` never fires a flag — downstream renders a
muted "work could not be verified" notice instead.

The table is deliberately NOT org-scoped: it carries no ``organisation_id``
column. Rows are written/read only by the harness probe machinery with an
explicit run_id; the UNIQUE(run_id, node_id) primary key is the tenant anchor.
A surrogate ``id`` is omitted — the composite PK IS the natural key, matching
the §15.12 schema diff exactly.
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

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_state: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    evidence_written_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
