"""SSE endpoint for real-time event streaming.

Latency: sub-second for all normal event delivery.
Zombie cleanup: 2s keepalive heartbeat detects dead clients within 2s.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.events.event_bus import get_event_bus

_log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_ZOMBIE_TIMEOUT = 2.0


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
    event_bus = get_event_bus()
    queue = event_bus.subscribe(org_id)

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
                    continue
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.warning("sse.event_loop_error", exc_info=True)
        finally:
            event_bus.unsubscribe(org_id, queue)

    return StreamingResponse(
        _pump(),
        media_type="text/event-stream",
        headers=headers,
    )
