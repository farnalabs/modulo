import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class LifecycleMap(SoftDeleteMixin, OrgScoped):
    __tablename__ = "lifecycle_maps"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_lifecycle_maps_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_lifecycle_maps_team_owner",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_lifecycle_maps_version",
        ),
        # Partial unique from migration 0156_add_soft_delete_partial_uniques —
        # one live map per (org, name); a soft-deleted row no longer blocks a
        # re-create. Declared on the model so the ORM metadata matches the DB.
        Index(
            "uq_lifecycle_maps_org_name",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            # sqlite_where as well: postgresql_where is ignored on SQLite, so
            # create_all'd schemas would otherwise build a FULL unique index.
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # The DB column is ``jsonb`` (migration 0147_json_to_jsonb_standardize).
    # The PG variant aligns the ORM declaration to that real column type, so a
    # comparison cannot bind as ``json`` and fail with ``operator does not
    # exist: jsonb = json`` — housekeeping's lifecycle-map emptiness predicate
    # was the demonstrated instance (FAR-1856). Generic ``JSON`` keeps SQLite
    # parity for the unit suites; the wider json-to-jsonb ORM alignment is
    # tracked as FAR-1878.
    content_json: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
