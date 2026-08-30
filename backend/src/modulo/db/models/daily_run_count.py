import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, Numeric, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.team import Team


class OrgDailyRunCount(OrgScoped):
    """Daily spend ledger row, keyed ``(organisation_id, team_id, run_date)``.

    ``team_id`` is NULL for the org-level row. The unique index is NULLS NOT
    DISTINCT (Postgres treats NULLs as equal there) so two concurrent
    first-of-day terminals for the same org cannot both insert org rows.
    """

    __tablename__ = "org_daily_run_counts"
    __table_args__ = (
        Index(
            "uq_org_daily_run_counts",
            "organisation_id",
            "team_id",
            "run_date",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("run_count >= 0", name="ck_org_daily_run_counts_run_count"),
        CheckConstraint("total_spend_usd >= 0", name="ck_org_daily_run_counts_total_spend"),
        CheckConstraint("refused_spend_usd >= 0", name="ck_org_daily_run_counts_refused_spend"),
    )

    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal(0), server_default="0"
    )
    # Daily-ledger clamp marker — set by check_and_record_spend when the
    # started-at-day row is stored at the column ceiling. Migration 0066.
    clamped: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Refused amount for the key, set at refusal; survives the run purge.
    # Migration 0066.
    refused_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal(0), server_default="0"
    )
    team: Mapped[Optional["Team"]] = relationship(foreign_keys=[team_id])
