import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Stage(OrgScoped):
    __tablename__ = "stages"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_stages_visibility"),
        CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_stages_team_owner"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"))
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
