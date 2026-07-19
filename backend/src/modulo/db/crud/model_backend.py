"""Org-scoped CRUD for ModelBackend.

All functions require RLS org context to be set by the caller.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.model_backend import ModelBackend


async def create_model_backend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    display_name: str,
    provider: str,
    model_id: str,
    credentials_ciphertext: bytes,
    account_id: uuid.UUID,
    default_params: dict[str, Any] | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
    fallback_backend_ids: list[str] | None = None,
    tier: str = "native",
) -> ModelBackend:
    mb = ModelBackend(
        organisation_id=org_id,
        name=name,
        display_name=display_name,
        provider=provider,
        model_id=model_id,
        credentials_ciphertext=credentials_ciphertext,
        account_id=account_id,
        default_params=default_params or {},
        visibility=visibility,
        owner_team_id=owner_team_id,
        fallback_backend_ids=fallback_backend_ids,
        tier=tier,
    )
    session.add(mb)
    await session.flush()
    return mb


async def get_model_backend(session: AsyncSession, model_backend_id: uuid.UUID) -> ModelBackend | None:
    result = await session.execute(select(ModelBackend).where(ModelBackend.id == model_backend_id))
    return result.scalar_one_or_none()


async def list_model_backends(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    excluded_tiers: list[str] | None = None,
) -> PageResult[ModelBackend]:
    if excluded_tiers is None:
        excluded_tiers = ["in_dev"]
    offset = (page - 1) * page_size
    try:
        total_query = select(func.count()).select_from(ModelBackend).where(ModelBackend.organisation_id == org_id)
        if excluded_tiers:
            total_query = total_query.where(~ModelBackend.tier.in_(excluded_tiers))
        total = (await session.execute(total_query)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    try:
        items_stmt = (
            select(ModelBackend)
            .where(ModelBackend.organisation_id == org_id)
            .order_by(ModelBackend.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        if excluded_tiers:
            items_stmt = items_stmt.where(~ModelBackend.tier.in_(excluded_tiers))
        items = list((await session.execute(items_stmt)).scalars())
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_model_backend(
    session: AsyncSession,
    model_backend_id: uuid.UUID,
    updates: dict[str, Any],
) -> ModelBackend | None:
    mb = await get_model_backend(session, model_backend_id)
    if mb is None:
        return None
    apply_updates(mb, updates)
    await session.flush()
    return mb


async def delete_model_backend(session: AsyncSession, model_backend_id: uuid.UUID) -> bool:
    mb = await get_model_backend(session, model_backend_id)
    if mb is None:
        return False
    await session.delete(mb)
    await session.flush()
    return True
