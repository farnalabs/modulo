import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class NotificationEndpoint(OrgScoped):
    __tablename__ = "notification_endpoints"

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(nullable=True)
    events: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    description: Mapped[str | None] = mapped_column(String(500))
    consecutive_dead_letter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    auto_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
