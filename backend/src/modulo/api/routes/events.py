"""SSE endpoint for real-time event streaming.

Latency: sub-second for all normal event delivery.
Zombie cleanup: 2s keepalive heartbeat detects dead clients within 2s.

FAR-250 cross-worker delivery: the first SSE client for an org acquires the
web-process Redis relay (subscribe on ``modulo:events:resource:{org_id}``,
refcounted per org, idle-TTL fallback), so events created in OTHER processes
(SAQ workers, other web replicas) reach this process's subscribers. Events
created locally already fan out in-process and their own Redis echo is
dropped by ``producer_id`` — each SSE client sees each event exactly once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from starlette import status

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory, require_permission
from modulo.core.events import relay as relay_mod
from modulo.core.events.event_bus import get_event_bus
from modulo.core.events.listeners import next_event_version
from modulo.core.events.notification_events import build_notification_event
from modulo.db.models.notification import Notification
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from modulo.auth.jwt import TenantPrincipal

_log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_active_connections: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
_queue_users: dict[int, str] = {}
_active_connections_lock: asyncio.Lock = asyncio.Lock()

# Relay reconnect backfill: how far back the DB re-read goes when a Redis
# subscription blips (pub/sub has no replay).
_BACKFILL_WINDOW = timedelta(minutes=5)
_BACKFILL_LIMIT = 100
_RELAY_IDLE_TTL_SECONDS = 60.0


# Test-reset tasks fired without a handle — kept referenced so the loop does
# not garbage-collect them mid-flight (RUF006).
_relay_reset_tasks: set[asyncio.Task[None]] = set()


def _test_reset_connections() -> None:
    """Test helper: clears all tracked SSE connections. Not for production use."""
    _active_connections.clear()
    _queue_users.clear()
    # Drop any relay subscription bookkeeping tied to the cleared connections.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        relay_mod._relay = None
        return
    task = loop.create_task(relay_mod.reset_relay_for_tests())
    _relay_reset_tasks.add(task)
    task.add_done_callback(_relay_reset_tasks.discard)


def _org_has_clients(org_id: str) -> bool:
    """True when the SSE route tracks at least one live connection for *org_id*."""
    return bool(_active_connections.get(org_id))


def _make_backfill_loader() -> Any:
    """Build the relay's DB backfill loader (api layer owns the session wiring).

    Re-reads recent notifications for *org_id* and shapes them into SSE event
    dicts (envelope + ``{notification_id, category, created_at}`` only — the
    relay backfills the notification gap; other resource types recover via
    their REST reads).
    """

    async def _load(org_id: str) -> list[dict[str, Any]]:
        org_uuid = uuid.UUID(org_id)
        engine = get_or_create_engine(get_settings())
        factory = get_or_create_session_factory(engine)
        cutoff = datetime.now(UTC) - _BACKFILL_WINDOW
        async with factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            result = await session.execute(
                select(Notification)
                .where(
                    Notification.organisation_id == org_uuid,
                    Notification.created_at >= cutoff,
                )
                .order_by(desc(Notification.created_at))
                .limit(_BACKFILL_LIMIT)
            )
            rows = list(result.scalars().all())
        return [
            build_notification_event(
                org_id=org_id,
                notification_id=row.id,
                category=row.category,
                created_at=row.created_at,
                version=next_event_version(org_id),
            )
            for row in rows
        ]

    return _load


async def _ensure_relay() -> Any:
    """Configure and return the web-process relay, or ``None`` when inert.

    Inert (``None`) when no Redis broker is configured on the bus (tests,
    Redis-less dev) — the SSE route then behaves exactly as before.
    """
    bus = get_event_bus()
    broker = bus.redis_broker
    if broker is None:
        return None
    return await relay_mod.configure_relay(
        broker=broker,
        bus=bus,
        has_clients=_org_has_clients,
        backfill_loader=_make_backfill_loader(),
        idle_ttl=_RELAY_IDLE_TTL_SECONDS,
    )


async def _track_connection(
    org_id: str,
    user_id: str,
    queue: asyncio.Queue[dict[str, Any]],
    max_org: int,
    max_user: int,
) -> None:
    """Register a connection, raising 429 if the per-org or per-user limit is exceeded."""
    async with _active_connections_lock:
        active = _active_connections.setdefault(org_id, set())
        if len(active) >= max_org:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many SSE connections for this organisation. Limit is {max_org} concurrent streams.",
            )
        user_count = sum(1 for q in active if _queue_users.get(id(q)) == user_id)
        if user_count >= max_user:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many SSE connections from this user. Limit is {max_user} concurrent streams per user.",
            )
        _queue_users[id(queue)] = user_id
        active.add(queue)


async def _untrack_connection(org_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Remove a connection from the active set."""
    async with _active_connections_lock:
        _queue_users.pop(id(queue), None)
        active = _active_connections.get(org_id)
        if active:
            active.discard(queue)
            if not active:
                del _active_connections[org_id]


@router.get(
    "/api/v1/events",
    operation_id="stream_events",
    summary="Subscribe to org-scoped real-time resource-change events via SSE.",
)
@handle_db_errors("events.sse_event_stream")
async def sse_event_stream(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    principal: TenantPrincipal = require_permission("events.list"),
) -> StreamingResponse:
    """SSE endpoint: streams resource-changed events for the current org.

    The client receives ``event: resource_changed`` messages with a JSON
    payload containing ``type``, ``id``, ``action``, ``version``, and
    ``org_id`` fields.

    Usage:
        curl -N -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/events
    """
    org_id = str(principal.organisation_id) if principal.organisation_id else ""
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot open SSE stream: user is not assigned to an organisation.",
        )

    event_bus = get_event_bus()
    queue = await event_bus.subscribe(org_id, maxsize=256)
    try:
        await _track_connection(
            org_id,
            str(principal.user_id),
            queue,
            settings.modulo_sse_max_connections_per_org,
            settings.modulo_sse_max_connections_per_user,
        )
    except HTTPException:
        await event_bus.unsubscribe(org_id, queue)
        raise

    # FAR-250: first client for this org subscribes the web-process relay to
    # Redis (refcounted). Fail-open: a relay problem must never reject the
    # stream — local delivery still works without it.
    relay = await _ensure_relay()
    if relay is not None:
        try:
            await relay.acquire(org_id)
        except asyncio.CancelledError:
            await event_bus.unsubscribe(org_id, queue)
            await _untrack_connection(org_id, queue)
            raise
        except Exception:
            _log.warning("sse.relay_acquire_failed", extra={"org_id": org_id}, exc_info=True)
            relay = None

    headers = {
        "Cache-Control": "no-cache, no-store",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    zombie_timeout = settings.modulo_sse_zombie_timeout_seconds

    async def _pump() -> AsyncGenerator[str, None]:
        """Pull events from the queue and write them to the stream.

        Two cleanup paths:
          1. **Graceful disconnect** — ASGI sends CancelledError when the
             client closes the connection. Caught here.
          2. **Zombie disconnect** — keepalive heartbeat catches hard drops
             (network failure, killed tab). Since ``queue.get()`` returns the
             instant an event is published, the timer only fires when nothing
             is changing — zero wakeups during active use.
        """
        yield ": connected\n\n"
        try:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=zombie_timeout)
                    yield f"event: resource_changed\ndata: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("sse.event_loop_error", exc_info=True)
        finally:
            await event_bus.unsubscribe(org_id, queue)
            await _untrack_connection(org_id, queue)
            if relay is not None:
                try:
                    await relay.release(org_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.warning("sse.relay_release_failed", extra={"org_id": org_id}, exc_info=True)

    return StreamingResponse(
        _pump(),
        media_type="text/event-stream",
        headers=headers,
    )
