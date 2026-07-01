"""SSE endpoint for real-time event streaming.

Latency: sub-second for all normal event delivery.
Zombie cleanup: 2s keepalive heartbeat detects dead clients within 2s.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette import status

from modulo.auth.dependencies import get_current_user
from modulo.core.events.event_bus import get_event_bus
from modulo.settings import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from modulo.auth.jwt import AuthenticatedPrincipal

_log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_active_connections: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
_queue_users: dict[int, str] = {}
_active_connections_lock: asyncio.Lock = asyncio.Lock()


def _reset_connections() -> None:
    """Clear all tracked SSE connections. Used in tests to prevent state leakage."""
    _active_connections.clear()
    _queue_users.clear()


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
async def sse_event_stream(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
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
            pass
        except Exception:
            _log.warning("sse.event_loop_error", exc_info=True)
        finally:
            await event_bus.unsubscribe(org_id, queue)
            await _untrack_connection(org_id, queue)

    return StreamingResponse(
        _pump(),
        media_type="text/event-stream",
        headers=headers,
    )
