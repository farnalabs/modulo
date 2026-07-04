"""Org-scoped CRUD for ConnectorInstance.

All functions require RLS org context to be set by the caller.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.connector_instance import ConnectorInstance


async def create_connector_instance(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    connector_type_id: str,
    owner_id: uuid.UUID,
    credentials_ciphertext: bytes,
    config_json: dict[str, Any] | None = None,
    allowed_operations: list[str] | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
    tier: str = "native",
) -> ConnectorInstance:
    ci = ConnectorInstance(
        organisation_id=org_id,
        name=name,
        connector_type_id=connector_type_id,
        owner_id=owner_id,
        credentials_ciphertext=credentials_ciphertext,
        config_json=config_json or {},
        allowed_operations=allowed_operations or [],
        visibility=visibility,
        owner_team_id=owner_team_id,
        tier=tier,
    )
    session.add(ci)
    await session.flush()
    return ci


async def get_connector_instance(session: AsyncSession, connector_id: uuid.UUID) -> ConnectorInstance | None:
    result = await session.execute(select(ConnectorInstance).where(ConnectorInstance.id == connector_id))
    return result.scalar_one_or_none()


async def list_connector_instances(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
) -> PageResult[ConnectorInstance]:
    if cursor is not None:
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            select(ConnectorInstance),
            cursor=cursor,
            limit=page_size,
            model=ConnectorInstance,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=page,
            page_size=page_size,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    offset = (page - 1) * page_size
    total = (await session.execute(select(func.count()).select_from(ConnectorInstance))).scalar_one()
    items = list(
        (
            await session.execute(
                select(ConnectorInstance).order_by(ConnectorInstance.created_at.desc()).offset(offset).limit(page_size)
            )
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_connector_instance(
    session: AsyncSession,
    connector_id: uuid.UUID,
    updates: dict[str, Any],
) -> ConnectorInstance | None:
    ci = await get_connector_instance(session, connector_id)
    if ci is None:
        return None
    apply_updates(ci, updates)
    await session.flush()
    return ci


async def delete_connector_instance(session: AsyncSession, connector_id: uuid.UUID) -> bool:
    ci = await get_connector_instance(session, connector_id)
    if ci is None:
        return False
    await session.delete(ci)
    await session.flush()
    return True
