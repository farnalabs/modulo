"""Product analytics event ingest endpoint (FAR-355).

POST /api/v1/metrics/events — accepts a batch of curated product analytics
events from the frontend, stages them in ``metrics_staging`` for the daily
``metrics_dump`` cron to consume.

Design doc: Repos/admin/strategy/product-analytics-design.md §4, §8.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.product_analytics.metrics_constants import (
    API_ERROR_DAILY_CAP,
    MAX_BATCH_SIZE,
    VALID_EVENT_TYPES,
)
from modulo.db.crud.organisation import get_organisation
from modulo.db.models.metrics_staging import MetricsStaging
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/metrics",
    tags=["metrics"],
)


# ── Pydantic models ──────────────────────────────────────────────────────────

_EXPECTED_FIELDS = {"event_id", "event_type", "recorded_at", "payload"}


class MetricsEventItem(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: str = Field(..., min_length=1, max_length=64)
    recorded_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        if v not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown event type: {v}")
        return v


class MetricsEventBatchRequest(BaseModel):
    events: list[MetricsEventItem] = Field(..., min_length=1, max_length=MAX_BATCH_SIZE)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _registered_path_templates(app: Any) -> set[str]:
    """Collect every registered URL path template under *app*.

    FastAPI 0.130+ stores included routers lazily as ``_IncludedRouter``
    wrappers whose ``path`` attribute is ``None``, so a flat scan of
    ``app.routes`` can no longer see route templates. Walk the nested router
    tree so the sanitizer matches real templates instead of always returning
    ``"unknown"``.
    """
    templates: set[str] = set()

    def visit(routes: Any) -> None:
        for r in routes:
            original = getattr(r, "original_router", None)
            if original is not None:
                visit(getattr(original, "routes", ()))
                continue
            path = getattr(r, "path", None)
            if isinstance(path, str):
                templates.add(path)

    visit(getattr(app, "routes", ()))
    return templates


def _sanitize_route_template(route: str | None, templates: set[str]) -> str:
    """Match *route* against the app's registered route templates.

    Returns the matched template string, or ``"unknown"`` when no match is
    found.  This prevents raw paths / query strings from leaking into the
    staging table.
    """
    if not route:
        return "unknown"
    return route if route in templates else "unknown"


async def _api_error_count_today(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> int:
    """Return the number of ``api_error`` events staged for *org_id* today."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count())
        .select_from(MetricsStaging)
        .where(
            MetricsStaging.organisation_id == org_id,
            MetricsStaging.event_type == "api_error",
            MetricsStaging.recorded_at >= today_start,
        )
    )
    result = await session.execute(stmt)
    count = result.scalar_one()
    return count or 0


def _consent_active(settings_json: dict[str, Any] | None) -> bool:
    """Return True when the org has opted in to product analytics."""
    if not isinstance(settings_json, dict):
        return False
    pa = settings_json.get("product_analytics")
    if not isinstance(pa, dict):
        return False
    return pa.get("level") == "all"


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/events", status_code=status.HTTP_204_NO_CONTENT)
async def ingest_events(
    req: MetricsEventBatchRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: TenantPrincipal = require_permission("metrics.ingest"),
) -> None:
    """Ingest a batch of curated product analytics events.

    - Consent gate: 204 when the org's ``product_analytics.level`` is not ``all``.
    - Best-effort insert: individual row failures are logged and skipped so the
      client always receives a 2xx.
    - ``api_error`` events are capped at ``API_ERROR_DAILY_CAP`` per org per day.
    - ``UNIQUE(event_id)`` handles dedup — duplicate inserts are silently ignored.
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            org = await get_organisation(session, current_user.organisation_id)
            if org is None or not _consent_active(org.settings_json):
                return

            # Pre-check api_error cap
            api_error_count = await _api_error_count_today(session, current_user.organisation_id)

            now = datetime.now(UTC)
            registered_templates = _registered_path_templates(request.app)
            for event in req.events:
                # api_error daily cap
                if event.event_type == "api_error":
                    if api_error_count >= API_ERROR_DAILY_CAP:
                        _log.debug(
                            "api_error daily cap reached for org %s",
                            current_user.organisation_id,
                        )
                        continue
                    api_error_count += 1

                # Sanitize route in api_error payloads
                payload = dict(event.payload)
                if event.event_type == "api_error" and "route" in payload:
                    payload["route"] = _sanitize_route_template(payload["route"], registered_templates)

                recorded_at = event.recorded_at or now

                stmt = (
                    pg_insert(MetricsStaging)
                    .values(
                        id=uuid.uuid4(),
                        organisation_id=current_user.organisation_id,
                        event_id=event.event_id,
                        event_type=event.event_type,
                        payload=payload,
                        recorded_at=recorded_at,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["organisation_id", "event_id"],
                    )
                )
                try:
                    await session.execute(stmt)
                except IntegrityError:
                    # Duplicate event_id — silently skip
                    _log.debug("Duplicate event_id %s, skipping", event.event_id)
                except SQLAlchemyError:
                    _log.exception(
                        "Failed to stage event %s (org=%s)",
                        event.event_id,
                        current_user.organisation_id,
                    )
                    # Best-effort: log and continue, never block the client

    except HTTPException:
        raise
    except SQLAlchemyError:
        _log.exception("Failed to ingest metrics batch (org=%s)", current_user.organisation_id)
        # Best-effort: return 204 even on DB failure — never block the client
    except Exception:
        _log.exception("Unexpected error ingesting metrics batch (org=%s)", current_user.organisation_id)
        # Best-effort: return 204 even on unexpected failure
