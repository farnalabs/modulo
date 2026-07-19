import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class OrgMembership(OrgScoped):
    __tablename__ = "org_memberships"
    __table_args__ = (
        UniqueConstraint("account_id", "organisation_id", name="uq_org_memberships_account_org"),
        CheckConstraint("role IN ('owner', 'admin', 'operator', 'runner', 'viewer')", name="ck_org_memberships_role"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="runner")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
