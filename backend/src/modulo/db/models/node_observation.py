import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class NodeObservation(OrgScoped):
    __tablename__ = "node_observations"
    __table_args__ = (UniqueConstraint("run_id", "node_id", name="uq_node_observations_run_node"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    human_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
