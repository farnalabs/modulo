"""Collection install/uninstall provenance (FAR-761).

``CollectionInstall`` is the org-scoped audit row for each install of a library
primitive into an organisation. ``CollectionInstallEntity`` is the per-entity
child recording exactly which schemas/agents/pipelines an install wrote.

The ORM metadata here was originally kept byte-for-byte consistent with
migration ``0207_collection_install_tracking`` — the primary key is
``install_id`` (NOT ``id``), the table carries ``created_at`` only (no
``updated_at``), and the child FK targets ``collection_install.install_id``. A
conformance test (``tests/unit/db/test_migration_0207_collection_install_tracking.py``)
pins those invariants so model/migration drift cannot silently pass the mocked
unit tests.

Migration ``0308_library_cluster_db_hardening`` then extended the table with the
two columns a provenance row was missing: ``deleted_at`` (soft delete — an
uninstall marks the row instead of destroying the audit trail, and the
``SoftDeleteMixin`` global filter hides it from reads) and ``created_by`` (the
account that performed the install). The ``0207`` ``UniqueConstraint`` on
``organisation_id``/``collection_id`` was converted to a partial unique index
with the SAME name and a ``WHERE deleted_at IS NULL`` predicate, so an
install → uninstall → reinstall cycle does not collide with the retired row.
``collection_install`` is NOT ``OrgScoped``-derived (explicit columns only, and
deliberately no ``updated_at`` — provenance rows are history), so
``SoftDeleteMixin`` is mixed in directly rather than through ``OrgScoped``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import ONDELETE_SET_NULL, Base, SoftDeleteMixin

# Repeated column type (S1192): JSON with PostgreSQL JSONB variant. The three
# provenance payload columns are jsonb in the DB (migration 0207); the PG
# variant keeps the ORM honest against the migrated schema while the generic
# JSON keeps SQLite parity (the repo-wide multi-backend convention).
_JSONB_COL = JSON().with_variant(JSONB(), "postgresql")


class CollectionInstall(SoftDeleteMixin, Base):
    """One row per install attempt of a library primitive into an organisation.

    ``status`` is ``'installed'`` or ``'failed'``; ``resolved_manifest`` /
    ``connector_checklist`` / ``installed_entities`` capture the resolved
    manifest and the per-connector checklist outcome for debugging.

    The primary key is ``install_id`` (the migration does NOT use the
    ``OrgScoped``-inherited ``id`` column, and the table has no ``updated_at``
    — provenance rows are append-only history). ``deleted_at`` (soft delete)
    is the one exception to "no schema changes after 0207": an uninstall now
    stamps it instead of destroying the row (see the module docstring).
    """

    __tablename__ = "collection_install"

    install_id: Mapped[UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    collection_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False, index=True)
    collection_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    organisation_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    community_sourced: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    agents_granted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    resolved_manifest: Mapped[dict[str, Any] | None] = mapped_column(_JSONB_COL, nullable=True)
    connector_checklist: Mapped[list[dict[str, Any]] | None] = mapped_column(_JSONB_COL, nullable=True)
    installed_entities: Mapped[list[dict[str, Any]] | None] = mapped_column(_JSONB_COL, nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        Uuid(),
        # Named to match migration 0308's explicit DDL so create_all and the
        # migrated DB agree (the sibling FKs on this table are named too).
        ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL, name="fk_collection_install_created_by"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            ondelete="CASCADE",
            name="fk_collection_install_organisation_id",
        ),
        ForeignKeyConstraint(
            ["collection_id"],
            ["library_primitives.id"],
            ondelete="RESTRICT",
            name="fk_collection_install_collection_id",
        ),
        CheckConstraint(
            "status IN ('installed', 'failed')",
            name="ck_collection_install_status",
        ),
        # Partial unique (0308): one LIVE install per (org, collection); a
        # soft-deleted (uninstalled) row no longer blocks a reinstall.
        Index(
            "uq_collection_install_org_collection",
            "organisation_id",
            "collection_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            # sqlite_where as well: postgresql_where is ignored on SQLite, so
            # create_all'd schemas would otherwise build a FULL unique index.
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class CollectionInstallEntity(Base):
    """One row per entity an install wrote (schema / agent / pipeline).

    The entity table has no ``organisation_id`` of its own — access is always
    via the org-scoped parent's ``install_id``. The FK targets
    ``collection_install.install_id`` (the real primary key column).
    """

    __tablename__ = "collection_install_entity"

    install_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("install_id", "entity_type", "entity_id", name="pk_collection_install_entity"),
        ForeignKeyConstraint(
            ["install_id"],
            ["collection_install.install_id"],
            ondelete="CASCADE",
            name="fk_collection_install_entity_install_id",
        ),
    )
