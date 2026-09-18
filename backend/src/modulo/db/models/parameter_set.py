import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class ParameterSet(SoftDeleteMixin, OrgScoped):
    __tablename__ = "parameter_sets"
    __table_args__ = (
        Index(
            "uq_parameter_sets_schema_name",
            "parameter_schema_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    parameter_schema_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("parameter_schemas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
