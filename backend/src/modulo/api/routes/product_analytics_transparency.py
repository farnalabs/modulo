"""Product analytics transparency endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_system_permission
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.system_config import get_config

_CODE_PRODUCT_ANALYTICS_MANAGE = "system.config.manage"

_STALE_WARNING_DAYS = 3

router = APIRouter(
    prefix="/api/v1/product-analytics",
    tags=["product-analytics-transparency"],
)


class TransparencyResponse(BaseModel):
    last_successful_dump_at: str | None = None
    dump_count_total: int = 0
    consent_level: str = "off"
    instance_enabled: bool = False
    enforcement_enabled: bool = False
    warning: str | None = None


@router.get("/transparency")
@handle_db_errors("product_analytics.transparency")
async def get_transparency(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _current_user: AuthenticatedPrincipal = require_system_permission(_CODE_PRODUCT_ANALYTICS_MANAGE),  # type: ignore[assignment]
) -> TransparencyResponse:
    async with session.begin():
        last_dump_entry = await get_config(session, "product_analytics_last_dump_at")
        dump_count_entry = await get_config(session, "product_analytics_dump_count")
        consent_entry = await get_config(session, "product_analytics_consent_level")
        instance_entry = await get_config(session, "product_analytics_enabled")
        enforcement_entry = await get_config(session, "product_analytics_enforcement_enabled")

    last_dump_at_raw = last_dump_entry.value if last_dump_entry else None
    last_dump_at: str | None = None
    if isinstance(last_dump_at_raw, str):
        last_dump_at = last_dump_at_raw
    elif last_dump_at_raw is not None:
        last_dump_at = str(last_dump_at_raw)

    dump_count_raw = dump_count_entry.value if dump_count_entry else 0
    dump_count_total = int(dump_count_raw) if dump_count_raw else 0

    consent_level_raw = consent_entry.value if consent_entry else "off"
    consent_level = str(consent_level_raw) if consent_level_raw else "off"

    instance_enabled_raw = instance_entry.value if instance_entry else False
    instance_enabled = bool(instance_enabled_raw) if instance_enabled_raw is not None else False

    enforcement_enabled_raw = enforcement_entry.value if enforcement_entry else False
    enforcement_enabled = bool(enforcement_enabled_raw) if enforcement_enabled_raw is not None else False

    warning = None

    if last_dump_at:
        try:
            last_dt = datetime.fromisoformat(last_dump_at)
            now = datetime.now(UTC)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            days_since = (now - last_dt).total_seconds() / 86400
            if days_since > _STALE_WARNING_DAYS and consent_level == "all":
                warning = "not_reaching_farnalabs"
        except (ValueError, TypeError):
            pass

    return TransparencyResponse(
        last_successful_dump_at=last_dump_at,
        dump_count_total=dump_count_total,
        consent_level=consent_level,
        instance_enabled=instance_enabled,
        enforcement_enabled=enforcement_enabled,
        warning=warning,
    )
