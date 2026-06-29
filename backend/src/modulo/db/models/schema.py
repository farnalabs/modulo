import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Schema(OrgScoped):
    __tablename__ = "schemas"
    __table_args__ = (UniqueConstraint("organisation_id", "name", name="uq_schemas_organisation_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    abstract_name: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchemaVersion(OrgScoped):
    __tablename__ = "schema_versions"
    __table_args__ = (
        UniqueConstraint("schema_id", "version", name="uq_schema_versions_schema_version"),
        UniqueConstraint(
            "schema_id",
            "version",
            "organisation_id",
            name="uq_schema_versions_schema_version_organisation",
        ),
    )

    schema_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("schemas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
