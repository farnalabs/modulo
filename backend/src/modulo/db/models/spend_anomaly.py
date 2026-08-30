import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Numeric, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class SpendAnomaly(OrgScoped):
    __tablename__ = "spend_anomalies"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_spend_anomalies_amount"),
        CheckConstraint("baseline >= 0", name="ck_spend_anomalies_baseline"),
    )

    anomaly_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipelines.id", ondelete="SET NULL"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    baseline: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    percent_above: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
