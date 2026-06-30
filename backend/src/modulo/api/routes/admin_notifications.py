"""Admin notification webhook management — CRUD, test, re-enable, delivery log, retry."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1/admin/notifications", tags=["admin-notifications"])

AVAILABLE_EVENTS = [
    "hitl_awaiting",
    "run_failed",
    "claim_expired",
    "hitl_overdue",
]


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required for notification management",
        )


# ── Request / Response models ──────────────────────────────────────────


class WebhookCreate(BaseModel):
    url: str = Field(..., max_length=2048)
    secret: str | None = Field(None)
    events: list[str] = Field(default_factory=list)
    description: str | None = Field(None, max_length=500)

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
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

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
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


@router.get("/deliveries", response_model=DeliveryLogResponse)
async def list_all_deliveries(
    cursor: str | None = Query(None, description="Cursor from previous response (ISO datetime)"),
    limit: int = Query(default=25, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    event_type_filter: str | None = Query(None, alias="event_type"),
    endpoint_id_filter: uuid.UUID | None = Query(None, alias="endpoint_id"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> DeliveryLogResponse:
    _require_admin(principal)
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
        logger.exception("notifications.delivery_table_missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Notification delivery logging is not available. Run database migrations to enable it.",
        )


async def _list_deliveries(
    cursor: str | None,
    limit: int,
    status_filter: str | None,
    event_type_filter: str | None,
    endpoint_id_filter: uuid.UUID | None,
    date_from: str | None,
    date_to: str | None,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
) -> DeliveryLogResponse:
    from sqlalchemy import func as sa_func

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
            try:
                dt_from = datetime.fromisoformat(date_from)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid from date format",
                ) from exc
            query = query.where(NotificationDeliveryLog.created_at >= dt_from)

        if date_to:
            try:
                dt_to = datetime.fromisoformat(date_to)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid to date format",
                ) from exc
            query = query.where(NotificationDeliveryLog.created_at <= dt_to)

        if cursor:
            try:
                cursor_dt = datetime.fromisoformat(cursor)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid cursor format",
                ) from exc
            query = query.where(NotificationDeliveryLog.created_at < cursor_dt)

        query = query.order_by(NotificationDeliveryLog.created_at.desc()).limit(limit + 1)

        rows = list((await session.execute(query)).all())

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        next_cursor = rows[-1][0].created_at.isoformat() if rows[-1][0].created_at else None

    total = 0
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
    total = count_result.scalar() or 0

    items = [
        DeliveryLogEntry(
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
        for d in rows
    ]

    return DeliveryLogResponse(items=items, next_cursor=next_cursor, total=total)


@router.post("/deliveries/retry-all-failed")
async def retry_all_failed_deliveries(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Retry all failed and dead_lettered deliveries across all webhooks in the org."""
    _require_admin(principal)
    import httpx

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

    retried = 0
    errors: list[str] = []

    for delivery, ep in failed:
        event_type = delivery.event_type
        body = json.dumps(
            {
                "event": delivery.event_type,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": {"event_type": event_type, "retry": True},
            }
        ).encode()

        headers = {"Content-Type": "application/json", "User-Agent": "Modulo-Notifier/1.0"}
        if ep.secret_ciphertext:
            try:
                fernet = Fernet(settings.fernet_key.encode())
                raw_secret = fernet.decrypt(ep.secret_ciphertext)
                import hashlib
                import hmac

                sig = hmac.new(raw_secret, body, hashlib.sha256).hexdigest()
                headers["X-Modulo-Signature"] = f"sha256={sig}"
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Failed to sign retry payload")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(ep.url, content=body, headers=headers)

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

            retried += 1
        except httpx.RequestError as exc:
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

            errors.append(str(exc))
            retried += 1

    return {"retried": retried, "errors": errors, "success": len(errors) == 0}


@router.get("/available-events", response_model=list[str])
async def list_available_events(
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[str]:
    _require_admin(principal)
    return AVAILABLE_EVENTS


# ── Webhook CRUD ────────────────────────────────────────────────────────


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[WebhookResponse]:
    _require_admin(principal)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(NotificationEndpoint)
            .where(NotificationEndpoint.organisation_id == principal.organisation_id)
            .order_by(NotificationEndpoint.created_at.desc())
        )
        endpoints = list(result.scalars())
    return [_ep_to_response(ep) for ep in endpoints]


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: WebhookCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WebhookResponse:
    _require_admin(principal)
    fernet = Fernet(settings.fernet_key.encode())
    secret_ciphertext: bytes | None = None
    if body.secret:
        secret_ciphertext = fernet.encrypt(body.secret.encode())

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = NotificationEndpoint(
            id=uuid.uuid4(),
            organisation_id=principal.organisation_id,
            url=body.url,
            secret_ciphertext=secret_ciphertext,
            events=json.dumps(body.events),
            description=body.description,
            created_by=principal.account_id,
        )
        session.add(ep)
        await session.flush()

    return _ep_to_response(ep)


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> WebhookResponse:
    _require_admin(principal)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, webhook_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    return _ep_to_response(ep)


@router.put("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> WebhookResponse:
    _require_admin(principal)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, webhook_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        if body.url is not None:
            ep.url = body.url
        if body.secret is not None:
            fernet = Fernet(settings.fernet_key.encode())
            ep.secret_ciphertext = fernet.encrypt(body.secret.encode())
        if body.events is not None:
            ep.events = json.dumps(body.events)
        if body.description is not None:
            ep.description = body.description

        await session.flush()

    return _ep_to_response(ep)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    _require_admin(principal)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, webhook_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
        await session.delete(ep)


# ── Test ───────────────────────────────────────────────────────────────


@router.post("/{webhook_id}/test", response_model=TestResult)
async def test_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> TestResult:
    _require_admin(principal)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, webhook_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

    import httpx

    payload = json.dumps(
        {
            "event": "test",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"type": "ping", "message": "Modulo notification test"},
        }
    ).encode()

    headers = {"Content-Type": "application/json", "User-Agent": "Modulo-Notifier/1.0"}
    if ep.secret_ciphertext:
        try:
            fernet = Fernet(settings.fernet_key.encode())
            raw_secret = fernet.decrypt(ep.secret_ciphertext)
            import hashlib
            import hmac

            sig = hmac.new(raw_secret, payload, hashlib.sha256).hexdigest()
            headers["X-Modulo-Signature"] = f"sha256={sig}"
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Failed to sign test payload")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
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


# ── Re-enable ──────────────────────────────────────────────────────────


@router.post("/{webhook_id}/re-enable", response_model=WebhookResponse)
async def re_enable_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> WebhookResponse:
    _require_admin(principal)
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, webhook_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
        ep.auto_disabled = False
        ep.disabled_at = None
        ep.consecutive_dead_letter_count = 0
        await session.flush()
    return _ep_to_response(ep)


# ── Delivery log ───────────────────────────────────────────────────────


@router.get("/{webhook_id}/deliveries", response_model=DeliveryLogResponse)
async def list_deliveries(
    webhook_id: uuid.UUID,
    cursor: str | None = Query(None, description="Cursor from previous response (ISO datetime)"),
    limit: int = Query(default=25, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> DeliveryLogResponse:
    _require_admin(principal)
    from sqlalchemy import func as sa_func

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, webhook_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        query = select(NotificationDeliveryLog).where(
            NotificationDeliveryLog.endpoint_id == webhook_id,
            NotificationDeliveryLog.organisation_id == principal.organisation_id,
        )

        if status_filter:
            query = query.where(NotificationDeliveryLog.status == status_filter)

        if cursor:
            try:
                cursor_dt = datetime.fromisoformat(cursor)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid cursor format",
                ) from exc
            query = query.where(NotificationDeliveryLog.created_at < cursor_dt)

        query = query.order_by(NotificationDeliveryLog.created_at.desc()).limit(limit + 1)

        rows = list((await session.execute(query)).scalars())

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        next_cursor = rows[-1].created_at.isoformat() if rows[-1].created_at else None

    total = 0
    count_result = await session.execute(
        select(sa_func.count(NotificationDeliveryLog.id)).where(
            NotificationDeliveryLog.endpoint_id == webhook_id,
            NotificationDeliveryLog.organisation_id == principal.organisation_id,
        )
    )
    total = count_result.scalar() or 0

    endpoint_url = ep.url

    items = [
        DeliveryLogEntry(
            id=str(d.id),
            event_type=d.event_type,
            status=d.status,
            attempt_count=d.attempt_count,
            response_code=d.response_code,
            last_error=d.last_error,
            response_body=d.response_body,
            endpoint_url=endpoint_url,
            created_at=d.created_at.isoformat() if d.created_at else "",
        )
        for d in rows
    ]

    return DeliveryLogResponse(items=items, next_cursor=next_cursor, total=total)


# ── Manual retry ───────────────────────────────────────────────────────


@router.post("/{webhook_id}/deliveries/{delivery_id}/retry", response_model=TestResult)
async def retry_delivery(
    webhook_id: uuid.UUID,
    delivery_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> TestResult:
    _require_admin(principal)
    import httpx

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        ep = await session.get(NotificationEndpoint, webhook_id)
        if ep is None or ep.organisation_id != principal.organisation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        delivery = await session.get(NotificationDeliveryLog, delivery_id)
        if delivery is None or delivery.endpoint_id != webhook_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Delivery log not found",
            )

    event_type = delivery.event_type
    body = json.dumps(
        {
            "event": delivery.event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {"event_type": event_type, "retry": True},
        }
    ).encode()

    headers = {"Content-Type": "application/json", "User-Agent": "Modulo-Notifier/1.0"}
    if ep.secret_ciphertext:
        try:
            fernet = Fernet(settings.fernet_key.encode())
            raw_secret = fernet.decrypt(ep.secret_ciphertext)
            import hashlib
            import hmac

            sig = hmac.new(raw_secret, body, hashlib.sha256).hexdigest()
            headers["X-Modulo-Signature"] = f"sha256={sig}"
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Failed to sign retry payload")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(ep.url, content=body, headers=headers)

        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            new_log = NotificationDeliveryLog(
                organisation_id=principal.organisation_id,
                event_type=delivery.event_type,
                endpoint_id=webhook_id,
                status="delivered" if resp.is_success else "failed",
                attempt_count=delivery.attempt_count + 1,
                response_code=resp.status_code,
                response_body=resp.text[:500] if resp.is_success else None,
                last_error=(None if resp.is_success else f"HTTP {resp.status_code}: {resp.text[:200]}"),
            )
            session.add(new_log)

        return TestResult(
            success=resp.is_success,
            status_code=resp.status_code,
            response_body=resp.text[:500],
            error=None,
        )
    except httpx.RequestError as exc:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            new_log = NotificationDeliveryLog(
                organisation_id=principal.organisation_id,
                event_type=delivery.event_type,
                endpoint_id=webhook_id,
                status="failed",
                attempt_count=delivery.attempt_count + 1,
                response_code=None,
                response_body=None,
                last_error=str(exc),
            )
            session.add(new_log)

        return TestResult(
            success=False,
            status_code=None,
            response_body=None,
            error=str(exc),
        )


# ── Helper ─────────────────────────────────────────────────────────────


def _ep_to_response(ep: NotificationEndpoint) -> WebhookResponse:
    events: list[str] = []
    try:
        events = json.loads(ep.events) if ep.events else []
    except (json.JSONDecodeError, TypeError):
        pass
    return WebhookResponse(
        id=str(ep.id),
        url=ep.url,
        events=events,
        description=ep.description,
        has_secret=ep.secret_ciphertext is not None,
        is_active=not bool(ep.auto_disabled) if ep.auto_disabled is not None else True,
        consecutive_dead_letter_count=ep.consecutive_dead_letter_count or 0,
        disabled_at=ep.disabled_at.isoformat() if ep.disabled_at else None,
        created_at=ep.created_at.isoformat() if ep.created_at else "",
    )
