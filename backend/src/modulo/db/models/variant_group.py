import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import JSONB_COL, OrgScoped, SoftDeleteMixin

if TYPE_CHECKING:
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline


class VariantGroup(SoftDeleteMixin, OrgScoped):
    __tablename__ = "variant_groups"
    __table_args__ = (
        CheckConstraint(
            "selection_strategy IN ('weighted', 'single')",
            name="ck_variant_groups_selection_strategy",
        ),
        # Partial unique from migration 0156_add_soft_delete_partial_uniques —
        # one live variant group per (org, name); a soft-deleted row no longer
        # blocks a re-create. Declared on the model so the ORM metadata matches
        # the DB.
        Index(
            "uq_variant_groups_org_name",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            # sqlite_where as well: postgresql_where is ignored on SQLite, so
            # create_all'd schemas would otherwise build a FULL unique index.
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    variants: Mapped[list[dict[str, Any]]] = mapped_column(JSONB_COL, nullable=False, default=list)
    selection_strategy: Mapped[str] = mapped_column(String(20), nullable=False, server_default="weighted")
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    degraded_evals: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    organisation: Mapped["Organisation"] = relationship()
    pipeline: Mapped["Pipeline"] = relationship()
