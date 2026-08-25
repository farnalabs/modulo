import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.account import Account

# FK target for the owning account (repeated across the schema tables).
_FK_ACCOUNTS_ID = "accounts.id"


class SchemaFolder(OrgScoped):
    __tablename__ = "schema_folders"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("schema_folders.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey(_FK_ACCOUNTS_ID, ondelete="RESTRICT"), nullable=False, index=True
    )

    parent: Mapped[Optional["SchemaFolder"]] = relationship(
        "SchemaFolder", remote_side="SchemaFolder.id", back_populates="children"
    )
    children: Mapped[list["SchemaFolder"]] = relationship(
        "SchemaFolder", remote_side="SchemaFolder.parent_id", back_populates="parent"
    )
    creator: Mapped["Account"] = relationship()


class Schema(OrgScoped):
    __tablename__ = "schemas"
    __table_args__ = (UniqueConstraint("organisation_id", "name", name="uq_schemas_organisation_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    abstract_name: Mapped[str | None] = mapped_column(String(255))
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey(_FK_ACCOUNTS_ID, ondelete="RESTRICT"), nullable=False, index=True
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("schema_folders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    folder: Mapped[Optional["SchemaFolder"]] = relationship("SchemaFolder")


class SchemaVersion(OrgScoped):
    __tablename__ = "schema_versions"
    __table_args__ = (
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
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey(_FK_ACCOUNTS_ID, ondelete="RESTRICT"), nullable=False, index=True
    )
