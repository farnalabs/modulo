import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class TeamMembership(OrgScoped):
    __tablename__ = "team_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('viewer', 'runner', 'operator')", name="ck_team_memberships_role"),
        UniqueConstraint("team_id", "account_id", name="uq_team_memberships_team_account"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
