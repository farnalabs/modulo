"""WebSocket endpoint for real-time run event streaming.

URL: GET /api/v1/runs/{run_id}/ws?since_event_seq=N&token=<ws-token>

Auth: Opaque 60s single-use token (preferred) or legacy JWT fallback,
obtained from POST /api/v1/auth/ws-token.
Passed as ``token`` query parameter (WebSocket handshake does not support
Authorization headers).

Protocol:
- Client obtains a ws-token via POST /api/v1/auth/ws-token (Bearer JWT auth).
- Connect with ``?since_event_seq=N`` to replay buffered events since seq N.
- Server sends ``RunEvent.to_json()`` objects for live events.
- When run reaches a terminal state, server sends ``{"status": "terminal"}``
  and closes the connection.
- If the run is already terminal at connect time, server sends terminal status
  and closes immediately (no ongoing subscription).
- After disconnect, clients should call GET /api/v1/runs/{id} (REST) to
  rebuild authoritative state.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import _get_engine
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.ws_token import WsTokenExpiredError, consume_ws_token
from modulo.core.pipeline_engine.error_codes import sanitize_error_text
from modulo.core.pipeline_engine.event_broker import RunEvent, get_registry
from modulo.db.crud.run import get_run
from modulo.db.models.run import TERMINAL_STATUSES
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_CODE_RUN_WS_RUN_WEBSOCKET = "run_ws.run_websocket"


_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/runs", tags=["runs-ws"])


def _sanitize_event(event: RunEvent) -> dict[str, Any]:
    """Serialise a broker event for the browser with error fields scrubbed.

    Defense-in-depth read-side guard: the executor sanitizes error detail at
    the publish/write site (FAR-163), but this forwarder must never ship raw
    tracebacks to the browser even if a future publisher skips that step.
    Only the error-carrying payload keys are scrubbed — node output payloads
    pass through untouched, and the original event is never mutated.
    """
    data = event.to_json()
    payload = data.get("payload")
    if isinstance(payload, dict):
        scrubbed = dict(payload)
        for key in ("detail", "error", "stall_reason"):
            if isinstance(scrubbed.get(key), str):
                scrubbed[key] = sanitize_error_text(scrubbed[key])
        data["payload"] = scrubbed
    return data


@router.websocket("/{run_id}/ws")
@handle_db_errors(_CODE_RUN_WS_RUN_WEBSOCKET)
async def run_websocket(
    ws: WebSocket,
    run_id: uuid.UUID,
    since_event_seq: int = 0,
    token: str | None = None,
) -> None:
    """Stream run events over WebSocket.

    Requires a short-lived ws-token from POST /api/v1/auth/ws-token
    (default 60s TTL, configurable via modulo_ws_token_ttl_seconds).
    Sends JSON objects conforming to RunEvent.to_json() schema.
    Closes with code 4001 on auth failure, 4004 on unknown run.
    """
    # --- Auth ---
    if token is None:
        await ws.close(code=4001)
        return
    settings = get_settings()

    # Consume opaque single-use ws-token.
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    try:
        payload = await consume_ws_token(redis, token)
    except WsTokenExpiredError:
        payload = None
    except Exception as exc:
        _log.exception(_CODE_RUN_WS_RUN_WEBSOCKET)
        _log.warning("ws_token.consume_failed", extra={"error": str(exc)})
        payload = None
    finally:
        await redis.aclose()

    if payload is None:
        await ws.close(code=4001)
        return
    principal = AuthenticatedPrincipal(
        username=payload["sub"],
        organisation_id=uuid.UUID(payload["org_id"]),
        account_id=uuid.UUID(payload.get("account_id") or payload.get("user_id")),
        org_role=payload["org_role"],
    )

    # Guard against absurd replay-range values.
    if since_event_seq < 0:
        await ws.close(code=4001)
        return
    if since_event_seq > 10_000:
        _log.warning("run_ws.replay_clamped", extra={"requested_seq": since_event_seq, "clamped_to": 0})
        since_event_seq = 0

    await ws.accept()

    # Alpha: engine created directly here rather than via DI (acceptable for alpha;
    # shares the same process-global pool used by the REST API via get_or_create_engine).
    engine = _get_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id, organisation_id=principal.organisation_id)
    except ProgrammingError:
        _log.exception(_CODE_RUN_WS_RUN_WEBSOCKET)
        await ws.send_json({"error": "migration_required", "detail": "Run database migrations to enable this feature."})
        await ws.close(code=1011)
        return
    except SQLAlchemyError:
        _log.exception(_CODE_RUN_WS_RUN_WEBSOCKET)
        await ws.send_json({"error": "db_unavailable", "detail": "Database temporarily unavailable."})
        await ws.close(code=1011)
        return
    except Exception:
        _log.exception("run_ws.db_check_failed")
        await ws.send_json({"error": "internal_error", "detail": MSG_UNEXPECTED_ERROR})
        await ws.close(code=1011)
        return

    if run is None:
        await ws.send_json({"error": "run_not_found", "detail": f"Run {run_id} not found"})
        await ws.close(code=4004)
        return

    if run.status in TERMINAL_STATUSES:
        await ws.send_json({"status": "terminal", "run_status": run.status, "run_id": str(run_id)})
        await ws.close()
        return

    # --- Subscribe to broker ---
    registry = get_registry()
    broker = registry.get_or_create(run_id)
    queue = broker.subscribe()

    try:
        # Replay buffered events the client missed
        for event in broker.replay_since(since_event_seq):
            await ws.send_json(_sanitize_event(event))

        # Forward live events until broker closes or client disconnects
        while True:
            item = await queue.get()
            if item is None:
                await ws.send_json({"status": "terminal"})
                break
            try:
                await ws.send_json(_sanitize_event(item))
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(queue)
