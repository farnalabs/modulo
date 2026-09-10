"""Collection install/uninstall provenance (FAR-761).

``CollectionInstall`` is the org-scoped audit row for each install of a library
primitive into an organisation. ``CollectionInstallEntity`` is the per-entity
child recording exactly which schemas/agents/pipelines an install wrote.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, ForeignKeyConstraint, PrimaryKeyConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, OrgScoped


class CollectionInstall(OrgScoped):
    """One row per install attempt of a library primitive into an organisation.

    ``status`` is ``'installed'`` or ``'failed'``; ``resolved_manifest`` /
    ``connector_checklist`` / ``installed_entities`` capture the resolved
    manifest and the per-connector checklist outcome for debugging.
    """

    __tablename__ = "collection_install"

    collection_id: Mapped[UUID] = mapped_column(
        Uuid(),
        ForeignKey("library_primitives.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    collection_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    resolved_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    connector_checklist: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    installed_entities: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_collection_install"),
        ForeignKeyConstraint(
            ["collection_id"],
            ["library_primitives.id"],
            ondelete="RESTRICT",
            name="fk_collection_install_collection_id",
        ),
    )


class CollectionInstallEntity(Base):
    """One row per entity an install wrote (schema / agent / pipeline).

    The entity table has no ``organisation_id`` of its own — access is always
    via the org-scoped parent's ``install_id``.
    """

    __tablename__ = "collection_install_entity"

    install_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("install_id", "entity_type", "entity_id", name="pk_collection_install_entity"),
        ForeignKeyConstraint(
            ["install_id"],
            ["collection_install.id"],
            ondelete="CASCADE",
            name="fk_collection_install_entity_install_id",
        ),
    )
