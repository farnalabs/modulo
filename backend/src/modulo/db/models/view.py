"""Saved View model — persisted filters and display preferences for run views."""

import uuid

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class SavedView(OrgScoped):
    """A saved view/filter configuration for the runs listing page."""

    __tablename__ = "saved_views"
    __table_args__ = (
        CheckConstraint("view_type IN ('run_list', 'pipeline_list', 'audit_log')", name="ck_saved_views_type"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    view_type: Mapped[str] = mapped_column(String(50), nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    columns: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    sort_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sort_order: Mapped[str] = mapped_column(String(10), nullable=False, default="desc")
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
