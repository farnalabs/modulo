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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

# Repeated column type (S1192): JSON with PostgreSQL JSONB variant. Every JSON
# column on this table is jsonb in the DB — 0147 standardised ``tags`` /
# ``content_json``, and 0205 created ``manifest_pins`` / ``trust_header``
# directly as JSONB; the PG variant keeps the ORM honest against the migrated
# schema while the generic JSON keeps SQLite parity (the repo-wide
# multi-backend convention).
_JSONB_COL = JSON().with_variant(JSONB(), "postgresql")

# Repeated partial-index predicate (S1192): the live-row predicate shared by the
# two partial indexes below. Kept as one constant so the three declarations
# cannot drift apart.
_DELETED_AT_WHERE = text("deleted_at IS NULL")

# Repeated self-FK target (S1192): every self-referential FK on this table points
# at ``library_primitives.id``. Kept as one constant so the three declarations
# cannot drift apart.
_LIBRARY_PRIMITIVES_ID = "library_primitives.id"

# Repeated FK action (S1192): ``SET NULL`` on the three nullable FK columns so a
# deleted target row never cascades into destroying the referencing row.
_SET_NULL = "SET NULL"


class LibraryPrimitive(SoftDeleteMixin, OrgScoped):
    __tablename__ = "library_primitives"
    __table_args__ = (
        CheckConstraint("source IN ('local', 'registry', 'modulo', 'community')", name="ck_library_primitives_source"),
        CheckConstraint(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', "
            "'test_fixture', 'pipeline_template', 'composite', 'lifecycle_map', "
            "'library_collection')",
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
        CheckConstraint(
            "download_count IS NULL OR download_count >= 0",
            name="ck_library_primitives_dl_count",
        ),
        CheckConstraint(
            "review_count IS NULL OR review_count >= 0",
            name="ck_library_primitives_review_count",
        ),
        CheckConstraint(
            "status IS NULL OR status IN ('draft', 'published')",
            name="ck_library_primitives_status",
        ),
        Index(
            "uq_library_primitive_version",
            "organisation_id",
            "source",
            "slug",
            "version",
            unique=True,
            postgresql_where=_DELETED_AT_WHERE,
        ),
        # Slug lookup: get_primitive_by_slug filters primitive_type + slug and is
        # called once per manifest pin on collection install/upgrade. The partial
        # unique above cannot serve it (wrong leading column).
        Index("ix_library_primitives_type_slug", "primitive_type", "slug"),
        # Community catalogue listing: _fetch_published_community_from_db filters
        # contribution_status + visibility in SQL and orders by created_at.
        # LEADS with those two constrained columns on purpose: the query does
        # NOT constrain organisation_id (it pops the tenant context to read the
        # cross-org community view), and a B-tree cannot skip-scan an
        # unconstrained leading column, so organisation_id would make the index
        # unusable for its only motivating query. Org-scoped reads of this table
        # keep their own ``ix_library_primitives_organisation_id``.
        Index(
            "ix_library_primitives_visibility_contribution",
            "visibility",
            "contribution_status",
            postgresql_where=_DELETED_AT_WHERE,
            sqlite_where=_DELETED_AT_WHERE,
        ),
    )

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    primitive_type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    tags: Mapped[list[str]] = mapped_column(_JSONB_COL, nullable=False, default=list)
    content_json: Mapped[dict[str, Any]] = mapped_column(_JSONB_COL, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    forked_from: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_LIBRARY_PRIMITIVES_ID, ondelete="RESTRICT"), index=True
    )
    checksum: Mapped[str | None] = mapped_column(String(128))
    ed25519_signature: Mapped[str | None] = mapped_column(String(256))
    verified: Mapped[bool | None] = mapped_column(Boolean)
    category: Mapped[str | None] = mapped_column(String(50))
    download_count: Mapped[int | None] = mapped_column(Integer)
    average_rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    review_count: Mapped[int | None] = mapped_column(Integer)
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    contribution_status: Mapped[str | None] = mapped_column(String(20))
    auto_update: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete=_SET_NULL), index=True
    )
    version_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        # Self-FK (FAR-1758): the service always points this at an EXISTING
        # ``library_primitives.id`` in the same organisation
        # (``version_group_id or existing.id``), mirroring the sibling
        # self-references above. ``SET NULL`` so a hard-deleted group member
        # never destroys its siblings' rows; migration 0311 adds the same
        # ``enforce_same_organisation`` tenant trigger the siblings carry, so a
        # cross-org (or dangling) value fails loudly instead of being skipped.
        ForeignKey(_LIBRARY_PRIMITIVES_ID, ondelete=_SET_NULL),
        nullable=True,
        index=True,
    )
    update_available_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_LIBRARY_PRIMITIVES_ID, ondelete=_SET_NULL), nullable=True, index=True
    )
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="native")
    # Collection authoring lifecycle fields (FAR-760).
    status: Mapped[str | None] = mapped_column(String(20))
    manifest_pins: Mapped[list[dict[str, Any]] | None] = mapped_column(_JSONB_COL)
    trust_header: Mapped[dict[str, Any] | None] = mapped_column(_JSONB_COL)
