"""Community execution gate grant service (FAR-764 / ADR 032 D2).

When a community-sourced collection is installed, its agents default to a
read-only / fully-denied tool scope until an operator explicitly grants access.
The grant is once-per-install: calling ``grant_collection_agents`` flips
``CollectionInstall.agents_granted`` to True for the given install.

The execution-layer enforcement lives in ``node_runner.py`` and checks
``agents_granted`` via the ``collection_install_id`` FK on each Agent.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.collection_install import CollectionInstall

__all__ = ["grant_collection_agents"]

logger = logging.getLogger(__name__)


class GrantError(Exception):
    """Base error for grant failures."""


class InstallNotFoundError(GrantError):
    """Raised when the install record does not exist or does not belong to the org."""


class AlreadyGrantedError(GrantError):
    """Raised when the install has already been granted."""


class NotCommunitySourcedError(GrantError):
    """Raised when attempting to grant a non-community-sourced install."""


async def grant_collection_agents(
    session: AsyncSession,
    org_id: uuid.UUID,
    install_id: uuid.UUID,
) -> CollectionInstall:
    """Grant access for community-sourced collection agents.

    Sets ``agents_granted=True`` on the install record.  Idempotent if already
    granted.  Raises ``NotCommunitySourcedError`` for non-community installs,
    ``InstallNotFoundError`` when the record is absent or org-mismatched.
    """
    install = await session.get(CollectionInstall, install_id)
    if install is None or install.organisation_id != org_id:
        raise InstallNotFoundError(f"Install {install_id} not found")

    if not install.community_sourced:
        raise NotCommunitySourcedError(
            f"Install {install_id} is not community-sourced; grant is only "
            "available for community-sourced collection installs"
        )

    if install.agents_granted:
        return install

    install.agents_granted = True
    await session.flush()
    logger.info("grant_collection_agents: install %s granted", install_id)
    return install
