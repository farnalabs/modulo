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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jwt import InvalidTokenError as JWTError
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import _get_engine
from modulo.auth.jwt import AuthenticatedPrincipal, decode_principal
from modulo.auth.ws_token import WsTokenExpiredError, consume_ws_token
from modulo.core.pipeline_engine.event_broker import get_registry
from modulo.db.crud.run import get_run
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

try:
    from redis.asyncio import Redis
except ImportError:
    Redis = None  # type: ignore[assignment,misc]

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/runs", tags=["runs-ws"])

_TERMINAL_STATUSES = {"complete", "failed", "cancelled"}


@router.websocket("/{run_id}/ws")
@handle_db_errors("run_ws.run_websocket")
@router.websocket("/{run_id}/ws")
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

    # Try opaque single-use token first, fall back to JWT for backward compat.
    principal: AuthenticatedPrincipal | None = None
    if settings.redis_url and Redis is not None:
        redis: Redis | None = None
        try:
            redis = Redis.from_url(settings.redis_url, decode_responses=False)
            try:
                payload = await consume_ws_token(redis, token)
                principal = AuthenticatedPrincipal(
                    username=payload["sub"],
                    organisation_id=uuid.UUID(payload["org_id"]),
                    account_id=uuid.UUID(payload["user_id"]),
                    org_role=payload["org_role"],
                )
            except WsTokenExpiredError:
                pass
        except Exception as exc:
            _log.warning("ws_token.consume_failed", extra={"error": str(exc)})
        finally:
            if redis is not None:
                await redis.aclose()

    if principal is None:
        try:
            principal = decode_principal(token, settings.secret_key, allowed_purposes=["ws"])
        except JWTError:
            await ws.close(code=4001)
            return

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
            run = await get_run(session, run_id)
    except ProgrammingError:
        await ws.send_json({"error": "migration_required", "detail": "Run database migrations to enable this feature."})
        await ws.close(code=1011)
        return
    except SQLAlchemyError:
        await ws.send_json({"error": "db_unavailable", "detail": "Database temporarily unavailable."})
        await ws.close(code=1011)
        return
    except Exception:
        _log.exception("run_ws.db_check_failed")
        await ws.send_json({"error": "internal_error", "detail": "An unexpected error occurred."})
        await ws.close(code=1011)
        return

    if run is None:
        await ws.send_json({"error": "run_not_found", "detail": f"Run {run_id} not found"})
        await ws.close(code=4004)
        return

    if run.status in _TERMINAL_STATUSES:
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
            await ws.send_json(event.to_json())

        # Forward live events until broker closes or client disconnects
        while True:
            item = await queue.get()
            if item is None:
                await ws.send_json({"status": "terminal"})
                break
            try:
                await ws.send_json(item.to_json())
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(queue)
