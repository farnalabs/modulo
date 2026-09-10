"""Collection uninstall service (FAR-762).

Removes entities created by a collection install, respecting the
modified-entity protection rule: if an entity was modified after install
(i.e. its ``collection_install_id`` was already cleared by the user), it
is detached from provenance rather than deleted.

In reverse topological order (pipelines → agents → schemas): unmodified
entities are deleted; modified entities have their provenance detached.
All-or-nothing: any failure rolls back the entire uninstall.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.agent import Agent
from modulo.db.models.collection_install import CollectionInstall, CollectionInstallEntity
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.schema import Schema

# Union of the entity types a collection install can track.  Reused across the
# per-entity-type branches below so ``session.get`` resolves to the correct
# model rather than being constrained by the first branch's assignment.
type CollectionEntity = "Schema | Agent | Pipeline"

__all__ = ["uninstall_collection"]

logger = logging.getLogger(__name__)


class UninstallError(Exception):
    """Base error for collection uninstall failures."""


class InstallNotFoundError(UninstallError):
    """Raised when the install record does not exist or belongs to another org."""


async def _load_install(
    session: AsyncSession,
    org_id: uuid.UUID,
    install_id: uuid.UUID,
) -> CollectionInstall:
    """Load and validate the install record.

    Raises ``InstallNotFoundError`` if the record is absent or belongs to a
    different organisation.
    """
    install = await session.get(CollectionInstall, install_id)
    if install is None or install.organisation_id != org_id:
        raise InstallNotFoundError(f"Install {install_id} not found for organisation {org_id}")
    return install


async def _load_entity_rows(
    session: AsyncSession,
    install_id: uuid.UUID,
) -> list[CollectionInstallEntity]:
    """Load all entity tracking rows for this install."""
    stmt = select(CollectionInstallEntity).where(CollectionInstallEntity.install_id == install_id)
    result = await session.execute(stmt)
    return list(result.scalars())


async def _check_unmodified(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    install_id: uuid.UUID,
) -> bool:
    """Check if an entity still carries the install's provenance stamp.

        Returns True if the entity is unmodified (collection_install_id still
    matches); False if the user already detached it.
    """
    entity: CollectionEntity | None = None
    if entity_type == "schema":
        entity = await session.get(Schema, entity_id)
        if entity is None:
            return False
        return entity.collection_install_id == install_id
    if entity_type == "agent":
        entity = await session.get(Agent, entity_id)
        if entity is None:
            return False
        return entity.collection_install_id == install_id
    if entity_type == "pipeline":
        entity = await session.get(Pipeline, entity_id)
        if entity is None:
            return False
        return entity.collection_install_id == install_id
    return False


async def _delete_entity(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
) -> None:
    """Delete an unmodified entity."""
    entity: CollectionEntity | None = None
    if entity_type == "schema":
        entity = await session.get(Schema, entity_id)
        if entity is not None:
            await session.delete(entity)
    elif entity_type == "agent":
        entity = await session.get(Agent, entity_id)
        if entity is not None:
            await session.delete(entity)
    elif entity_type == "pipeline":
        entity = await session.get(Pipeline, entity_id)
        if entity is not None:
            await session.delete(entity)


async def _detach_entity(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
) -> None:
    """Detach provenance from a modified entity (set collection_install_id = None)."""
    entity: CollectionEntity | None = None
    if entity_type == "schema":
        entity = await session.get(Schema, entity_id)
        if entity is not None:
            entity.collection_install_id = None
    elif entity_type == "agent":
        entity = await session.get(Agent, entity_id)
        if entity is not None:
            entity.collection_install_id = None
    elif entity_type == "pipeline":
        entity = await session.get(Pipeline, entity_id)
        if entity is not None:
            entity.collection_install_id = None


# Reverse topological order: pipelines → agents → schemas
_UNINSTALL_ORDER: list[str] = ["pipeline", "agent", "schema"]


async def uninstall_collection(
    session: AsyncSession,
    org_id: uuid.UUID,
    install_id: uuid.UUID,
) -> dict[str, Any]:
    """Uninstall a collection, removing or deterring entities as appropriate.

    Returns a report dict with ``deleted`` and ``detached`` entity lists.
    Modified entities (whose ``collection_install_id`` was already cleared)
    are detached rather than deleted.

    All-or-nothing: if any step fails, the entire uninstall is rolled back.
    """
    install = await _load_install(session, org_id, install_id)
    entity_rows = await _load_entity_rows(session, install_id)

    # Sort entities by uninstall order
    sorted_entities = sorted(
        entity_rows,
        key=lambda e: (
            _UNINSTALL_ORDER.index(e.entity_type) if e.entity_type in _UNINSTALL_ORDER else len(_UNINSTALL_ORDER)
        ),
    )

    deleted: list[dict[str, str]] = []
    detached: list[dict[str, str]] = []

    for entity_row in sorted_entities:
        is_unmodified = await _check_unmodified(
            session,
            entity_row.entity_type,
            entity_row.entity_id,
            install_id,
        )

        if is_unmodified:
            await _delete_entity(session, entity_row.entity_type, entity_row.entity_id)
            deleted.append(
                {
                    "entity_type": entity_row.entity_type,
                    "entity_id": str(entity_row.entity_id),
                }
            )
        else:
            await _detach_entity(session, entity_row.entity_type, entity_row.entity_id)
            detached.append(
                {
                    "entity_type": entity_row.entity_type,
                    "entity_id": str(entity_row.entity_id),
                }
            )

    # Delete the CollectionInstall record + child rows (CASCADE deletes entities)
    await session.delete(install)
    await session.flush()

    return {
        "install_id": str(install_id),
        "deleted": deleted,
        "detached": detached,
    }
