"""SSE endpoint for real-time event streaming.

Latency: sub-second for all normal event delivery.
Zombie cleanup: 2s keepalive heartbeat detects dead clients within 2s.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette import status

from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.events.event_bus import get_event_bus

_log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_ZOMBIE_TIMEOUT = 2.0
_MAX_CONNECTIONS_PER_ORG = 100
_MAX_CONNECTIONS_PER_USER = 10

_active_connections: dict[str, set[asyncio.Queue]] = {}
_queue_users: dict[int, str] = {}  # id(q) -> user_id
_active_connections_lock: asyncio.Lock = asyncio.Lock()


async def _track_connection(org_id: str, user_id: str, queue: asyncio.Queue) -> None:
    """Register a connection, raising 429 if the per-org or per-user limit is exceeded."""
    async with _active_connections_lock:
        active = _active_connections.setdefault(org_id, set())
        if len(active) >= _MAX_CONNECTIONS_PER_ORG:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many SSE connections for this organisation. "
                f"Limit is {_MAX_CONNECTIONS_PER_ORG} concurrent streams.",
            )
        user_count = sum(
            1 for q in active if _queue_users.get(id(q)) == user_id
        )
        if user_count >= _MAX_CONNECTIONS_PER_USER:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many SSE connections from this user. "
                f"Limit is {_MAX_CONNECTIONS_PER_USER} concurrent streams per user.",
            )
        _queue_users[id(queue)] = user_id
        active.add(queue)


async def _untrack_connection(org_id: str, queue: asyncio.Queue) -> None:
    """Remove a connection from the active set."""
    async with _active_connections_lock:
        _queue_users.pop(id(queue), None)
        active = _active_connections.get(org_id)
        if active:
            active.discard(queue)
            if not active:
                del _active_connections[org_id]


@router.get("/api/v1/events")
async def sse_event_stream(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot open SSE stream: user is not assigned to an organisation.",
        )

    event_bus = get_event_bus()
    queue = await event_bus.subscribe(org_id)
    await _track_connection(org_id, str(principal.user_id), queue)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    async def _pump():
        """Pull events from the queue and write them to the stream.

        Two cleanup paths:
          1. **Graceful disconnect** — ASGI sends CancelledError when the
             client closes the connection. Caught here.
          2. **Zombie disconnect** — 2s keepalive poll catches hard drops
             (network failure, killed tab). Also: since ``queue.get()``
             returns the instant an event is published, the 2s timer only
             fires when *nothing is changing* — zero wakeups during active
             use.
        """
        try:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_ZOMBIE_TIMEOUT)
                    yield f"event: resource_changed\ndata: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
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
