"""Webhook trigger endpoint.

URL: POST /api/v1/triggers/{trigger_id}/webhook
     POST /api/v1/triggers/{trigger_id}/webhook/replay/{event_id}

Auth: HMAC-SHA256 via X-Modulo-Webhook-Secret header (configured per trigger).
      X-Modulo-Timestamp header is required; validated within ±300s window.
      Triggers with no hmac_secret accept unauthenticated requests.

All delivery attempts are logged as TriggerEvent rows regardless of outcome.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import _get_engine, get_db_session, get_or_create_engine, pg_connection_string
from modulo.auth.dependencies import get_current_tenant_user_optional
from modulo.auth.jwt import TenantPrincipal
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.core.trigger_engine import (
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    ReplayNotFoundError,
    TimestampExpiredError,
    TriggerEngine,
    TriggerInactiveError,
    TriggerNotFoundError,
)
from modulo.db.crud.run import update_run_status
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/triggers", tags=["webhooks"])

_trigger_engine = TriggerEngine()


@handle_db_errors("webhooks.receive_webhook")
@router.post("/{trigger_id}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    trigger_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    engine: AsyncEngine = Depends(_get_engine),
) -> dict[str, Any]:
    """Receive an incoming webhook and enqueue a pipeline run.

    Requires X-Modulo-Timestamp header (Unix seconds, ±300s window).
    Requires X-Modulo-Webhook-Secret header if trigger has hmac_secret configured.

    Returns 202 on success. All validation outcomes are recorded as TriggerEvent rows.
    Returns 400 on duplicate payload, 401 on HMAC failure, 429 on flood rejection.
    """
    raw_body = await request.body()
    hmac_signature = request.headers.get("X-Modulo-Webhook-Secret")
    modulo_timestamp = request.headers.get("X-Modulo-Timestamp") or str(int(__import__("time").time()))

    try:
        raw_payload: dict[str, Any] = await request.json()
        if not isinstance(raw_payload, dict):
            raise TypeError("not a JSON object")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object",
        ) from exc

    try:
        async with session.begin():
            from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

            trigger_row = await session.execute(select(Trigger).where(Trigger.id == trigger_id))
            trigger = trigger_row.scalar_one_or_none()
            if trigger is None:
                raise TriggerNotFoundError(trigger_id=trigger_id)

            # Resolve org_id from trigger pipeline (for unauth webhooks) or from auth principal
            org_id = principal.organisation_id if principal else None
            if org_id is None:
                from modulo.db.models.pipeline import Pipeline

                pipe = await session.execute(select(Pipeline).where(Pipeline.id == trigger.pipeline_id))
                pipeline = pipe.scalar_one_or_none()
                if pipeline:
                    org_id = pipeline.organisation_id
            if org_id is None:
                raise HTTPException(status_code=401, detail="Could not resolve organization")

            await set_rls_org(session, org_id)
            snapshot = await create_snapshot_from_live_graph(session, pipeline_id=trigger.pipeline_id, account_id=None)
            if snapshot is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create pipeline snapshot for webhook trigger",
                )

            run, _, input_payload = await _trigger_engine.handle_webhook(
                session,
                trigger_id=trigger_id,
                org_id=org_id,
                raw_body=raw_body,
                raw_payload=raw_payload,
                hmac_signature=hmac_signature,
                modulo_timestamp=modulo_timestamp,
                snapshot_id=snapshot.id,
            )
    except TriggerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except TimestampExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Modulo-Timestamp is outside the ±300s replay window",
        ) from exc
    except HmacValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="HMAC signature verification failed",
        ) from exc
    except DuplicateWebhookError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate webhook payload",
        ) from exc
    except ConcurrentRunLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Concurrent run limit of {exc.limit} reached",
        ) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("receive_webhook failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    run_id = run.id
    executor = PipelineExecutor(
        engine,
        checkpointer_conn_string=pg_connection_string(str(engine.url)),
    )
    background_tasks.add_task(_run_in_background, executor, run_id, org_id, input_payload)

    return {"run_id": str(run_id), "status": "accepted"}


@handle_db_errors("webhooks.replay_webhook")
@router.post("/{trigger_id}/webhook/replay/{event_id}", status_code=status.HTTP_202_ACCEPTED)
async def replay_webhook(
    trigger_id: uuid.UUID,
    event_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    engine: AsyncEngine = Depends(_get_engine),
) -> dict[str, Any]:
    """Re-fire a webhook run from a previous TriggerEvent log entry.

    Replays the original raw payload through the trigger pipeline, skipping
    HMAC and timestamp validation but preserving dedup and flood protection.
    """
    try:
        async with session.begin():
            from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

            trigger_row = await session.execute(select(Trigger).where(Trigger.id == trigger_id))
            trigger = trigger_row.scalar_one_or_none()
            if trigger is None:
                raise TriggerNotFoundError(trigger_id=trigger_id)

            # Resolve org_id from trigger pipeline (for unauth webhooks) or from auth principal
            org_id = principal.organisation_id if principal else None
            if org_id is None:
                from modulo.db.models.pipeline import Pipeline

                pipe = await session.execute(select(Pipeline).where(Pipeline.id == trigger.pipeline_id))
                pipeline = pipe.scalar_one_or_none()
                if pipeline:
                    org_id = pipeline.organisation_id
            if org_id is None:
                raise HTTPException(status_code=401, detail="Could not resolve organization")

            await set_rls_org(session, org_id)
            snapshot = await create_snapshot_from_live_graph(session, pipeline_id=trigger.pipeline_id, account_id=None)
            if snapshot is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create pipeline snapshot for webhook replay",
                )

            run, _, input_payload = await _trigger_engine.replay_event(
                session,
                event_id=event_id,
                org_id=org_id,
                snapshot_id=snapshot.id,
            )
    except ReplayNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger event not found") from exc
    except TriggerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except DuplicateWebhookError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate webhook payload",
        ) from exc
    except ConcurrentRunLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Concurrent run limit of {exc.limit} reached",
        ) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("replay_webhook failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    run_id = run.id
    executor = PipelineExecutor(
        engine,
        checkpointer_conn_string=pg_connection_string(str(engine.url)),
    )
    background_tasks.add_task(_run_in_background, executor, run_id, org_id, input_payload)

    return {"run_id": str(run_id), "status": "accepted"}


@handle_db_errors("webhooks.cleanup_expired")
@router.post("/cleanup-expired", status_code=status.HTTP_200_OK)
async def cleanup_expired(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
) -> dict[str, int]:
    """Delete expired dedup hashes and webhook payloads.

    Acquires a Postgres advisory lock to prevent concurrent cleanup across workers.
    Safe to call from cron every 5 minutes.
    """
    org_id = principal.organisation_id if principal else None
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    result: dict[str, int] = {"dedup_hashes_deleted": 0, "payloads_deleted": 0}
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            result["dedup_hashes_deleted"] = await _trigger_engine.cleanup_expired_dedup_hashes(session)
        # Separate transaction for payloads
        async with session.begin():
            await set_rls_org(session, org_id)
            result["payloads_deleted"] = await _trigger_engine.cleanup_expired_payloads(session)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except Exception:
        _log.exception("Cleanup job failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cleanup job failed",
        ) from None
    return result


async def _run_in_background(
    executor: PipelineExecutor,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    input_payload: dict[str, Any],
) -> None:
    try:
        await executor.execute(run_id=run_id, org_id=org_id, input_payload=input_payload)
    except Exception:
        _log.exception("Unhandled error in webhook-triggered run %s", run_id)
        try:
            settings = get_settings()
            engine = get_or_create_engine(settings)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await update_run_status(session, run_id, "failed", error_code="internal_error")
        except Exception:
            _log.exception("webhook.run.mark_failed_error", extra={"run_id": str(run_id)})
