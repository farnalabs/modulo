"""CRUD for TierCatalog and FeatureFlagCatalog."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.tier_catalog import FeatureFlagCatalog, TierCatalog


async def get_tier(tier_id: str, session: AsyncSession) -> dict[str, Any] | None:
    result = await session.execute(
        select(TierCatalog).where(TierCatalog.tier_id == tier_id)
    )
    tier = result.scalar_one_or_none()
    if tier is None:
        return None
    return {
        "tier_id": tier.tier_id,
        "label": tier.label,
        "rank": tier.rank,
        "requires_license": tier.requires_license,
        "description": tier.description,
    }


async def list_tiers(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(TierCatalog).order_by(TierCatalog.rank)
    )
    return [
        {
            "tier_id": t.tier_id,
            "label": t.label,
            "rank": t.rank,
            "requires_license": t.requires_license,
            "description": t.description,
        }
        for t in result.scalars().all()
    ]


async def get_feature_flag(name: str, session: AsyncSession) -> dict[str, Any] | None:
    result = await session.execute(
        select(FeatureFlagCatalog).where(FeatureFlagCatalog.name == name)
    )
    flag = result.scalar_one_or_none()
    if flag is None:
        return None
    return {
        "name": flag.name,
        "description": flag.description,
        "tier_id": flag.tier_id,
        "depends_on": flag.depends_on,
        "is_active": flag.is_active,
    }


async def list_feature_flags(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(FeatureFlagCatalog).order_by(FeatureFlagCatalog.name)
    )
    return [
        {
            "name": f.name,
            "description": f.description,
            "tier_id": f.tier_id,
            "depends_on": f.depends_on,
            "is_active": f.is_active,
        }
        for f in result.scalars().all()
    ]


async def list_feature_flags_by_tier(tier_id: str, session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(FeatureFlagCatalog)
        .where(FeatureFlagCatalog.tier_id == tier_id)
        .order_by(FeatureFlagCatalog.name)
    )
    return [
        {
            "name": f.name,
            "description": f.description,
            "tier_id": f.tier_id,
            "depends_on": f.depends_on,
            "is_active": f.is_active,
        }
        for f in result.scalars().all()
    ]
