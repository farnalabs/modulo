"""Org-scoped CRUD for PrimitiveRating."""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.primitive_rating import PrimitiveRating


async def submit_rating(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    thumbs_up: bool,
    comment: str | None = None,
    user_id: uuid.UUID | None = None,
) -> PrimitiveRating:
    rating = PrimitiveRating(
        organisation_id=org_id,
        primitive_id=primitive_id,
        user_id=user_id,
        thumbs_up=thumbs_up,
        comment=comment,
    )
    session.add(rating)
    await session.flush()
    return rating


async def get_rating_aggregate(
    session: AsyncSession,
    primitive_id: uuid.UUID,
) -> tuple[Decimal | None, int]:
    count_stmt = (
        select(func.count())
        .select_from(PrimitiveRating)
        .where(PrimitiveRating.primitive_id == primitive_id)
    )
    total_count = (await session.execute(count_stmt)).scalar_one()

    if total_count == 0:
        return None, 0

    thumbs_up_stmt = (
        select(func.count())
        .select_from(PrimitiveRating)
        .where(
            PrimitiveRating.primitive_id == primitive_id,
            PrimitiveRating.thumbs_up.is_(True),
        )
    )
    thumbs_up_count = (await session.execute(thumbs_up_stmt)).scalar_one()
    ratio = Decimal(thumbs_up_count) / Decimal(total_count)
    avg = ratio * Decimal("5")
    return avg, total_count


async def update_primitive_ratings_aggregate(
    session: AsyncSession,
    primitive_id: uuid.UUID,
) -> None:
    avg, count = await get_rating_aggregate(session, primitive_id)
    stmt = (
        select(LibraryPrimitive)
        .where(LibraryPrimitive.id == primitive_id)
    )
    result = await session.execute(stmt)
    prim = result.scalar_one_or_none()
    if prim is not None:
        prim.average_rating = avg
        prim.review_count = count
        await session.flush()


async def list_ratings_for_primitive(
    session: AsyncSession,
    primitive_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[PrimitiveRating]:
    offset = (page - 1) * page_size
    count_stmt = (
        select(func.count())
        .select_from(PrimitiveRating)
        .where(PrimitiveRating.primitive_id == primitive_id)
    )
    total = (await session.execute(count_stmt)).scalar_one()
    stmt = (
        select(PrimitiveRating)
        .where(PrimitiveRating.primitive_id == primitive_id)
        .order_by(PrimitiveRating.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    items = list(result.scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)
