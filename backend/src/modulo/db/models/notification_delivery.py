import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class NotificationDeliveryLog(OrgScoped):
    __tablename__ = "notification_delivery_log"

    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("notification_endpoints.id", ondelete="SET NULL"),
        index=True,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="delivered")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    response_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(2000))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_body: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    payload_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
