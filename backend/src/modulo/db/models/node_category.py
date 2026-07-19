"""NodeCategory model — user-defined visual categories for pipeline nodes."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class NodeCategory(OrgScoped):
    __tablename__ = "node_categories"
    __table_args__ = (UniqueConstraint("organisation_id", "name", name="uq_node_categories_org_name"),)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#6366f1")
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
