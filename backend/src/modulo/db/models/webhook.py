import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, LargeBinary, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class WebhookPayload(OrgScoped):
    __tablename__ = "webhook_payloads"

    trigger_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("trigger_events.id", ondelete="CASCADE"), nullable=True
    )
    raw_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
