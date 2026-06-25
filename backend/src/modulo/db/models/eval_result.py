from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from modulo.db.models.base import OrgScoped


class EvalResult(OrgScoped):
    __tablename__ = "eval_results"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    eval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("eval_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(2000))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
