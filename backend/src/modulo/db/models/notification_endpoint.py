import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Uuid
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import JSONB_COL, OrgScoped, SoftDeleteMixin


class NotificationEndpoint(SoftDeleteMixin, OrgScoped):
    __tablename__ = "notification_endpoints"

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    events: Mapped[list[str]] = mapped_column(JSONB_COL, nullable=False, server_default=sa_text("'[]'"))

    description: Mapped[str | None] = mapped_column(String(500))
    consecutive_dead_letter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    auto_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="CASCADE"), index=True)
