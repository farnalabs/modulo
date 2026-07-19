import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Date, ForeignKey, Integer, Numeric, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.team import Team


class OrgDailyRunCount(OrgScoped):
    __tablename__ = "org_daily_run_counts"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "team_id",
            "run_date",
            name="uq_org_daily_run_counts_org_team_date",
        ),
    )

    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="CASCADE"))
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal(0), server_default="0"
    )
    team: Mapped[Optional["Team"]] = relationship(foreign_keys=[team_id])
