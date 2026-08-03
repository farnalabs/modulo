"""Webhook trigger endpoint.

URL: POST /api/v1/triggers/{trigger_id}/webhook
     POST /api/v1/triggers/{trigger_id}/webhook/replay/{event_id}

Auth: HMAC-SHA256 via X-Modulo-Webhook-Secret header (configured per trigger).
      X-Modulo-Timestamp header is required; validated within ±300s window.
      Triggers with no hmac_secret accept unauthenticated requests.

All delivery attempts are logged as TriggerEvent rows regardless of outcome.
"""

import asyncio
import functools
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    _get_engine,
    get_current_tenant_user_optional,
    get_db_session,
    require_permission,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import PermissionDenied, assert_org_role
from modulo.core.dispatch import dispatch_run_sync
from modulo.core.trigger_engine import (
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    PipelineRateLimitError,
    ReplayNotFoundError,
    TimestampExpiredError,
    TriggerEngine,
    TriggerInactiveError,
    TriggerNotFoundError,
    _verify_hmac,
    _verify_timestamp,
)
from modulo.db.models.trigger import Trigger
from modulo.db.models.webhook import WebhookPayload
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/triggers", tags=["webhooks"])

_trigger_engine = TriggerEngine()


@handle_db_errors("webhooks.receive_webhook")
@router.post("/{trigger_id}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    trigger_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    engine: AsyncEngine = Depends(_get_engine),
) -> dict[str, Any]:
    """Receive an incoming webhook and enqueue a pipeline run.

    Requires X-Modulo-Timestamp header (Unix seconds, ±300s window).
    Requires X-Modulo-Webhook-Secret header if trigger has hmac_secret configured.

    ADR 017 exempt-channel: this route is CSRF-exempt via the audited
    ``/api/v1/triggers/`` prefix and exempt from the org-role sweep because it
    authenticates via the trigger's shared-secret HMAC (or is public run
    creation for HMAC-less triggers by design). Replay and cleanup-expired are
    NOT exempt — see those handlers.

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
        _log.exception("webhooks.receive_webhook")
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

            run, _, _input_payload = await _trigger_engine.handle_webhook(
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
    except PipelineRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except ProgrammingError:
        _log.exception("webhooks.receive_webhook")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("webhooks.receive_webhook")
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
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        functools.partial(dispatch_run_sync, queue="runs", fail_fast=True),
        str(run_id),
        str(org_id),
    )

    return {"run_id": str(run_id), "status": "accepted"}


@handle_db_errors("webhooks.replay_webhook")
@router.post("/{trigger_id}/webhook/replay/{event_id}", status_code=status.HTTP_202_ACCEPTED)
async def replay_webhook(
    trigger_id: uuid.UUID,
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    engine: AsyncEngine = Depends(_get_engine),
) -> dict[str, Any]:
    """Re-fire a webhook run from a previous TriggerEvent log entry.

    Replays the original raw payload through the trigger pipeline, skipping
    HMAC and timestamp validation but preserving dedup and flood protection.

    ADR 017: replay is a mutating run-creation channel and is NOT exempt. A
    principal (if present) must hold the ``run.trigger`` permission (``runner``
    minimum). An unauthenticated caller must present a valid HMAC signature
    (``X-Modulo-Webhook-Secret`` + ``X-Modulo-Timestamp``) over the stored
    payload — the same verification ``receive_webhook`` performs.
    """
    if principal is not None:
        try:
            assert_org_role(principal.org_role, "runner", "run.trigger")
        except PermissionDenied as exc:
            _log.warning(
                "permission.denied",
                extra={
                    "permission": "run.trigger",
                    "required": "runner",
                    "actual": principal.org_role,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission 'run.trigger' requires 'runner' role",
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

            if principal is None:
                # ADR 017: unauthenticated replay requires a valid HMAC signature
                # over the stored payload (same check as receive_webhook).
                hmac_signature = request.headers.get("X-Modulo-Webhook-Secret")
                modulo_timestamp = request.headers.get("X-Modulo-Timestamp")
                ts = _verify_timestamp(modulo_timestamp)
                cfg = trigger.config_json or {}
                hmac_secret: str | None = cfg.get("hmac_secret")
                if hmac_secret is None:
                    raise HmacValidationError()
                payload_row = await session.execute(
                    select(WebhookPayload).where(
                        WebhookPayload.trigger_event_id == event_id,
                        WebhookPayload.organisation_id == org_id,
                    )
                )
                stored = payload_row.scalar_one_or_none()
                if stored is None:
                    raise ReplayNotFoundError(event_id)
                if not _verify_hmac(stored.raw_body, hmac_secret, hmac_signature, timestamp=ts):
                    raise HmacValidationError()

            snapshot = await create_snapshot_from_live_graph(session, pipeline_id=trigger.pipeline_id, account_id=None)
            if snapshot is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create pipeline snapshot for webhook replay",
                )

            run, _, _input_payload = await _trigger_engine.replay_event(
                session,
                event_id=event_id,
                org_id=org_id,
                snapshot_id=snapshot.id,
            )
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
        _log.exception("webhooks.replay_webhook")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("webhooks.replay_webhook")
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
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        functools.partial(dispatch_run_sync, queue="runs", fail_fast=True),
        str(run_id),
        str(org_id),
    )

    return {"run_id": str(run_id), "status": "accepted"}


@handle_db_errors("webhooks.cleanup_expired")
@router.post("/cleanup-expired", status_code=status.HTTP_200_OK)
async def cleanup_expired(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.cleanup"),
) -> dict[str, int]:
    """Delete expired dedup hashes and webhook payloads.

    Acquires a Postgres advisory lock to prevent concurrent cleanup across workers.
    Safe to call from cron every 5 minutes (with a ``runner`` credential).

    ADR 017: swept with ``trigger.cleanup`` (``runner`` minimum) — this route
    mutates state and resolves a user principal, so it is no longer exempt.
    """
    org_id = principal.organisation_id

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
        _log.exception("webhooks.cleanup_expired")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("webhooks.cleanup_expired")
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
