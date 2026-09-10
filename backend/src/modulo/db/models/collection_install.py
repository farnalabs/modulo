"""Collection install/uninstall provenance (FAR-761).

``CollectionInstall`` is the org-scoped audit row for each install of a library
primitive into an organisation. ``CollectionInstallEntity`` is the per-entity
child recording exactly which schemas/agents/pipelines an install wrote.

The ORM metadata here is kept byte-for-byte consistent with migration
``0206_collection_install_tracking`` — the primary key is ``install_id`` (NOT
``id``), the table carries ``created_at`` only (no ``updated_at``), and the child
FK targets ``collection_install.install_id``. A conformance test
(``tests/unit/db/test_migration_0206_collection_install_tracking.py``) pins these
invariants so model/migration drift cannot silently pass the mocked unit tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class CollectionInstall(Base):
    """One row per install attempt of a library primitive into an organisation.

    ``status`` is ``'installed'`` or ``'failed'``; ``resolved_manifest`` /
    ``connector_checklist`` / ``installed_entities`` capture the resolved
    manifest and the per-connector checklist outcome for debugging.

    The primary key is ``install_id`` (the migration does NOT use the
    ``OrgScoped``-inherited ``id`` column, and the table has no ``updated_at``
    — provenance rows are append-only history).
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
    resolved_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    connector_checklist: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    installed_entities: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

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
        UniqueConstraint("organisation_id", "collection_id", name="uq_collection_install_org_collection"),
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
