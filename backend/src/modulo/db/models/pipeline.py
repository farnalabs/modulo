import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

if TYPE_CHECKING:
    from modulo.db.models.account import Account
    from modulo.db.models.organisation import Organisation


class Pipeline(SoftDeleteMixin, OrgScoped):
    __tablename__ = "pipelines"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_pipelines_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_pipelines_team_owner",
        ),
        CheckConstraint("max_concurrent_runs > 0", name="ck_pipelines_max_concurrent_runs"),
        CheckConstraint(
            "lock_wait_timeout_seconds BETWEEN 30 AND 3600",
            name="ck_pipelines_lock_wait_timeout",
        ),
        CheckConstraint("node_timeout_seconds > 0", name="ck_pipelines_node_timeout"),
        CheckConstraint(
            "default_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')",
            name="ck_pipelines_autonomy_level",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipeline_folders.id", ondelete="SET NULL"), index=True
    )
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    lock_wait_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    node_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    max_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600, server_default="3600")
    max_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cost-control circuit breaker (FAR-105, spec §8.10) — per-pipeline monthly
    # spend threshold. When the pipeline's monthly accumulated spend + a new
    # run's cost would exceed ``circuit_breaker_threshold``, the breaker trips
    # (``circuit_breaker_tripped``), permanently pausing the pipeline's
    # triggers until an admin re-enables the pipeline. Migration 0086.
    circuit_breaker_threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    circuit_breaker_tripped: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    circuit_breaker_tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_context_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    default_autonomy_level: Mapped[str | None] = mapped_column(String(30), server_default="manual_approval")
    graph_nodes_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    default_feedback_handler: Mapped[str | None] = mapped_column(String(50))
    rate_limit_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    stale_run_timeout_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default=text("'30'")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    organisation: Mapped["Organisation"] = relationship()
    creator: Mapped["Account"] = relationship()
