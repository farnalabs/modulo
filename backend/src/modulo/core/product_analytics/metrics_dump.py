"""Metrics dump -- product analytics SAQ cron job.

The SAQ cron ticks every 10 minutes; a per-instance jitter offset (aligned to
the 10-minute grid, spread across a 6-hour window) opens a 10-minute execution
window so each instance actually performs its (daily) dump on exactly one tick.
Builds an aggregate payload from all consenting orgs and POSTs it to the vendor
endpoint.  Watermark advances only on full success.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.cost_controller.system_config import (
    acquire_kv_lock,
    read_system_config,
    write_system_config,
)
from modulo.core.product_analytics.vendor_client import VendorClient

_log = logging.getLogger(__name__)

__all__ = ["metrics_dump"]

# Schema version -- bumped when the payload shape changes.
SCHEMA_VERSION: int = 1

# Watermark key in system_config.
_WATERMARK_KEY = "product_analytics_last_dumped_date"

# Backfill cap (design doc section 8).
_BACKFILL_MAX_DAYS = 14

# Jitter constants — each instance picks a random offset within a 6-hour
# window so that multiple instances don't all dump at the same minute.
_DUMP_WINDOW_MINUTES = 360  # 6 hours
_DUMP_EXECUTION_WINDOW_MINUTES = 10  # each instance gets 10 min to complete
_OFFSET_KEY = "product_analytics_dump_offset_minutes"


async def _get_or_create_system_config(factory: Any, key: str, create: Any) -> str:
    """Get-or-create a string-valued ``system_config`` entry.

    Shared by the jitter offset and the instance id: read inside a transaction;
    if absent, generate via ``create`` (a zero-arg callable returning ``str``)
    and persist.  Returns the stored value as a string.
    """
    async with factory() as session, session.begin():
        value = await read_system_config(session, key)
        if value is None:
            value = create()
            await write_system_config(session, key, value)
    return str(value)


async def _should_dump_now(factory: Any, now: datetime | None = None) -> bool:
    """Check if it's this instance's turn to dump based on stored jitter offset.

    The SAQ cron ticks every ``_DUMP_EXECUTION_WINDOW_MINUTES`` (10 min), so the
    instance must pick an offset *aligned* to that grid within the
    ``_DUMP_WINDOW_MINUTES`` (6h) window.  Because the offset is a multiple of
    the tick interval, exactly one cron fire per day lands inside the
    ``[offset, offset + 10)`` execution window and performs the dump.  A legacy
    unaligned offset (drawn before this alignment) is realigned on first read so
    its window is centred on a real tick.

    ``now`` is injectable for testing; it defaults to ``datetime.now(UTC)``.
    """
    import secrets as _secrets

    if now is None:
        now = datetime.now(UTC)
    current_minute_of_day = now.hour * 60 + now.minute

    slots = _DUMP_WINDOW_MINUTES // _DUMP_EXECUTION_WINDOW_MINUTES

    def _create_offset() -> str:
        value = str(_secrets.randbelow(slots) * _DUMP_EXECUTION_WINDOW_MINUTES)
        _log.info("product_analytics.jitter_offset_set", extra={"offset_minutes": int(value)})
        return value

    offset = int(await _get_or_create_system_config(factory, _OFFSET_KEY, _create_offset))

    # Realign a legacy unaligned offset so the window sits on a real tick.
    if offset % _DUMP_EXECUTION_WINDOW_MINUTES != 0:
        offset = (offset // _DUMP_EXECUTION_WINDOW_MINUTES) * _DUMP_EXECUTION_WINDOW_MINUTES
        async with factory() as session, session.begin():
            await write_system_config(session, _OFFSET_KEY, str(offset))

    return offset <= current_minute_of_day < offset + _DUMP_EXECUTION_WINDOW_MINUTES


async def metrics_dump(_ctx: dict[str, Any]) -> dict[str, Any]:
    """SAQ system-cron job -- daily metrics dump.

    The SAQ cron ticks every 10 minutes (``*/10 * * * *``); the per-instance
    jitter gate (``_should_dump_now``) opens a 10-minute execution window so the
    dump runs on exactly one tick per day, spread across a 6-hour window.
    unique=True, system session factory.
    Skips when: no consenting orgs or instance switch off.
    """
    from modulo.core.saq_worker import _make_system_session_factory
    from modulo.settings import get_settings

    settings = get_settings()
    # System session factory (modulo_system role, LOGIN, BYPASSRLS) — REQUIRED.
    # _build_payload reads TEAM-SCOPED tables (pipelines, model_backends,
    # connector_instances, environment_profiles, library_primitives) across ALL
    # consenting orgs with NO set_rls_org context. Under the modulo_app role
    # (NOBYPASSRLS) the strict rls_org_isolation policy filters those reads to
    # the (empty) app.organisation_id, silently returning ZERO rows. The system
    # factory bypasses RLS so the multi-org aggregation sees every row. Do NOT
    # swap to _make_session_factory.
    factory = _make_system_session_factory()

    # Jitter gate — each instance has a random offset within a 6-hour window.
    if not await _should_dump_now(factory):
        _log.debug("product_analytics.jitter_skip")
        return {"skipped": "jitter_skip"}

    # Instance-level gate (design doc section 5).
    instance_enabled = await _check_instance_switch(factory)
    if not instance_enabled:
        _log.info("product_analytics.instance_switch_off")
        return {"skipped": "instance_switch_off"}

    # Find consenting orgs.
    async with factory() as session, session.begin():
        orgs = await _get_consenting_orgs(session)

    if not orgs:
        _log.info("product_analytics.no_consenting_orgs")
        return {"skipped": "no_consenting_orgs"}

    # Determine dump window.
    dump_date = datetime.now(UTC).date()
    async with factory() as session, session.begin():
        last_dumped = await read_system_config(session, _WATERMARK_KEY)

    if last_dumped is not None:
        if isinstance(last_dumped, str):
            last_dumped = date.fromisoformat(last_dumped)
        start_date = last_dumped + timedelta(days=1)
    else:
        earliest_consent = min(
            (o["level_changed_at"] for o in orgs if o["level_changed_at"] is not None),
            default=dump_date,
        )
        if isinstance(earliest_consent, str):
            earliest_consent = date.fromisoformat(earliest_consent)
        backfill_start = max(earliest_consent, dump_date - timedelta(days=_BACKFILL_MAX_DAYS))
        start_date = backfill_start

    if start_date > dump_date:
        _log.info("product_analytics.up_to_date", extra={"last_dumped": str(last_dumped)})
        return {"skipped": "up_to_date", "last_dumped": str(last_dumped)}

    # Build and send per-day payloads.
    endpoint_url = settings.product_analytics_endpoint_url
    instance_secret = settings.product_analytics_instance_secret
    if not endpoint_url or not instance_secret:
        _log.warning("product_analytics.missing_vendor_config")
        return {"skipped": "missing_vendor_config"}

    client = VendorClient(endpoint_url, instance_secret)
    succeeded_dates: list[date] = []
    try:
        current_date = start_date
        while current_date <= dump_date:
            eligible_orgs = [o for o in orgs if o["level_changed_at"] is None or o["level_changed_at"] <= current_date]
            if not eligible_orgs:
                current_date += timedelta(days=1)
                continue

            payload = await _build_payload(factory, eligible_orgs, current_date)
            payload_bytes = json.dumps(payload, default=str, separators=(",", ":")).encode()
            timestamp = time.time()
            sequence = int(current_date.strftime("%Y%m%d"))

            success, status_code, error = await client.post_batch(payload_bytes, timestamp, sequence)
            if success:
                succeeded_dates.append(current_date)
                _log.info(
                    "product_analytics.dump_success",
                    extra={"date": str(current_date), "org_count": len(eligible_orgs)},
                )
            else:
                _log.warning(
                    "product_analytics.dump_failed",
                    extra={"date": str(current_date), "status_code": status_code, "error": error},
                )
                break

            current_date += timedelta(days=1)
    finally:
        await client.close()

    # Advance watermark (design doc section 8).
    if succeeded_dates:
        new_watermark = max(succeeded_dates)
        async with factory() as session, session.begin():
            await acquire_kv_lock(session, _WATERMARK_KEY)
            await write_system_config(session, _WATERMARK_KEY, new_watermark.isoformat())

    return {
        "dumped_dates": [str(d) for d in succeeded_dates],
        "org_count": len(orgs),
    }


async def _check_instance_switch(factory: Any) -> bool:
    async with factory() as session, session.begin():
        enabled = await read_system_config(session, "product_analytics_enabled")
    return bool(enabled)


async def _get_consenting_orgs(session: AsyncSession) -> list[dict[str, Any]]:
    from modulo.db.models.organisation import Organisation

    result = await session.execute(
        select(Organisation.id, Organisation.settings_json).where(Organisation.status == "active")
    )
    orgs: list[dict[str, Any]] = []
    for row in result:
        settings = row.settings_json or {}
        pa = settings.get("product_analytics", {})
        if pa.get("level") == "all":
            level_changed_at = pa.get("level_changed_at")
            if isinstance(level_changed_at, str):
                level_changed_at = date.fromisoformat(level_changed_at)
            orgs.append({"id": row.id, "level_changed_at": level_changed_at})
    return orgs


async def _build_payload(
    factory: Any,
    orgs: list[dict[str, Any]],
    target_date: date,
) -> dict[str, Any]:
    org_ids = [o["id"] for o in orgs]

    async with factory() as session, session.begin():
        entity_counts = await _count_entities(session, org_ids)
        run_stats = await _aggregate_run_stats(session, org_ids, target_date)
        error_stats = await _aggregate_error_stats(session, org_ids, target_date)
        integration_inventory = await _build_integration_inventory(session, org_ids)

    return {
        "schema_version": SCHEMA_VERSION,
        "date": target_date.isoformat(),
        "timestamp": datetime.now(UTC).isoformat(),
        "instance_id": await _get_or_create_instance_id(factory),
        "org_count": len(orgs),
        "entity_counts": entity_counts,
        "run_stats": run_stats,
        "error_stats": error_stats,
        "integration_inventory": integration_inventory,
        "instance_metadata": await _build_instance_metadata(factory),
    }


async def _count_entities(session: AsyncSession, org_ids: list[uuid.UUID]) -> dict[str, int]:
    from modulo.db.models.agent import Agent
    from modulo.db.models.connector_instance import ConnectorInstance
    from modulo.db.models.environment_profile import EnvironmentProfile
    from modulo.db.models.eval_definition import EvalDefinition
    from modulo.db.models.model_backend import ModelBackend
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.schema import Schema
    from modulo.db.models.team import Team
    from modulo.db.models.trigger import Trigger

    counts: dict[str, int] = {}

    result = await session.execute(
        select(func.count())
        .select_from(Pipeline)
        .where(Pipeline.organisation_id.in_(org_ids), Pipeline.deleted_at.is_(None))
    )
    counts["pipelines"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(Agent).where(Agent.organisation_id.in_(org_ids)))
    counts["agents"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(Schema).where(Schema.organisation_id.in_(org_ids)))
    counts["schemas"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(Team).where(Team.organisation_id.in_(org_ids)))
    counts["teams"] = result.scalar_one() or 0

    result = await session.execute(
        select(func.count()).select_from(ModelBackend).where(ModelBackend.organisation_id.in_(org_ids))
    )
    counts["model_backends"] = result.scalar_one() or 0

    result = await session.execute(
        select(func.count()).select_from(ConnectorInstance).where(ConnectorInstance.organisation_id.in_(org_ids))
    )
    counts["connector_instances"] = result.scalar_one() or 0

    result = await session.execute(
        select(func.count()).select_from(Trigger).where(Trigger.organisation_id.in_(org_ids))
    )
    counts["triggers"] = result.scalar_one() or 0

    result = await session.execute(
        select(func.count()).select_from(EvalDefinition).where(EvalDefinition.organisation_id.in_(org_ids))
    )
    counts["eval_definitions"] = result.scalar_one() or 0

    result = await session.execute(
        select(func.count()).select_from(EnvironmentProfile).where(EnvironmentProfile.organisation_id.in_(org_ids))
    )
    counts["environment_profiles"] = result.scalar_one() or 0

    counts["orgs"] = len(org_ids)

    return counts


async def _aggregate_run_stats(
    session: AsyncSession,
    org_ids: list[uuid.UUID],
    target_date: date,
) -> dict[str, Any]:
    from modulo.db.models.run import TERMINAL_STATUSES
    from modulo.db.models.run_daily_facts import RunDailyFact

    # "complete" is the success signal; per the raw-status-complete guard
    # (agent-failure UX design §15.2) it must not be treated as a bare
    # status comparison. Derive it as the complement of the other terminal
    # statuses (single source of truth: modulo.db.models.run.TERMINAL_STATUSES)
    # rather than matching the success literal directly.
    _non_complete_terminal = tuple(TERMINAL_STATUSES - {"complete"})

    result = await session.execute(
        select(
            func.count().label("total_runs"),
            func.sum(sa.case((RunDailyFact.status.in_(_non_complete_terminal), 0), else_=1)).label("complete"),
            func.sum(sa.case((RunDailyFact.status == "failed", 1), else_=0)).label("failed"),
            func.sum(sa.case((RunDailyFact.status == "cancelled", 1), else_=0)).label("cancelled"),
            func.sum(sa.case((RunDailyFact.status == "stalled", 1), else_=0)).label("stalled"),
            func.coalesce(func.sum(RunDailyFact.total_cost_usd), 0).label("total_cost_usd"),
            func.coalesce(func.sum(RunDailyFact.total_tokens), 0).label("total_tokens"),
        ).where(
            RunDailyFact.organisation_id.in_(org_ids),
            RunDailyFact.run_date == target_date,
        )
    )
    row = result.one()
    return {
        "total_runs": row.total_runs or 0,
        "complete": row.complete or 0,
        "failed": row.failed or 0,
        "cancelled": row.cancelled or 0,
        "stalled": row.stalled or 0,
        "total_cost_usd": str(row.total_cost_usd),
        "total_tokens": row.total_tokens or 0,
    }


async def _aggregate_error_stats(
    session: AsyncSession,
    org_ids: list[uuid.UUID],
    target_date: date,
) -> dict[str, Any]:
    from modulo.db.models.error_group import ErrorGroup

    result = await session.execute(
        select(
            ErrorGroup.level_peak,
            func.count().label("count"),
        )
        .where(
            ErrorGroup.organisation_id.in_(org_ids),
            sa.cast(ErrorGroup.last_seen, sa.Date) == target_date,
        )
        .group_by(ErrorGroup.level_peak)
    )
    by_level: dict[str, int] = {}
    total = 0
    for row in result:
        count = int(row._mapping["count"] or 0)
        by_level[row.level_peak] = count
        total += count
    return {"total": total, "by_level": by_level}


async def _build_integration_inventory(session: AsyncSession, org_ids: list[uuid.UUID]) -> dict[str, Any]:
    from modulo.db.models.connector_instance import ConnectorInstance
    from modulo.db.models.model_backend import ModelBackend
    from modulo.db.models.trigger import Trigger

    result = await session.execute(
        select(ModelBackend.provider, func.count().label("count"))
        .where(ModelBackend.organisation_id.in_(org_ids))
        .group_by(ModelBackend.provider)
    )
    model_providers: dict[str, int] = {row.provider: int(row._mapping["count"] or 0) for row in result}

    result = await session.execute(
        select(ConnectorInstance.connector_type_id, func.count().label("count"))
        .where(ConnectorInstance.organisation_id.in_(org_ids))
        .group_by(ConnectorInstance.connector_type_id)
    )
    connector_types: dict[str, int] = {str(row.connector_type_id): int(row._mapping["count"] or 0) for row in result}

    result = await session.execute(
        select(Trigger.trigger_type, func.count().label("count"))
        .where(Trigger.organisation_id.in_(org_ids))
        .group_by(Trigger.trigger_type)
    )
    trigger_types: dict[str, int] = {row.trigger_type: int(row._mapping["count"] or 0) for row in result}

    return {
        "model_providers": model_providers,
        "connector_types": connector_types,
        "trigger_types": trigger_types,
    }


async def _get_or_create_instance_id(factory: Any) -> str:
    key = "product_analytics_instance_id"

    def _create_id() -> str:
        return str(uuid.uuid4())

    return await _get_or_create_system_config(factory, key, _create_id)


async def _build_instance_metadata(_factory: Any) -> dict[str, Any]:
    import os

    from modulo.version import get_version

    return {
        "version": get_version(),
        "git_sha": os.environ.get("GIT_SHA", ""),
        "deployment_mode": os.environ.get("MODULO_DEPLOYMENT_MODE", "self-hosted"),
        "schema_version": SCHEMA_VERSION,
    }
