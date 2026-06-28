import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Team(OrgScoped):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("organisation_id", "name", name="uq_teams_organisation_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    notification_endpoints: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    daily_spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
