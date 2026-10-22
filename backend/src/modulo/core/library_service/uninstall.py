"""Collection uninstall service (FAR-762).

Removes entities created by a collection install, respecting the
modified-entity protection rule: if an entity was modified after install
(i.e. its ``collection_install_id`` was already cleared by the user), it
is detached from provenance rather than deleted.

In reverse topological order (pipelines → agents → schemas): unmodified
entities are deleted; modified entities have their provenance detached.
Per-item savepoints: a blocked deletion (e.g. governance decision records
present) is reported and the batch continues.

The ``CollectionInstall`` provenance row itself is SOFT-deleted (``deleted_at``
stamped), never hard-deleted: it is append-only history and its
``collection_install_entity`` children must survive the uninstall. A second
uninstall of the same install raises ``InstallNotFoundError``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models import Agent, CollectionInstall, CollectionInstallEntity, Pipeline, Schema
from modulo.db.soft_delete import include_soft_deleted

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

    Raises ``InstallNotFoundError`` if the record is absent, belongs to a
    different organisation, or is already retired (soft-deleted).
    """
    install = await session.get(CollectionInstall, install_id)
    if install is None or install.organisation_id != org_id:
        raise InstallNotFoundError(f"Install {install_id} not found for organisation {org_id}")
    # A FRESH ``session.get`` fetch IS covered by the global soft-delete
    # filter: the ``do_orm_execute`` listener injects ``deleted_at IS NULL``
    # into the get-by-PK SELECT (asserted by
    # ``tests/integration/db/test_soft_delete_filter.py``). What bypasses it is
    # a row already in the session IDENTITY MAP, which is returned without a
    # query — so an already-uninstalled row must be rejected explicitly here.
    # Otherwise a second uninstall would "succeed" against a row the rest of
    # the product treats as gone, and would re-run entity deletion for a
    # retired provenance record.
    if install.deleted_at is not None:
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
    if entity_type == "schema":
        entity = await session.scalar(select(Schema).where(Schema.id == entity_id))
        if entity is None:
            return False
        return entity.collection_install_id == install_id
    if entity_type == "agent":
        entity = await session.scalar(select(Agent).where(Agent.id == entity_id))
        if entity is None:
            return False
        return bool(entity.collection_install_id == install_id)
    if entity_type == "pipeline":
        entity = await session.scalar(include_soft_deleted(select(Pipeline).where(Pipeline.id == entity_id)))
        if entity is None:
            return False
        return bool(entity.collection_install_id == install_id)
    return False


async def _delete_entity(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
) -> None:
    """Delete an unmodified entity."""
    if entity_type == "schema":
        entity = await session.scalar(select(Schema).where(Schema.id == entity_id))
        if entity is not None:
            await session.delete(entity)
    elif entity_type == "agent":
        entity = await session.scalar(select(Agent).where(Agent.id == entity_id))
        if entity is not None:
            await session.delete(entity)
    elif entity_type == "pipeline":
        entity = await session.scalar(include_soft_deleted(select(Pipeline).where(Pipeline.id == entity_id)))
        if entity is not None:
            await session.delete(entity)


async def _detach_entity(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
) -> None:
    """Detach provenance from a modified entity (set collection_install_id = None)."""
    if entity_type == "schema":
        entity = await session.scalar(select(Schema).where(Schema.id == entity_id))
        if entity is not None:
            entity.collection_install_id = None
    elif entity_type == "agent":
        entity = await session.scalar(select(Agent).where(Agent.id == entity_id))
        if entity is not None:
            entity.collection_install_id = None
    elif entity_type == "pipeline":
        entity = await session.scalar(include_soft_deleted(select(Pipeline).where(Pipeline.id == entity_id)))
        if entity is not None:
            entity.collection_install_id = None


async def _entity_exists(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
) -> bool:
    """Return True if the tracked entity row still exists.

    A tracking row may outlive its entity when the entity was removed
    out-of-band; in that case there is nothing to delete or detach.
    """
    if entity_type == "schema":
        return await session.scalar(select(Schema).where(Schema.id == entity_id)) is not None
    if entity_type == "agent":
        return await session.scalar(select(Agent).where(Agent.id == entity_id)) is not None
    if entity_type == "pipeline":
        return await session.scalar(include_soft_deleted(select(Pipeline).where(Pipeline.id == entity_id))) is not None
    return False


# Reverse topological order: pipelines → agents → schemas
_UNINSTALL_ORDER: list[str] = ["pipeline", "agent", "schema"]


async def uninstall_collection(
    session: AsyncSession,
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    collection_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Uninstall a collection, removing or detaching entities as appropriate.

    Returns a report dict with ``deleted``, ``detached``, and ``blocked`` entity
    lists.  Modified entities (whose ``collection_install_id`` was already
    cleared) are detached rather than deleted.  A deletion blocked by a RESTRICT
    FK violation (e.g. governance decision records present) is reported as
    ``blocked`` and the batch continues to the next item.

    When ``collection_id`` is supplied it is validated against the install's
    owning collection so an install cannot be uninstalled through the wrong
    collection URL.

    Per-item savepoints: each pipeline deletion is wrapped in a savepoint so a
    blocked item does not roll back the rest of the batch.
    """
    install = await _load_install(session, org_id, install_id)
    if collection_id is not None and install.collection_id != collection_id:
        raise InstallNotFoundError(f"Install {install_id} does not belong to collection {collection_id}")
    entity_rows = await _load_entity_rows(session, install_id)
    sorted_entities = sorted(entity_rows, key=_uninstall_sort_key)

    deleted: list[dict[str, str]] = []
    detached: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []

    for entity_row in sorted_entities:
        await _process_uninstall_entity(
            session,
            entity_row,
            install_id=install_id,
            deleted=deleted,
            detached=detached,
            blocked=blocked,
        )

    # Retire the CollectionInstall provenance row via SOFT delete — the row is
    # append-only history (its ``collection_install_entity`` children stay for
    # audit), so a hard ``session.delete`` would destroy the provenance trail.
    # The partial unique index (org, collection) WHERE deleted_at IS NULL then
    # permits a reinstall, and the global soft-delete filter hides the retired
    # row from every read path.
    install.deleted_at = func.now()
    await session.flush()

    return {
        "install_id": str(install_id),
        "deleted": deleted,
        "detached": detached,
        "blocked": blocked,
    }


def _uninstall_sort_key(entity_row: Any) -> int:
    """Sort key placing entities in reverse topological uninstall order."""
    if entity_row.entity_type in _UNINSTALL_ORDER:
        return _UNINSTALL_ORDER.index(entity_row.entity_type)
    return len(_UNINSTALL_ORDER)


def _entity_ref(entity_row: Any) -> dict[str, str]:
    """Build the report entry identifying a tracked entity."""
    return {
        "entity_type": entity_row.entity_type,
        "entity_id": str(entity_row.entity_id),
    }


async def _process_uninstall_entity(
    session: AsyncSession,
    entity_row: Any,
    *,
    install_id: uuid.UUID,
    deleted: list[dict[str, str]],
    detached: list[dict[str, str]],
    blocked: list[dict[str, str]],
) -> None:
    """Delete an unmodified entity, or detach a modified one, into the report."""
    # The underlying entity may have been removed out-of-band. Skip it:
    # there is nothing to delete or detach, and reporting it as "detached"
    # would be misleading. Its tracking row is kept as provenance — the
    # install record is soft-deleted (not cascaded away) below.
    if not await _entity_exists(session, entity_row.entity_type, entity_row.entity_id):
        return

    if await _check_unmodified(session, entity_row.entity_type, entity_row.entity_id, install_id):
        await _delete_unmodified_entity(session, entity_row, deleted=deleted, blocked=blocked)
        return

    await _detach_entity(session, entity_row.entity_type, entity_row.entity_id)
    detached.append(_entity_ref(entity_row))


async def _delete_unmodified_entity(
    session: AsyncSession,
    entity_row: Any,
    *,
    deleted: list[dict[str, str]],
    blocked: list[dict[str, str]],
) -> None:
    """Delete one unmodified entity in a savepoint, recording the outcome."""
    try:
        async with session.begin_nested():
            await _delete_entity(session, entity_row.entity_type, entity_row.entity_id)
        deleted.append(_entity_ref(entity_row))
    except IntegrityError as exc:
        _record_integrity_block(exc, entity_row, blocked)


def _record_integrity_block(
    exc: IntegrityError,
    entity_row: Any,
    blocked: list[dict[str, str]],
) -> None:
    """Record a policy-gate-decision FK block, re-raising any other integrity error."""
    from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

    if not is_policy_gate_decision_fk_error(exc):
        raise
    blocked.append(
        {
            "entity_type": entity_row.entity_type,
            "entity_id": str(entity_row.entity_id),
            "blocked_by": "policy_gate_decisions",
            "error": (
                f"Cannot delete {entity_row.entity_type} {entity_row.entity_id}: "
                "governance decision records present — "
                "archive or purge policy_gate_decisions first"
            ),
        }
    )
