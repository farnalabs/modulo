"""Org-scoped CRUD for the RunnerProbeCache table (FAR-591, D5).

The per-machine health-probe system cron upserts one row per
(organisation, machine); the Runners page + node editor read the cache.
Staleness is a PURE computation (``probe_is_stale``) so the
"status unknown (last checked Xs ago)" strip state is unit-testable
without a database: a dead probe can never leave "healthy" on screen.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.runner_probe_cache import RunnerProbeCache

#: The per-machine system-cron probe cadence in seconds (60s per ADR 029).
PROBE_INTERVAL_SECONDS = 60

#: A cached result older than 2x the interval renders as
#: "status unknown (last checked Xs ago)" — a dead probe never reads healthy.
PROBE_STALENESS_THRESHOLD_SECONDS = 2 * PROBE_INTERVAL_SECONDS


def probe_age_seconds(probed_at: datetime, now: datetime) -> int:
    """Age of a cached probe result in whole seconds (never negative)."""
    age = (now - probed_at).total_seconds()
    return max(0, int(age))


def probe_is_stale(probed_at: datetime, now: datetime) -> bool:
    """True when the cached result exceeds the staleness threshold (2x interval)."""
    return probe_age_seconds(probed_at, now) > PROBE_STALENESS_THRESHOLD_SECONDS


async def upsert_runner_probe_cache(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    machine_id: str,
    engine_reachable: bool,
    images_present: bool | None,
    image_checks: dict[str, Any] | None = None,
    engine_info: dict[str, Any] | None = None,
    probe_error: str | None = None,
    probed_at: datetime | None = None,
) -> RunnerProbeCache:
    """Insert or refresh the (org, machine) probe row (the cron is the sole writer)."""
    now = probed_at or datetime.now(UTC)
    result = await session.execute(
        select(RunnerProbeCache).where(
            RunnerProbeCache.organisation_id == org_id,
            RunnerProbeCache.machine_id == machine_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = RunnerProbeCache(
            organisation_id=org_id,
            machine_id=machine_id,
        )
        session.add(row)
    row.engine_reachable = engine_reachable
    row.images_present = images_present
    row.image_checks_json = image_checks or {}
    row.engine_info_json = engine_info or {}
    row.probe_error = probe_error
    row.probed_at = now
    await session.flush()
    return row


async def get_runner_probe_cache(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    machine_id: str,
) -> RunnerProbeCache | None:
    result = await session.execute(
        select(RunnerProbeCache).where(
            RunnerProbeCache.organisation_id == org_id,
            RunnerProbeCache.machine_id == machine_id,
        )
    )
    return result.scalar_one_or_none()


async def list_runner_probe_cache(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
) -> list[RunnerProbeCache]:
    result = await session.execute(
        select(RunnerProbeCache).where(RunnerProbeCache.organisation_id == org_id).order_by(RunnerProbeCache.machine_id)
    )
    return list(result.scalars().all())


async def list_orgs_with_runner_profiles(session: AsyncSession) -> list[uuid.UUID]:
    """Distinct orgs holding at least one non-deleted runner_docker profile.

    The probe sweeps ONLY these orgs — an org with no Bundled Runner profile
    never gets a probe row (and its strip renders "not enabled on this
    deployment").
    """
    from sqlalchemy import distinct

    from modulo.db.models.environment_profile import EnvironmentProfile

    result = await session.execute(
        select(distinct(EnvironmentProfile.organisation_id)).where(
            EnvironmentProfile.provider_type == "runner_docker",
            EnvironmentProfile.deleted_at.is_(None),
        )
    )
    return [row[0] for row in result.all()]


async def list_org_image_refs(session: AsyncSession, org_id: uuid.UUID) -> list[str]:
    """Distinct pinned image refs among the org's non-deleted runner_docker profiles."""
    from modulo.db.models.environment_profile import EnvironmentProfile

    result = await session.execute(
        select(EnvironmentProfile.image_ref).where(
            EnvironmentProfile.organisation_id == org_id,
            EnvironmentProfile.provider_type == "runner_docker",
            EnvironmentProfile.deleted_at.is_(None),
            EnvironmentProfile.image_ref.is_not(None),
        )
    )
    return sorted({row[0] for row in result.all() if row[0]})
