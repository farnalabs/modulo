import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class SpendAnomaly(OrgScoped):
    __tablename__ = "spend_anomalies"

    anomaly_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipelines.id", ondelete="SET NULL"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    baseline: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    percent_above: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
