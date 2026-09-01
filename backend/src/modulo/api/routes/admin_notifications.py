"""Admin notification webhook management — CRUD, test, re-enable, delivery log, retry."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func as sa_func
from sqlalchemy import select, update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import decode_stored_secret_scoped
from modulo.core.notifier import (
    EVENT_BUDGET_EXCEEDED,
    EVENT_CIRCUIT_BREAKER_TRIPPED,
    EVENT_RUN_STALLED,
    EVENT_TRIGGER_DEACTIVATED,
    endpoint_events_to_list,
)
from modulo.core.ssrf import pinned_async_client
from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings
from modulo.util import is_valid_http_url

_CODE_ADMIN_NOTIFICATION_MANAGE = "admin.notification.manage"
_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING = "notifications.delivery_table_missing"
_MSG_NOTIFICATION_DELIVERY_LOGGING_NOT = (
    "Notification delivery logging is not available. Run database migrations to enable it."
)
_CODE_NOTIFICATIONS_DB_ERROR = "notifications.db_error"
_MSG_DATABASE_ERROR_PLEASE_TRY = "Database error. Please try again later."
_CODE_NOTIFICATIONS_UNEXPECTED_ERROR = "notifications.unexpected_error"
_MSG_MODULO_NOTIFIER_1_0 = "Modulo-Notifier/1.0"
_MSG_APPLICATION_JSON = "application/json"
_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING = "notifications.endpoint_table_missing"
_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN = (
    "Notifications are not available. Run database migrations to enable this feature."
)
_MSG_WEBHOOK_NOT_FOUND = "Webhook not found"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/notifications", tags=["admin-notifications"])

AVAILABLE_EVENTS = [
    "hitl_awaiting",
    "run_failed",
    EVENT_RUN_STALLED,
    "claim_expired",
    "hitl_overdue",
    EVENT_BUDGET_EXCEEDED,
    EVENT_CIRCUIT_BREAKER_TRIPPED,
    EVENT_TRIGGER_DEACTIVATED,
]


# ── Request / Response models ──────────────────────────────────────────


class WebhookCreate(BaseModel):
    url: str = Field(..., max_length=2048)
    secret: str | None = Field(None)
    events: list[str] = Field(default_factory=list)
    description: str | None = Field(None, max_length=500)
    team_id: uuid.UUID | None = Field(
        None, description="Optional team scope; when set, only that team's events hit this endpoint"
    )

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        if not is_valid_http_url(v):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("events")
    @classmethod
    def _events_must_be_valid(cls, v: list[str]) -> list[str]:
        invalid = [e for e in v if e not in AVAILABLE_EVENTS]
        if invalid:
            raise ValueError(f"Unknown event types: {invalid}")
        return v


class WebhookUpdate(BaseModel):
    url: str | None = Field(None, max_length=2048)
    secret: str | None = None
    events: list[str] | None = None
    description: str | None = Field(None, max_length=500)
    team_id: uuid.UUID | None = Field(
        None, description="Optional team scope; when set, only that team's events hit this endpoint"
    )

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str | None) -> str | None:
        if v is not None and not is_valid_http_url(v):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("events")
    @classmethod
    def _events_must_be_valid(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            invalid = [e for e in v if e not in AVAILABLE_EVENTS]
            if invalid:
                raise ValueError(f"Unknown event types: {invalid}")
        return v


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    description: str | None
    has_secret: bool
    is_active: bool
    consecutive_dead_letter_count: int
    team_id: str | None = None
    disabled_at: str | None
    created_at: str


class DeliveryLogEntry(BaseModel):
    id: str
    event_type: str
    status: str
    attempt_count: int
    response_code: int | None
    last_error: str | None
    response_body: str | None = None
    endpoint_url: str | None = None
    endpoint_id: str | None = None
    created_at: str


class DeliveryLogResponse(BaseModel):
    items: list[DeliveryLogEntry]
    next_cursor: str | None
    total: int


class TestResult(BaseModel):
    success: bool
    status_code: int | None
    response_body: str | None
    error: str | None


# ── Non-webhook-scoped routes (MUST precede {webhook_id} routes) ────────


@router.get("/deliveries")
@handle_db_errors("admin.notifications.list_all_deliveries")
async def list_all_deliveries(
    cursor: str | None = Query(None, description="Cursor from previous response (ISO datetime)"),
    limit: int = Query(default=25, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    event_type_filter: str | None = Query(None, alias="event_type"),
    endpoint_id_filter: uuid.UUID | None = Query(None, alias="endpoint_id"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
) -> DeliveryLogResponse:
    try:
        return await _list_deliveries(
            cursor=cursor,
            limit=limit,
            status_filter=status_filter,
            event_type_filter=event_type_filter,
            endpoint_id_filter=endpoint_id_filter,
            date_from=date_from,
            date_to=date_to,
            session=session,
            principal=principal,
        )
    except ProgrammingError:
        logger.warning(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING, extra={"route": "list_all_deliveries"})
        logger.exception(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATION_DELIVERY_LOGGING_NOT,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "list_all_deliveries"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "list_all_deliveries"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


async def _list_deliveries(
    cursor: str | None,
    limit: int,
    status_filter: str | None,
    event_type_filter: str | None,
    endpoint_id_filter: uuid.UUID | None,
    date_from: str | None,
    date_to: str | None,
    session: AsyncSession,
    principal: TenantPrincipal,
) -> DeliveryLogResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)

        query = (
            select(
                NotificationDeliveryLog,
                NotificationEndpoint.url,
            )
            .outerjoin(
                NotificationEndpoint,
                NotificationDeliveryLog.endpoint_id == NotificationEndpoint.id,
            )
            .where(
                NotificationDeliveryLog.organisation_id == principal.organisation_id,
            )
        )

        if status_filter:
            query = query.where(NotificationDeliveryLog.status == status_filter)

        if event_type_filter:
            query = query.where(NotificationDeliveryLog.event_type == event_type_filter)

        if endpoint_id_filter:
            query = query.where(NotificationDeliveryLog.endpoint_id == endpoint_id_filter)

        if date_from:
            query = query.where(NotificationDeliveryLog.created_at >= _parse_delivery_date(date_from, "from date"))

        if date_to:
            query = query.where(NotificationDeliveryLog.created_at <= _parse_delivery_date(date_to, "to date"))

        if cursor:
            query = query.where(NotificationDeliveryLog.created_at < _parse_delivery_date(cursor, "cursor"))

        query = query.order_by(NotificationDeliveryLog.created_at.desc()).limit(limit + 1)

        rows = list((await session.execute(query)).all())

        total = await _count_deliveries(
            session,
            principal,
            status_filter=status_filter,
            event_type_filter=event_type_filter,
            endpoint_id_filter=endpoint_id_filter,
        )

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows and rows[-1][0].created_at:
        next_cursor = rows[-1][0].created_at.isoformat()

    items = [_delivery_entry(d) for d in rows]

    return DeliveryLogResponse(items=items, next_cursor=next_cursor, total=total)


def _parse_delivery_date(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid {field} format",
        ) from exc


def _delivery_entry(d: Any) -> DeliveryLogEntry:
    return DeliveryLogEntry(
        id=str(d[0].id),
        event_type=d[0].event_type,
        status=d[0].status,
        attempt_count=d[0].attempt_count,
        response_code=d[0].response_code,
        last_error=d[0].last_error,
        response_body=d[0].response_body,
        endpoint_url=d[1] or "",
        endpoint_id=str(d[0].endpoint_id) if d[0].endpoint_id else None,
        created_at=d[0].created_at.isoformat() if d[0].created_at else "",
    )


async def _count_deliveries(
    session: AsyncSession,
    principal: TenantPrincipal,
    *,
    status_filter: str | None,
    event_type_filter: str | None,
    endpoint_id_filter: uuid.UUID | None,
) -> int:
    count_query = select(sa_func.count(NotificationDeliveryLog.id)).where(
        NotificationDeliveryLog.organisation_id == principal.organisation_id,
    )
    if status_filter:
        count_query = count_query.where(NotificationDeliveryLog.status == status_filter)
    if event_type_filter:
        count_query = count_query.where(NotificationDeliveryLog.event_type == event_type_filter)
    if endpoint_id_filter:
        count_query = count_query.where(NotificationDeliveryLog.endpoint_id == endpoint_id_filter)
    count_result = await session.execute(count_query)
    return count_result.scalar() or 0


@router.post("/deliveries/retry-all-failed")
@handle_db_errors("admin.notifications.retry_all_failed_deliveries")
async def retry_all_failed_deliveries(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Retry all failed and dead_lettered deliveries across all webhooks in the org."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            failed = list(
                (
                    await session.execute(
                        select(NotificationDeliveryLog, NotificationEndpoint)
                        .join(
                            NotificationEndpoint,
                            NotificationDeliveryLog.endpoint_id == NotificationEndpoint.id,
                        )
                        .where(
                            NotificationDeliveryLog.organisation_id == principal.organisation_id,
                            NotificationDeliveryLog.status.in_(["failed", "dead_lettered"]),
                        )
                    )
                ).all()
            )
    except ProgrammingError:
        logger.warning(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING, extra={"route": "retry_all_failed_deliveries"})
        logger.exception(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATION_DELIVERY_LOGGING_NOT,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "retry_all_failed_deliveries"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "retry_all_failed_deliveries"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    retried = 0
    errors: list[str] = []

    for delivery, ep in failed:
        _resp, error = await _retry_one_delivery(session, principal, settings, delivery, ep)
        retried += 1
        if error:
            errors.append(error)

    return {"retried": retried, "errors": errors, "success": len(errors) == 0}


async def _retry_one_delivery(
    session: AsyncSession,
    principal: TenantPrincipal,
    settings: Settings,
    delivery: NotificationDeliveryLog,
    ep: NotificationEndpoint,
) -> tuple[httpx.Response | None, str | None]:
    """Retry one failed delivery and record its outcome.

    Returns ``(response, None)`` when the request reached the endpoint, or
    ``(None, error)`` on a transport failure (also recorded as a new failed
    delivery).
    """
    body = json.dumps(
        {
            "event": delivery.event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"event_type": delivery.event_type, "retry": True},
        }
    ).encode()

    headers = {"Content-Type": _MSG_APPLICATION_JSON, "User-Agent": _MSG_MODULO_NOTIFIER_1_0}
    if ep.secret_ciphertext:
        try:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                raw_secret = await decode_stored_secret_scoped(
                    session, ep.secret_ciphertext, settings.fernet_key, org_id=principal.organisation_id
                )
            sig = hmac.new(raw_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Modulo-Signature"] = f"sha256={sig}"
        except Exception:
            logger.exception("Failed to sign retry payload")

    try:
        client = await pinned_async_client(ep.url)
        client.timeout = httpx.Timeout(15.0)
    except ValueError as exc:
        logger.warning(
            "admin.notifications.retry_delivery.ssrf_rejected",
            extra={"endpoint_id": str(ep.id), "error": str(exc)},
        )
        await _record_delivery_error(session, principal, delivery, ep, exc)
        return None, str(exc)
    try:
        resp = await client.post(ep.url, content=body, headers=headers)
        await _record_delivery_result(session, principal, delivery, ep, resp)
        return resp, None
    except httpx.RequestError as exc:
        await _record_delivery_error(session, principal, delivery, ep, exc)
        return None, str(exc)
    finally:
        await client.aclose()


async def _record_delivery_result(
    session: AsyncSession,
    principal: TenantPrincipal,
    delivery: NotificationDeliveryLog,
    ep: NotificationEndpoint,
    resp: httpx.Response,
) -> None:
    """Record a delivery that reached the endpoint and update its counters."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            new_log = NotificationDeliveryLog(
                organisation_id=principal.organisation_id,
                event_type=delivery.event_type,
                endpoint_id=delivery.endpoint_id,
                status="delivered" if resp.is_success else "failed",
                attempt_count=delivery.attempt_count + 1,
                response_code=resp.status_code,
                response_body=resp.text[:500] if resp.is_success else None,
                last_error=(None if resp.is_success else f"HTTP {resp.status_code}: {resp.text[:200]}"),
            )
            session.add(new_log)

            if resp.is_success:
                await session.execute(
                    update(NotificationEndpoint)
                    .where(
                        NotificationEndpoint.id == ep.id,
                        NotificationEndpoint.consecutive_dead_letter_count > 0,
                    )
                    .values(consecutive_dead_letter_count=0)
                )
            else:
                await _bump_dead_letter_count(session, ep)
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING, extra={"route": "retry_all_failed_deliveries.record"}
        )
        logger.exception(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATION_DELIVERY_LOGGING_NOT,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "retry_all_failed_deliveries.record"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None


async def _record_delivery_error(
    session: AsyncSession,
    principal: TenantPrincipal,
    delivery: NotificationDeliveryLog,
    ep: NotificationEndpoint,
    exc: BaseException,
) -> None:
    """Record a transport-failed delivery and bump the endpoint dead-letter count."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            new_log = NotificationDeliveryLog(
                organisation_id=principal.organisation_id,
                event_type=delivery.event_type,
                endpoint_id=delivery.endpoint_id,
                status="failed",
                attempt_count=delivery.attempt_count + 1,
                response_code=None,
                response_body=None,
                last_error=str(exc),
            )
            session.add(new_log)
            await _bump_dead_letter_count(session, ep)
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING,
            extra={"route": "retry_all_failed_deliveries.error_record"},
        )
        logger.exception(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATION_DELIVERY_LOGGING_NOT,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "retry_all_failed_deliveries.error_record"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None


async def _bump_dead_letter_count(session: AsyncSession, ep: NotificationEndpoint) -> None:
    """Increment an endpoint's dead-letter count, auto-disabling it at the threshold."""
    result = await session.execute(
        update(NotificationEndpoint)
        .where(NotificationEndpoint.id == ep.id)
        .values(
            consecutive_dead_letter_count=(NotificationEndpoint.consecutive_dead_letter_count + 1),
        )
        .returning(NotificationEndpoint.consecutive_dead_letter_count)
    )
    new_count = result.scalar_one()
    if new_count >= 10:
        await session.execute(
            update(NotificationEndpoint)
            .where(NotificationEndpoint.id == ep.id)
            .values(auto_disabled=True, disabled_at=datetime.now(UTC))
        )


@router.get("/available-events")
@handle_db_errors("admin.notifications.list_available_events")
async def list_available_events(
    _principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
) -> list[str]:
    return AVAILABLE_EVENTS


# ── Webhook CRUD ────────────────────────────────────────────────────────


@router.get("")
@handle_db_errors("admin.notifications.list_webhooks")
async def list_webhooks(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
) -> list[WebhookResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(NotificationEndpoint)
                .where(NotificationEndpoint.organisation_id == principal.organisation_id)
                .order_by(NotificationEndpoint.created_at.desc())
            )
            endpoints = list(result.scalars())
    except ProgrammingError:
        logger.warning(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING, extra={"route": "list_webhooks"})
        logger.exception(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "list_webhooks"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "list_webhooks"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return [_ep_to_response(ep) for ep in endpoints]


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.notifications.create_webhook")
async def create_webhook(
    req: WebhookCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
    settings: Settings = Depends(get_settings),
) -> WebhookResponse:
    fernet = Fernet(settings.fernet_key.encode())
    secret_ciphertext: bytes | None = None
    if req.secret:
        secret_ciphertext = fernet.encrypt(req.secret.encode())

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            if req.team_id is not None:
                await _validate_team_exists(session, principal.organisation_id, req.team_id)
            ep = NotificationEndpoint(
                id=uuid.uuid4(),
                organisation_id=principal.organisation_id,
                url=req.url,
                secret_ciphertext=secret_ciphertext,
                events=req.events,
                description=req.description,
                account_id=principal.account_id,
                team_id=req.team_id,
            )
            session.add(ep)
            await session.flush()
    except ProgrammingError:
        logger.warning(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING, extra={"route": "create_webhook"})
        logger.exception(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "create_webhook"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "create_webhook"})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return _ep_to_response(ep)


@router.get("/{webhook_id}")
@handle_db_errors("admin.notifications.get_webhook")
async def get_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
) -> WebhookResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            ep = await session.get(NotificationEndpoint, webhook_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_WEBHOOK_NOT_FOUND)
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING, extra={"route": "get_webhook", "webhook_id": str(webhook_id)}
        )
        logger.exception(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "get_webhook", "webhook_id": str(webhook_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            _CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "get_webhook", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return _ep_to_response(ep)


@router.put("/{webhook_id}")
@handle_db_errors("admin.notifications.update_webhook")
async def update_webhook(
    webhook_id: uuid.UUID,
    req: WebhookUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
    settings: Settings = Depends(get_settings),
) -> WebhookResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            ep = await session.get(NotificationEndpoint, webhook_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_WEBHOOK_NOT_FOUND)

            if req.url is not None:
                ep.url = req.url
            if req.secret is not None:
                fernet = Fernet(settings.fernet_key.encode())
                ep.secret_ciphertext = fernet.encrypt(req.secret.encode())
            if req.events is not None:
                ep.events = req.events
            if req.description is not None:
                ep.description = req.description
            if req.team_id is not None:
                await _validate_team_exists(session, principal.organisation_id, req.team_id)
                ep.team_id = req.team_id

            await session.flush()
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING, extra={"route": "update_webhook", "webhook_id": str(webhook_id)}
        )
        logger.exception(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "update_webhook", "webhook_id": str(webhook_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            _CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "update_webhook", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return _ep_to_response(ep)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("admin.notifications.delete_webhook")
async def delete_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            ep = await session.get(NotificationEndpoint, webhook_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_WEBHOOK_NOT_FOUND)
            await session.delete(ep)
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING, extra={"route": "delete_webhook", "webhook_id": str(webhook_id)}
        )
        logger.exception(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "delete_webhook", "webhook_id": str(webhook_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            _CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "delete_webhook", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# ── Test ───────────────────────────────────────────────────────────────


@router.post("/{webhook_id}/test")
@handle_db_errors("admin.notifications.test_webhook")
async def test_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
    settings: Settings = Depends(get_settings),
) -> TestResult:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            ep = await session.get(NotificationEndpoint, webhook_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_WEBHOOK_NOT_FOUND)
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING, extra={"route": "test_webhook", "webhook_id": str(webhook_id)}
        )
        logger.exception(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "test_webhook", "webhook_id": str(webhook_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            _CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "test_webhook", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    payload = json.dumps(
        {
            "event": "test",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"type": "ping", "message": "Modulo notification test"},
        }
    ).encode()

    headers = {"Content-Type": _MSG_APPLICATION_JSON, "User-Agent": _MSG_MODULO_NOTIFIER_1_0}
    if ep.secret_ciphertext:
        try:
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                raw_secret = await decode_stored_secret_scoped(
                    session, ep.secret_ciphertext, settings.fernet_key, org_id=principal.organisation_id
                )
            sig = hmac.new(raw_secret.encode(), payload, hashlib.sha256).hexdigest()
            headers["X-Modulo-Signature"] = f"sha256={sig}"
        except Exception:
            logger.exception("Failed to sign test payload")

    try:
        client = await pinned_async_client(ep.url)
        client.timeout = httpx.Timeout(15.0)
    except ValueError as exc:
        logger.warning(
            "admin.notifications.test_webhook.ssrf_rejected",
            extra={"endpoint_id": str(ep.id), "error": str(exc)},
        )
        return TestResult(
            success=False,
            status_code=None,
            response_body=None,
            error=str(exc),
        )
    try:
        resp = await client.post(ep.url, content=payload, headers=headers)
        response_body = resp.text[:500]
        return TestResult(
            success=resp.is_success,
            status_code=resp.status_code,
            response_body=response_body,
            error=None,
        )
    except httpx.RequestError as exc:
        return TestResult(
            success=False,
            status_code=None,
            response_body=None,
            error=str(exc),
        )
    finally:
        await client.aclose()


# ── Re-enable ──────────────────────────────────────────────────────────


@router.post("/{webhook_id}/re-enable")
@handle_db_errors("admin.notifications.re_enable_webhook")
async def re_enable_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
) -> WebhookResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            ep = await session.get(NotificationEndpoint, webhook_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_WEBHOOK_NOT_FOUND)
            ep.auto_disabled = False
            ep.disabled_at = None
            ep.consecutive_dead_letter_count = 0
            await session.flush()
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING,
            extra={"route": "re_enable_webhook", "webhook_id": str(webhook_id)},
        )
        logger.exception(_CODE_NOTIFICATIONS_ENDPOINT_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(
            _CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "re_enable_webhook", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            _CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "re_enable_webhook", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return _ep_to_response(ep)


# ── Delivery log ───────────────────────────────────────────────────────


@router.get("/{webhook_id}/deliveries")
@handle_db_errors("admin.notifications.list_deliveries")
async def list_deliveries(
    webhook_id: uuid.UUID,
    cursor: str | None = Query(None, description="Cursor from previous response (ISO datetime)"),
    limit: int = Query(default=25, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
) -> DeliveryLogResponse:

    rows, total, ep = await _fetch_webhook_deliveries(
        session,
        principal,
        webhook_id=webhook_id,
        cursor=cursor,
        limit=limit,
        status_filter=status_filter,
    )

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows and rows[-1].created_at:
        next_cursor = rows[-1].created_at.isoformat()

    items = [_delivery_entry_from_row(d, ep.url) for d in rows]

    return DeliveryLogResponse(items=items, next_cursor=next_cursor, total=total)


async def _fetch_webhook_deliveries(
    session: AsyncSession,
    principal: TenantPrincipal,
    *,
    webhook_id: uuid.UUID,
    cursor: str | None,
    limit: int,
    status_filter: str | None,
) -> tuple[list[NotificationDeliveryLog], int, NotificationEndpoint]:
    """Load a webhook's delivery log page (with ownership check) and its total count."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            ep = await session.get(NotificationEndpoint, webhook_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_WEBHOOK_NOT_FOUND)

            query = select(NotificationDeliveryLog).where(
                NotificationDeliveryLog.endpoint_id == webhook_id,
                NotificationDeliveryLog.organisation_id == principal.organisation_id,
            )

            if status_filter:
                query = query.where(NotificationDeliveryLog.status == status_filter)

            if cursor:
                query = query.where(NotificationDeliveryLog.created_at < _parse_delivery_date(cursor, "cursor"))

            query = query.order_by(NotificationDeliveryLog.created_at.desc()).limit(limit + 1)

            rows = list((await session.execute(query)).scalars())

            count_result = await session.execute(
                select(sa_func.count(NotificationDeliveryLog.id)).where(
                    NotificationDeliveryLog.endpoint_id == webhook_id,
                    NotificationDeliveryLog.organisation_id == principal.organisation_id,
                )
            )
            total = count_result.scalar() or 0
            return rows, total, ep
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING,
            extra={"route": "list_deliveries", "webhook_id": str(webhook_id)},
        )
        logger.exception(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATION_DELIVERY_LOGGING_NOT,
        ) from None
    except SQLAlchemyError:
        logger.exception(
            _CODE_NOTIFICATIONS_DB_ERROR, extra={"route": "list_deliveries", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            _CODE_NOTIFICATIONS_UNEXPECTED_ERROR, extra={"route": "list_deliveries", "webhook_id": str(webhook_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


def _delivery_entry_from_row(row: NotificationDeliveryLog, endpoint_url: str) -> DeliveryLogEntry:
    return DeliveryLogEntry(
        id=str(row.id),
        event_type=row.event_type,
        status=row.status,
        attempt_count=row.attempt_count,
        response_code=row.response_code,
        last_error=row.last_error,
        response_body=row.response_body,
        endpoint_url=endpoint_url,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


# ── Manual retry ───────────────────────────────────────────────────────


@router.post("/{webhook_id}/deliveries/{delivery_id}/retry")
@handle_db_errors("admin.notifications.retry_delivery")
async def retry_delivery(
    webhook_id: uuid.UUID,
    delivery_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_NOTIFICATION_MANAGE),
    settings: Settings = Depends(get_settings),
) -> TestResult:

    ep, delivery = await _fetch_delivery_for_retry(session, principal, webhook_id, delivery_id)

    resp, error = await _retry_one_delivery(session, principal, settings, delivery, ep)
    if resp is None:
        return TestResult(success=False, status_code=None, response_body=None, error=error)

    return TestResult(
        success=resp.is_success,
        status_code=resp.status_code,
        response_body=resp.text[:500],
        error=None,
    )


async def _fetch_delivery_for_retry(
    session: AsyncSession,
    principal: TenantPrincipal,
    webhook_id: uuid.UUID,
    delivery_id: uuid.UUID,
) -> tuple[NotificationEndpoint, NotificationDeliveryLog]:
    """Load and ownership-check the webhook and its delivery log entry."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            ep = await session.get(NotificationEndpoint, webhook_id)
            if ep is None or ep.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_WEBHOOK_NOT_FOUND)

            delivery = await session.get(NotificationDeliveryLog, delivery_id)
            if delivery is None or delivery.endpoint_id != webhook_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Delivery log not found",
                )
            return ep, delivery
    except ProgrammingError:
        logger.warning(
            _CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING,
            extra={"route": "retry_delivery", "webhook_id": str(webhook_id), "delivery_id": str(delivery_id)},
        )
        logger.exception(_CODE_NOTIFICATIONS_DELIVERY_TABLE_MISSING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_NOTIFICATIONS_NOT_AVAILABLE_RUN,
        ) from None
    except SQLAlchemyError:
        logger.exception(
            _CODE_NOTIFICATIONS_DB_ERROR,
            extra={"route": "retry_delivery", "webhook_id": str(webhook_id), "delivery_id": str(delivery_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            _CODE_NOTIFICATIONS_UNEXPECTED_ERROR,
            extra={"route": "retry_delivery", "webhook_id": str(webhook_id), "delivery_id": str(delivery_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# ── Helper ─────────────────────────────────────────────────────────────


async def _validate_team_exists(session: AsyncSession, org_id: uuid.UUID, team_id: uuid.UUID) -> None:
    """Assert ``team_id`` references a non-deleted team in ``org_id`` (422 otherwise).

    Runs inside the RLS-scoped transaction, so the org filter is enforced by the
    RLS policy as well as the explicit ``organisation_id`` predicate.
    """
    result = await session.execute(
        select(Team).where(
            Team.id == team_id,
            Team.organisation_id == org_id,
            Team.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown team id: {team_id}",
        )


def _ep_to_response(ep: NotificationEndpoint) -> WebhookResponse:
    events = endpoint_events_to_list(ep.events)
    return WebhookResponse(
        id=str(ep.id),
        url=ep.url,
        events=events,
        description=ep.description,
        has_secret=ep.secret_ciphertext is not None,
        is_active=not bool(ep.auto_disabled) if ep.auto_disabled is not None else True,
        consecutive_dead_letter_count=ep.consecutive_dead_letter_count or 0,
        team_id=str(ep.team_id) if ep.team_id else None,
        disabled_at=ep.disabled_at.isoformat() if ep.disabled_at else None,
        created_at=ep.created_at.isoformat() if ep.created_at else "",
    )
