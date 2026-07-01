"""SSE endpoint for real-time event streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.events.event_bus import get_event_bus

_log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


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

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: resource_changed\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    _log.warning("sse.event_loop_error", exc_info=True)
                    break
        finally:
            event_bus.unsubscribe(org_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=headers,
    )
