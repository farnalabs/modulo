import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class ChatSession(OrgScoped):
    __tablename__ = "chat_sessions"
    __table_args__ = (UniqueConstraint("account_id", "session_number", name="uq_chat_sessions_user_session_number"),)

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    context_window_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt_hash: Mapped[str | None] = mapped_column(String(64))
    session_number: Mapped[int] = mapped_column(Integer, nullable=False)
