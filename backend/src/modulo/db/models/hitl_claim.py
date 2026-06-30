import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class HitlClaim(OrgScoped):
    __tablename__ = "hitl_claims"
    __table_args__ = (UniqueConstraint("run_id", "gate_id", name="uq_hitl_claims_run_gate"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    required_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"))
    gate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(String(20))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
