"""Node model — a pipeline node that can be composed into a hierarchy
with parent-child relationships and temporal execution constraints.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.account import Account
    from modulo.db.models.pipeline import Pipeline


class Node(OrgScoped):
    __tablename__ = "nodes"
    __table_args__ = (
        CheckConstraint("timeout_seconds IS NULL OR timeout_seconds > 0", name="ck_nodes_timeout_seconds"),
        CheckConstraint("retry_count IS NULL OR retry_count >= 0", name="ck_nodes_retry_count"),
        CheckConstraint(
            "retry_delay_seconds IS NULL OR retry_delay_seconds >= 0",
            name="ck_nodes_retry_delay_seconds",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_delay_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )

    children: Mapped[list[Node]] = relationship(
        "Node",
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    parent: Mapped[Node | None] = relationship(
        "Node",
        back_populates="children",
        remote_side="Node.id",
        lazy="selectin",
    )
    pipeline: Mapped[Pipeline] = relationship()
    creator: Mapped[Account] = relationship()
