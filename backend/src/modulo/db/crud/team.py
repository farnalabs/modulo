"""CRUD for Team records."""

import uuid
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.team import Team


class TeamUpdateOutcome(Enum):
    """Result of an optimistic-concurrency team update."""

    UPDATED = "updated"
    NOT_FOUND = "not_found"
    STALE = "stale"


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


async def update_team_if_unchanged(
    session: AsyncSession,
    team_id: uuid.UUID,
    updates: dict[str, object],
    expected_updated_at: str,
) -> tuple[TeamUpdateOutcome, Team | None]:
    """Optimistic-concurrency team update: the version check is part of the write.

    A single conditional UPDATE bumps ``updated_at`` and only matches rows whose
    ``updated_at`` still equals the caller's ``expected_updated_at``. Under
    READ COMMITTED two truly concurrent writers both run this UPDATE; the first
    to commit wins, and the second matches zero rows (the winner already bumped
    ``updated_at``) and is reported as STALE. This closes the lost-update race
    that a separate SELECT-then-UPDATE check-then-write leaves open.
    """
    try:
        expected = datetime.fromisoformat(expected_updated_at)
    except ValueError:
        return TeamUpdateOutcome.STALE, None
    result = await session.execute(
        update(Team)
        .where(Team.id == team_id, Team.deleted_at.is_(None), Team.updated_at == expected)
        .values(**updates, updated_at=datetime.now(UTC))
        .returning(Team)
    )
    team = result.scalar_one_or_none()
    if team is not None:
        return TeamUpdateOutcome.UPDATED, team
    exists = await session.execute(select(Team.id).where(Team.id == team_id, Team.deleted_at.is_(None)))
    if exists.scalar_one_or_none() is None:
        return TeamUpdateOutcome.NOT_FOUND, None
    return TeamUpdateOutcome.STALE, None


async def delete_team(session: AsyncSession, team_id: uuid.UUID) -> bool:
    team = await get_team(session, team_id)
    if team is None:
        return False
    team.deleted_at = datetime.now(UTC)
    await session.flush()
    return True


async def count_owned_resources(
    session: AsyncSession,
    *,
    team_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Count owned resources (the 4-way delete-blocking set) per team.

    Mirrors the resource check in the delete endpoints: a resource blocks
    deletion when its ``owner_team_id`` points at the team. The set is the
    same 4 types — pipeline, connector, model backend, library primitive —
    so the count shown in the team list is always consistent with what would
    block a delete.
    """
    if not team_ids:
        return {}

    from modulo.db.models.connector_instance import ConnectorInstance
    from modulo.db.models.library_primitive import LibraryPrimitive
    from modulo.db.models.model_backend import ModelBackend
    from modulo.db.models.pipeline import Pipeline

    counts: dict[uuid.UUID, int] = {}
    for model_cls in (Pipeline, ConnectorInstance, ModelBackend, LibraryPrimitive):
        owner_col = model_cls.__table__.c.owner_team_id
        rows = (
            await session.execute(select(owner_col, func.count()).where(owner_col.in_(team_ids)).group_by(owner_col))
        ).all()
        for team_id, cnt in rows:
            if team_id is None:
                continue
            counts[team_id] = counts.get(team_id, 0) + int(cnt or 0)
    return counts
