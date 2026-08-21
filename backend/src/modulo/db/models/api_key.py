import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class OrgApiKey(OrgScoped):
    __tablename__ = "org_api_keys"
    __table_args__ = (
        CheckConstraint("role IN ('operator', 'runner')", name="ck_org_api_keys_role"),
        UniqueConstraint("lookup_prefix", name="uq_org_api_keys_lookup_prefix"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    lookup_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    hashed_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
