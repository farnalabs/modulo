from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.team import Team


class Run(OrgScoped):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction')",
            name="ck_runs_trigger_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'awaiting_human', 'claimed', "
            "'waiting_for_lock', 'complete', 'failed', 'cancelled', 'eval_failed')",
            name="ck_runs_status",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("pipeline_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("triggers.id", ondelete="SET NULL"))
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    node_token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_detail: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String(255))
    langgraph_thread_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    outputs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    organisation: Mapped[Organisation] = relationship()
    pipeline: Mapped[Pipeline] = relationship()
    snapshot: Mapped[PipelineSnapshot] = relationship()
    owner_team: Mapped[Team | None] = relationship()
