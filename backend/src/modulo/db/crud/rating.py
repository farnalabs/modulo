"""Org-scoped CRUD for PrimitiveRating — with validation guards."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.primitive_abuse_report import PrimitiveAbuseReport
from modulo.db.models.primitive_rating import PrimitiveRating

# ---------------------------------------------------------------------------
# Domain exceptions (decoupled from web framework)
# ---------------------------------------------------------------------------


class SelfRatingError(Exception):
    """Raised when a user tries to rate their own primitive."""


class RatingCooldownError(Exception):
    """Raised when a user tries to rate too soon after their last rating."""


class CopyToAdaptError(Exception):
    """Raised when a user rates a primitive they have not copied."""


# ---------------------------------------------------------------------------
# Validation guards
# ---------------------------------------------------------------------------

_COOLDOWN_MINUTES = 10


async def _guard_self_rating(session: AsyncSession, primitive_id: uuid.UUID, account_id: uuid.UUID | None) -> None:
    """Block self-rating: a user cannot rate their own primitive."""
    if account_id is None:
        return
    stmt = select(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id).limit(1)
    result = await session.execute(stmt)
    prim = result.scalar_one_or_none()
    if prim is not None and prim.account_id == account_id:
        raise SelfRatingError("You cannot rate your own primitive")


async def _guard_cooldown(
    session: AsyncSession, primitive_id: uuid.UUID, org_id: uuid.UUID, account_id: uuid.UUID | None
) -> None:
    """Enforce 10-min cooldown between ratings from the same user on the same primitive."""
    if account_id is None:
        return
    cutoff = datetime.now(UTC) - timedelta(minutes=_COOLDOWN_MINUTES)
    stmt = (
        select(func.count())
        .select_from(PrimitiveRating)
        .where(
            PrimitiveRating.organisation_id == org_id,
            PrimitiveRating.primitive_id == primitive_id,
            PrimitiveRating.account_id == account_id,
            PrimitiveRating.created_at > cutoff,
        )
    )
    recent_count = (await session.execute(stmt)).scalar_one()
    if recent_count > 0:
        raise RatingCooldownError(f"Please wait {_COOLDOWN_MINUTES} minutes before rating this primitive again")


async def _guard_copy_to_adapt(
    session: AsyncSession, primitive_id: uuid.UUID, org_id: uuid.UUID, account_id: uuid.UUID | None
) -> None:
    """Require that the user has copied the primitive before rating it."""
    if account_id is None:
        return
    stmt = (
        select(func.count())
        .select_from(LibraryPrimitive)
        .where(
            LibraryPrimitive.organisation_id == org_id,
            LibraryPrimitive.forked_from == primitive_id,
            LibraryPrimitive.account_id == account_id,
        )
    )
    copy_count = (await session.execute(stmt)).scalar_one()
    if copy_count == 0:
        raise CopyToAdaptError("You must copy this primitive before rating it")


# ---------------------------------------------------------------------------
# Rating CRUD
# ---------------------------------------------------------------------------


async def submit_rating(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    thumbs_up: bool,
    comment: str | None = None,
    account_id: uuid.UUID | None = None,
) -> PrimitiveRating:
    """Submit a rating with validation guards (self-rating, cooldown, copy-to-adapt)."""
    # Run validation guards in order.
    await _guard_self_rating(session, primitive_id, account_id)
    await _guard_cooldown(session, primitive_id, org_id, account_id)
    await _guard_copy_to_adapt(session, primitive_id, org_id, account_id)

    rating = PrimitiveRating(
        organisation_id=org_id,
        primitive_id=primitive_id,
        account_id=account_id,
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
    """Return (average_rating, total_count) for a primitive.

    Average is computed as thumbs_up_ratio * 5, yielding a 1-5 weighted score.
    """
    count_stmt = select(func.count()).select_from(PrimitiveRating).where(PrimitiveRating.primitive_id == primitive_id)
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
    """Recompute and persist the aggregated rating on the LibraryPrimitive row."""
    avg, count = await get_rating_aggregate(session, primitive_id)
    stmt = select(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id)
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
    """List ratings for a primitive, newest first."""
    offset = (page - 1) * page_size
    count_stmt = select(func.count()).select_from(PrimitiveRating).where(PrimitiveRating.primitive_id == primitive_id)
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


# ---------------------------------------------------------------------------
# Abuse report CRUD
# ---------------------------------------------------------------------------


async def submit_abuse_report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    rating_id: uuid.UUID | None = None,
    reporter_account_id: uuid.UUID | None = None,
    reason: str,
) -> PrimitiveAbuseReport:
    """Submit an abuse report against a rating."""
    report = PrimitiveAbuseReport(
        organisation_id=org_id,
        primitive_id=primitive_id,
        rating_id=rating_id,
        reporter_account_id=reporter_account_id,
        reason=reason,
        status="pending",
    )
    session.add(report)
    await session.flush()
    return report


async def list_abuse_reports(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    status_filter: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[PrimitiveAbuseReport]:
    """List abuse reports (admin)."""
    offset = (page - 1) * page_size
    conditions = [PrimitiveAbuseReport.organisation_id == org_id]
    if status_filter:
        conditions.append(PrimitiveAbuseReport.status == status_filter)

    count_stmt = select(func.count()).select_from(PrimitiveAbuseReport).where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(PrimitiveAbuseReport)
        .where(*conditions)
        .order_by(PrimitiveAbuseReport.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    items = list(result.scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def review_abuse_report(
    session: AsyncSession,
    report_id: uuid.UUID,
    *,
    new_status: str,
    reviewer_account_id: uuid.UUID | None = None,
) -> PrimitiveAbuseReport | None:
    """Update the status of an abuse report (admin)."""
    stmt = select(PrimitiveAbuseReport).where(PrimitiveAbuseReport.id == report_id)
    result = await session.execute(stmt)
    report = result.scalar_one_or_none()
    if report is not None:
        report.status = new_status
        report.reviewer_account_id = reviewer_account_id
        report.reviewed_at = datetime.now(UTC)
        await session.flush()
    return report
