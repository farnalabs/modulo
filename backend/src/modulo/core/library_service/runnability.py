"""Collection runnability check (FAR-762).

``compute_runnable`` is a pure function derived on read — not stored.
An install is runnable when:

1. All connector requirements in the ``connector_checklist`` are
   ``'configured+bound'`` (every required connector type has an active
   instance in the org).
2. The org has at least one model backend configured.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.collection_install import CollectionInstall
from modulo.db.models.model_backend import ModelBackend

__all__ = ["compute_runnable"]

logger = logging.getLogger(__name__)


async def _org_has_model_backend(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Return True when the org has at least one active model backend."""
    stmt = (
        select(ModelBackend.id)
        .where(
            ModelBackend.organisation_id == org_id,
            ModelBackend.status == "active",
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


def _check_connector_checklist(
    connector_checklist: list[dict[str, Any]] | None,
) -> bool:
    """Return True when all connector requirements are satisfied.

    Each entry in the checklist must have ``status == 'configured+bound'``.
    An empty or missing checklist means no connector requirements exist,
    which is trivially satisfied.
    """
    if not connector_checklist:
        return True
    return all(entry.get("status") == "configured+bound" for entry in connector_checklist if isinstance(entry, dict))


async def compute_runnable(
    session: AsyncSession,
    install_id: uuid.UUID,
) -> bool:
    """Determine whether an install is runnable.

    Pure function on read: checks the connector checklist and org model
    backend presence.  Returns False when any connector is not configured+bound
    or the org has no model backends.
    """
    install = await session.get(CollectionInstall, install_id)
    if install is None:
        return False

    # Check connector requirements
    if not _check_connector_checklist(install.connector_checklist):
        return False

    # Check org has at least one model backend
    return await _org_has_model_backend(session, install.organisation_id)
