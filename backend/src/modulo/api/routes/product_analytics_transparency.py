"""Product analytics transparency endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

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


def _transparency_fields(
    last_dump_value: Any,
    dump_count_value: Any,
    consent_value: Any,
    instance_value: Any,
    enforcement_value: Any,
) -> tuple[str | None, int, str, bool, bool]:
    """Normalise the five raw system-config values into response fields.

    Coercion mirrors the original endpoint logic exactly: dump count is an
    int (falsy -> 0), consent a string (falsy -> "off"), the two enable
    flags bools (None -> False), and the last-dump timestamp a string
    (non-None non-str -> str()).
    """
    last_dump_at: str | None
    if isinstance(last_dump_value, str):
        last_dump_at = last_dump_value
    elif last_dump_value is not None:
        last_dump_at = str(last_dump_value)
    else:
        last_dump_at = None

    dump_count_total = int(dump_count_value) if dump_count_value else 0
    consent_level = str(consent_value) if consent_value else "off"
    instance_enabled = bool(instance_value) if instance_value is not None else False
    enforcement_enabled = bool(enforcement_value) if enforcement_value is not None else False
    return last_dump_at, dump_count_total, consent_level, instance_enabled, enforcement_enabled


def _stale_dump_warning(last_dump_at: str | None, consent_level: str) -> str | None:
    """The staleness warning when dumps stopped reaching farnalabs.

    Fires only when the last successful dump is older than
    *_STALE_WARNING_DAYS* AND consent is ``all``. An unparseable timestamp
    never warns (best-effort, fail-silent).
    """
    if not last_dump_at:
        return None
    try:
        last_dt = datetime.fromisoformat(last_dump_at)
        now = datetime.now(UTC)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        days_since = (now - last_dt).total_seconds() / 86400
        if days_since > _STALE_WARNING_DAYS and consent_level == "all":
            return "not_reaching_farnalabs"
    except (ValueError, TypeError):
        pass
    return None


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

    last_dump_at, dump_count_total, consent_level, instance_enabled, enforcement_enabled = _transparency_fields(
        last_dump_entry.value if last_dump_entry else None,
        dump_count_entry.value if dump_count_entry else 0,
        consent_entry.value if consent_entry else "off",
        instance_entry.value if instance_entry else False,
        enforcement_entry.value if enforcement_entry else False,
    )
    warning = _stale_dump_warning(last_dump_at, consent_level)

    return TransparencyResponse(
        last_successful_dump_at=last_dump_at,
        dump_count_total=dump_count_total,
        consent_level=consent_level,
        instance_enabled=instance_enabled,
        enforcement_enabled=enforcement_enabled,
        warning=warning,
    )
