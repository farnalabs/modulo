import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, OrgScoped


class AuditEvent(OrgScoped):
    __tablename__ = "audit_events"

    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(255))
    previous_hash: Mapped[str | None] = mapped_column(Text)


class AuditChainHead(Base):
    """Tracks the most recent audit event hash per organisation."""

    __tablename__ = "audit_chain_heads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    last_event_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("audit_events.id", ondelete="SET NULL"), index=True
    )
    event_count: Mapped[int] = mapped_column(nullable=False, default=0)
