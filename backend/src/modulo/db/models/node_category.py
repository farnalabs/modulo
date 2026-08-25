"""NodeCategory model — user-defined visual categories for pipeline nodes."""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class NodeCategory(SoftDeleteMixin, OrgScoped):
    __tablename__ = "node_categories"
    __table_args__ = (
        Index(
            "uq_node_categories_org_name",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#6366f1")
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
