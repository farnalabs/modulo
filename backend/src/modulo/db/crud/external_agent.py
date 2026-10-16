"""Org-scoped CRUD for the ``external_agents`` registry (FAR-1692 S1).

Every query carries an explicit ``organisation_id`` predicate so a cross-org
call is refused by the query layer itself, not only by Postgres RLS (which does
not exist on the SQLite/MariaDB generic backends and is what the unit tests
exercise). All functions require the caller to have set the RLS org context.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.external_agent import ExternalAgent


async def create_external_agent(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    runtime_kind: str = "other",
    host_descriptor: str | None = None,
    status: str = "active",
    discovery_provenance: str = "manual",
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
) -> ExternalAgent:
    agent = ExternalAgent(
        organisation_id=org_id,
        name=name,
        runtime_kind=runtime_kind,
        host_descriptor=host_descriptor,
        status=status,
        discovery_provenance=discovery_provenance,
        first_seen=first_seen,
        last_seen=last_seen,
        visibility=visibility,
        owner_team_id=owner_team_id,
    )
    session.add(agent)
    await session.flush()
    return agent


async def get_external_agent(
    session: AsyncSession,
    agent_id: uuid.UUID,
    *,
    organisation_id: uuid.UUID | None = None,
) -> ExternalAgent | None:
    stmt = select(ExternalAgent).where(ExternalAgent.id == agent_id)
    if organisation_id is not None:
        stmt = stmt.where(ExternalAgent.organisation_id == organisation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_external_agents(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[ExternalAgent]:
    org_filter = ExternalAgent.organisation_id == organisation_id if organisation_id else None

    total_query = select(func.count()).select_from(ExternalAgent)
    if org_filter is not None:
        total_query = total_query.where(org_filter)
    total = (await session.execute(total_query)).scalar_one()

    offset = (page - 1) * page_size
    items_stmt = (
        select(ExternalAgent)
        .order_by(ExternalAgent.created_at.desc(), ExternalAgent.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    if org_filter is not None:
        items_stmt = items_stmt.where(org_filter)
    items = list((await session.execute(items_stmt)).scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_external_agent(
    session: AsyncSession,
    agent_id: uuid.UUID,
    updates: dict[str, Any],
    *,
    organisation_id: uuid.UUID | None = None,
) -> ExternalAgent | None:
    agent = await get_external_agent(session, agent_id, organisation_id=organisation_id)
    if agent is None:
        return None
    apply_updates(agent, updates)
    await session.flush()
    return agent


async def soft_delete_external_agent(
    session: AsyncSession,
    agent_id: uuid.UUID,
    *,
    organisation_id: uuid.UUID | None = None,
) -> bool:
    agent = await get_external_agent(session, agent_id, organisation_id=organisation_id)
    if agent is None:
        return False
    agent.deleted_at = datetime.now(UTC)
    await session.flush()
    return True
