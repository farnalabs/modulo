"""CRUD for Team records."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.team import Team


async def create_team(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
) -> Team:
    team = Team(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=description,
    )
    session.add(team)
    await session.flush()
    return team


async def get_team(session: AsyncSession, team_id: uuid.UUID) -> Team | None:
    result = await session.execute(select(Team).where(Team.id == team_id, Team.deleted_at.is_(None)))
    return result.scalar_one_or_none()


async def get_team_by_name(session: AsyncSession, org_id: uuid.UUID, name: str) -> Team | None:
    result = await session.execute(
        select(Team).where(
            Team.organisation_id == org_id,
            Team.name == name,
            Team.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def list_teams(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[Team]:
    count_q = select(func.count()).select_from(Team).where(Team.organisation_id == org_id, Team.deleted_at.is_(None))
    try:
        total_result = await session.execute(count_q)
        total = total_result.scalar() or 0
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)

    try:
        query = (
            select(Team)
            .where(Team.organisation_id == org_id, Team.deleted_at.is_(None))
            .order_by(Team.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await session.execute(query)
        items = list(result.scalars().all())
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)

    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_team(
    session: AsyncSession,
    team_id: uuid.UUID,
    updates: dict[str, object],
) -> Team | None:
    team = await get_team(session, team_id)
    if team is None:
        return None
    apply_updates(team, updates)
    await session.flush()
    return team


async def delete_team(session: AsyncSession, team_id: uuid.UUID) -> bool:
    team = await get_team(session, team_id)
    if team is None:
        return False
    team.deleted_at = datetime.now(UTC)
    await session.flush()
    return True


# Shared implementation for both reassign endpoints (teams + admin). The four
# model classes all carry a check constraint
# ``'visibility = org OR owner_team_id IS NOT NULL'``, so clearing
# ``owner_team_id`` in isolation trips the constraint on rows whose
# ``visibility`` is still ``team``. The same UPDATE must therefore also set
# ``visibility = 'org'``.
_REASSIGN_MODELS: list[tuple[Any, str]] = [
    (Pipeline, "pipeline"),
    (ConnectorInstance, "connector"),
    (ModelBackend, "model backend"),
    (LibraryPrimitive, "library primitive"),
]


async def reassign_team_resources_to_org(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID,
) -> tuple[int, list[str]]:
    """Bulk-reassign every resource owned by *team_id* to org-wide.

    PRD §9.3 team-deletion flow: before deleting a team, the admin reassigns
    all team-owned resources to org-wide (``owner_team_id -> NULL``,
    ``visibility -> 'org'``), after which deletion is no longer blocked by
    ``team_has_resources``. Idempotent: a team with no owned resources returns
    ``reassigned=0``; reassigning already-org resources succeeds.

    Returns a ``(reassigned, resource_types)`` tuple; ``resource_types`` lists
    the labels (pipeline / connector / model backend / library primitive) of
    the model classes that had at least one row reassigned.
    """
    reassigned = 0
    touched: list[str] = []
    for model_cls, label in _REASSIGN_MODELS:
        result = await session.execute(
            update(model_cls)
            .where(
                model_cls.organisation_id == org_id,
                model_cls.owner_team_id == team_id,
            )
            .values(owner_team_id=None, visibility="org")
        )
        count = max(int(result.rowcount or 0), 0)  # type: ignore[attr-defined]
        if count:
            reassigned += count
            touched.append(label)
    return reassigned, touched
