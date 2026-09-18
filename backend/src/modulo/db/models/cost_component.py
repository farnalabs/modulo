import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class CostComponentKind(StrEnum):
    CALCULATED = "calculated"
    SELF_REPORTED = "self_reported"


class CostComponent(SoftDeleteMixin, OrgScoped):  # SoftDeleteMixin FIRST (house pattern)
    """An org-scoped cost component (a named contributor to a run's total cost).

    ``formula`` is NULLABLE — ``NULL`` for ``self_reported`` (the engine
    evaluates the implicit ``reported``), required non-null for ``calculated``.
    Unique constraints are PARTIAL unique indexes (compose with soft delete):
    Postgres-only; on SQLite (dev backend) enforcement is delegated to
    cross-field validation. See migration 0066 for the DDL.
    """

    __tablename__ = "cost_components"
    __table_args__ = (
        CheckConstraint("kind IN ('calculated', 'self_reported')", name="ck_cost_components_kind"),
        Index("ix_cost_components_org_enabled_sort", "organisation_id", "enabled", "sort_order"),
        Index(
            "uq_cost_components_org_name_active",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_cost_components_org_report_key_self",
            "organisation_id",
            "report_key",
            unique=True,
            postgresql_where=text("kind = 'self_reported' AND deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # calculated | self_reported
    rate_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))  # null => not rate-based / env fallback
    rate_fallback: Mapped[str | None] = mapped_column(String(32))  # e.g. "e2b_rate"
    formula: Mapped[str | None] = mapped_column(String(256))  # required iff calculated; NULL for self_reported
    report_key: Mapped[str | None] = mapped_column(String(64))  # required iff kind == self_reported
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    @property
    def uid(self) -> uuid.UUID:  # convenience for API serialization
        return self.id
