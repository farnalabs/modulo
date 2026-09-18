import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Numeric, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class Team(SoftDeleteMixin, OrgScoped):  # SoftDeleteMixin FIRST (house pattern)
    __tablename__ = "teams"
    __table_args__ = (
        Index(
            "uq_teams_organisation_name",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    notification_endpoints: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    daily_spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
