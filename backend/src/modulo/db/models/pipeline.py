from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.user import User


class Pipeline(OrgScoped):
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
            "default_autonomy_level IN (  'manual_approval', 'notify_on_complete', 'fully_autonomous')",
            name="ck_pipelines_autonomy_level",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    stage_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("stages.id", ondelete="SET NULL"))
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"))
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    lock_wait_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    node_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    run_context_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    graph_nodes_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    default_feedback_handler: Mapped[str | None] = mapped_column(String(50))
    default_autonomy_level: Mapped[str | None] = mapped_column(
        String(30),
        server_default="manual_approval",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    organisation: Mapped[Organisation] = relationship()
    creator: Mapped[User] = relationship()
