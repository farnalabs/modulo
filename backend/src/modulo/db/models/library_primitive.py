import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class LibraryPrimitive(SoftDeleteMixin, OrgScoped):
    __tablename__ = "library_primitives"
    __table_args__ = (
        CheckConstraint("source IN ('local', 'registry', 'modulo', 'community')", name="ck_library_primitives_source"),
        CheckConstraint(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', "
            "'test_fixture', 'pipeline_template', 'composite', 'lifecycle_map')",
            name="ck_library_primitives_type",
        ),
        CheckConstraint(
            "visibility IN ('org', 'team', 'community')",
            name="ck_library_primitives_visibility",
        ),
        CheckConstraint(
            "visibility IN ('org', 'community') OR owner_team_id IS NOT NULL",
            name="ck_library_primitives_team_owner",
        ),
        CheckConstraint(
            "contribution_status IN ('draft', 'review_queue', 'published')",
            name="ck_library_primitives_contribution_status",
        ),
        CheckConstraint(
            "(source = 'local' AND source_url IS NULL AND checksum IS NULL "
            "AND ed25519_signature IS NULL AND verified IS NULL "
            "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
            "OR (source = 'modulo' AND source_url IS NULL AND checksum IS NULL "
            "AND ed25519_signature IS NULL AND verified IS NULL "
            "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
            "OR (source = 'community' AND source_url IS NULL AND checksum IS NULL "
            "AND ed25519_signature IS NULL "
            "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
            "OR (source = 'registry' AND owner_team_id IS NULL AND visibility = 'org' "
            "AND forked_from IS NULL AND source_url IS NOT NULL AND checksum IS NOT NULL "
            "AND verified IS NOT NULL "
            "AND download_count IS NOT NULL)",
            name="ck_library_primitives_source_fields",
        ),
        CheckConstraint(
            "average_rating IS NULL OR average_rating BETWEEN 1 AND 5",
            name="ck_library_primitives_rating",
        ),
        CheckConstraint(
            "tier IN ('native', 'preview', 'in_dev')",
            name="ck_library_primitives_tier",
        ),
        Index(
            "uq_library_primitive_version",
            "organisation_id",
            "source",
            "slug",
            "version",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    primitive_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    forked_from: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("library_primitives.id", ondelete="RESTRICT")
    )
    checksum: Mapped[str | None] = mapped_column(String(128))
    ed25519_signature: Mapped[str | None] = mapped_column(String(256))
    verified: Mapped[bool | None] = mapped_column(Boolean)
    category: Mapped[str | None] = mapped_column(String(50))
    download_count: Mapped[int | None] = mapped_column(Integer)
    average_rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    review_count: Mapped[int | None] = mapped_column(Integer)
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"))
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    contribution_status: Mapped[str | None] = mapped_column(String(20))
    auto_update: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"))
    version_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
    )
    update_available_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("library_primitives.id", ondelete="SET NULL"),
        nullable=True,
    )
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="native")
