import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class HitlClaim(OrgScoped):
    __tablename__ = "hitl_claims"
    __table_args__ = (UniqueConstraint("run_id", "gate_id", name="uq_hitl_claims_run_gate"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    required_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    gate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(20))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Full resume payload persisted at decision time (B1) - the same dict the
    # web routes pass to executor.resume. Survives SAQ job loss so a recovered
    # resume injects the human's actual verdict, never an empty approval.
    # jsonb in the parallel migration; generic JSON keeps SQLite/MariaDB parity.
    decision_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set by the hitl_overdue notification job once a `hitl_overdue` event has
    # been dispatched for this claim — keeps the job idempotent (one warning
    # per claim, no re-alerting every tick).
    overdue_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
