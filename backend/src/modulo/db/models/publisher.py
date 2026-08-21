from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Publisher(OrgScoped):
    __tablename__ = "publishers"
    __table_args__ = (
        UniqueConstraint("organisation_id", "public_key_hex", name="uq_publishers_org_key"),
        UniqueConstraint("organisation_id", "name", name="uq_publishers_org_name"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    public_key_hex: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trust_tier: Mapped[str] = mapped_column(String(10), nullable=False, server_default="amber")
    verified_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    website_url: Mapped[str | None] = mapped_column(String(2000))
