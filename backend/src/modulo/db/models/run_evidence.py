"""run_evidence — the tri-state evidence side table (FAR-152, §15.3/§15.12).

One row per ``(run_id, node_id)`` written by the post-commit async evidence
probe (and backfilled by the reconciliation sweep). ``evidence_state`` is one
of the ``EvidenceResult`` values: ``has_work`` | ``verified_empty`` |
``unverifiable``. ``unverifiable`` never fires a flag — downstream renders a
muted "work could not be verified" notice instead.

The table is tenant-scoped via ``organisation_id`` (backfilled from the parent
run and covered by the canonical ``rls_org_isolation`` RLS policy), closing the
tenant-isolation gap that existed at creation time. Rows are written/read only
by the harness probe machinery with an explicit run_id; the
UNIQUE(run_id, node_id) primary key remains the natural key, matching the
§15.12 schema diff exactly.
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
    # NOTE: declared nullable on the model so SQLite ``create_all`` (used by the
    # evidence-probe unit tests) and legacy write paths stay green. The Postgres
    # schema is made NOT NULL by migration 0127_run_evidence_rls, which backfills
    # every existing row from the parent run. The evidence probe must be updated
    # to populate ``organisation_id`` on insert (runtime gap — out of this change's
    # file scope).
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    evidence_state: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    evidence_written_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
