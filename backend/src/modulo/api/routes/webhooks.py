"""Webhook trigger endpoint.

URL: POST /api/v1/triggers/{trigger_id}/webhook
     POST /api/v1/triggers/{trigger_id}/webhook/replay/{event_id}

Auth: HMAC-SHA256 via X-Modulo-Webhook-Secret header (configured per trigger).
      X-Modulo-Timestamp header is required; validated within ±300s window.
      Triggers with no hmac_secret accept unauthenticated requests.

All delivery attempts are logged as TriggerEvent rows regardless of outcome.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.constants import (
    MSG_DB_OPERATION_FAILED,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_INTERNAL_SERVER_ERROR,
)
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    _get_engine,
    get_current_tenant_user_optional,
    get_db_session,
    get_or_create_engine,
    require_permission,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import PermissionDenied, assert_org_role
from modulo.auth.secret_storage import decode_stored_secret
from modulo.core.dispatch import dispatch_run
from modulo.core.error_tracking import ErrorIngestionService
from modulo.core.exceptions import SnapshotLockNotAvailableError, TriggersPausedError

# Deprecated private aliases — kept importable so legacy patch targets and
# callers referencing the underscore names keep working (M5 public-API fix).
from modulo.core.trigger_engine import (  # noqa: F401
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    HmacValidationError,
    PipelineRateLimitError,
    ReplayNotFoundError,
    TimestampExpiredError,
    TriggerEngine,
    TriggerInactiveError,
    TriggerNotFoundError,
    _sha256_hex,
    _verify_hmac,
    _verify_timestamp,
    sha256_hex,
    verify_hmac,
    verify_timestamp,
)
from modulo.core.trigger_engine.pre_guardrail import GuardrailBlockedAtIntakeError
from modulo.db.models.organisation import Organisation
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.webhook import WebhookPayload
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.db.settings_resolver import ensure_triggers_resumable
from modulo.settings import get_settings
from modulo.version import get_version

_CODE_WEBHOOKS_RECEIVE_WEBHOOK = "webhooks.receive_webhook"
_MSG_TRIGGER_NOT_FOUND = "Trigger not found"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/triggers", tags=["webhooks"])

_trigger_engine = TriggerEngine()


async def _org_row_exists(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether the org row still exists.

    Orphan-org guard for the ``paused`` TriggerEvent write: a trigger whose org
    row was HARD-deleted must not attempt the INSERT (it would violate the
    organisations FK -> 503). Fail-closed with no event row and no crash.
    """
    org_exists = await session.execute(select(Organisation.id).where(Organisation.id == org_id))
    return org_exists.scalar_one_or_none() is not None


async def _ingest_webhook_dispatch_error(run_id: str, org_id: str, detail: str) -> None:
    """Ingest an error_event (source='saq', function='webhook_dispatch').

    Best-effort and never raises — error ingestion must not break the request
    or the background dispatch task. Runs in its own session/transaction.
    """
    try:
        oid = uuid.UUID(str(org_id))
        factory = async_sessionmaker(
            get_or_create_engine(get_settings()),
            expire_on_commit=False,
            autobegin=False,
        )
        async with factory() as session, session.begin():
            await set_rls_org(session, oid)
            await ErrorIngestionService().ingest(
                session,
                oid,
                {
                    "level": "error",
                    "message": f"webhook dispatch failed for run {run_id}: {detail}",
                    "source": "saq",
                    "context_json": {"function": "webhook_dispatch", "run_id": run_id},
                    "environment": os.environ.get("MODULO_ENV", "development"),
                    "version": get_version(),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("webhooks.ingest_dispatch_error_failed run=%s", run_id)


async def _dispatch_webhook_run(run_id: str, org_id: str) -> None:
    """Dispatch a webhook-created run via fail-fast SAQ enqueue (background).

    Records an ``error_event`` (source='saq', function='webhook_dispatch') when
    the fail-fast enqueue fails or raises, WITHOUT blocking the 202 response.
    Capacity-deferred runs are NOT errors — ``dispatcher_reconcile`` recovers
    them — so only the ``enqueue_failed`` outcome (or a raised exception) is
    reported.
    """
    try:
        outcome, _job_id = await dispatch_run(str(run_id), str(org_id), queue="runs", fail_fast=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _ingest_webhook_dispatch_error(str(run_id), str(org_id), f"dispatch raised: {exc}")
        return
    if outcome == "enqueue_failed":
        await _ingest_webhook_dispatch_error(str(run_id), str(org_id), "SAQ enqueue failed")


@router.post(
    "/{trigger_id}/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"description": "Bad request"},
        401: {"description": "Unauthorized"},
        404: {"description": "Not Found"},
        422: {"description": "Unprocessable Entity"},
        429: {"description": "Too Many Requests"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_WEBHOOKS_RECEIVE_WEBHOOK)
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

    ADR 017 exempt-channel: this route is CSRF-exempt via the audited
    ``/api/v1/triggers/`` prefix and exempt from the org-role sweep because it
    authenticates via the trigger's shared-secret HMAC (or is public run
    creation for HMAC-less triggers by design). Replay and cleanup-expired are
    NOT exempt — see those handlers.

    Returns 202 on success. All validation outcomes are recorded as TriggerEvent rows.
    Returns 400 on duplicate payload or guardrail-blocked payload, 401 on HMAC
    failure, 429 on flood rejection.
    """
    raw_body = await request.body()
    hmac_signature = request.headers.get("X-Modulo-Webhook-Secret")
    modulo_timestamp = request.headers.get("X-Modulo-Timestamp") or str(int(time.time()))
    trigger: Trigger | None = None
    guardrail_block_detail: str | None = None

    try:
        raw_payload: dict[str, Any] = await request.json()
        if not isinstance(raw_payload, dict):
            raise TypeError("not a JSON object")
    except Exception as exc:
        _log.exception(_CODE_WEBHOOKS_RECEIVE_WEBHOOK)
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
            await set_rls_execution_context(session)

            # Route-level timestamp + HMAC validation (belt-and-braces ahead of
            # the engine's own check). Only triggers configured with an
            # hmac_secret are validated here — HMAC-less triggers accept
            # unauthenticated deliveries by design. Failure events for these
            # typed errors are rolled back with the request (documented
            # pre-existing limitation).
            cfg = trigger.config_json or {}
            hmac_secret_raw: str | None = cfg.get("hmac_secret")
            hmac_secret: str | None = None
            if hmac_secret_raw is not None:
                try:
                    hmac_secret = decode_stored_secret(hmac_secret_raw, get_settings().fernet_key)
                except Exception:
                    _log.exception("webhooks.hmac_secret_decrypt_failed trigger=%s", trigger_id)
                    hmac_secret = hmac_secret_raw
            if hmac_secret is not None:
                ts = verify_timestamp(modulo_timestamp)
                if not verify_hmac(raw_body, hmac_secret, hmac_signature, timestamp=ts):
                    raise HmacValidationError()
            else:
                _log.warning(
                    "webhooks.receive_webhook: unauthenticated delivery accepted "
                    "(no hmac_secret configured) — trigger %s org %s",
                    trigger_id,
                    org_id,
                )

            try:
                # Pause pre-check BEFORE the snapshot and dedup work — a paused
                # delivery must not touch pipeline_snapshots, webhook_dedup_hashes,
                # or create a run. The inner catch is the SINGLE writer of the
                # ``paused`` TriggerEvent; it commits with this transaction.
                if trigger.active:
                    await ensure_triggers_resumable(session, org_id, trigger_id=trigger_id, trigger_type="webhook")

                snapshot = await create_snapshot_from_live_graph(
                    session, pipeline_id=trigger.pipeline_id, account_id=None
                )
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
            except TriggersPausedError:
                # Safe event write: an orphan trigger whose org row was
                # HARD-deleted must not attempt the TriggerEvent INSERT (it
                # would violate the organisations FK -> 503). Fail-closed with
                # no event row and no crash.
                if not await _org_row_exists(session, org_id):
                    _log.warning(
                        "webhooks.receive_webhook: org %s missing — skipping paused event write for trigger %s",
                        org_id,
                        trigger_id,
                    )
                    return {"status": "paused"}
                paused_event = TriggerEvent(
                    organisation_id=org_id,
                    trigger_id=trigger.id,
                    trigger_type="webhook",
                    raw_payload_hash=sha256_hex(raw_body),
                    validation_result="paused",
                )
                session.add(paused_event)
                await session.flush()
                return {"status": "paused"}
            except GuardrailBlockedAtIntakeError as exc:
                # The engine wrote the ``guardrail_blocked`` TriggerEvent and
                # stored the raw payload INSIDE this transaction. Catch here
                # (mirroring the paused pattern) so the transaction COMMITS and
                # the event survives, then surface the 400 after the
                # transaction — the delivery is reject-and-retry, NOT
                # acked-as-accepted, and no run was created.
                guardrail_block_detail = exc.detail
    except TriggerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND) from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND) from exc
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
    except SnapshotLockNotAvailableError:
        _log.info(
            "webhooks.receive_webhook.snapshot_lock_busy trigger=%s pipeline=%s",
            trigger_id,
            trigger.pipeline_id if trigger is not None else None,
        )
        return {"run_id": None, "status": "queued", "detail": "Pipeline busy — queued for retry"}
    except ProgrammingError:
        _log.exception(_CODE_WEBHOOKS_RECEIVE_WEBHOOK)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_WEBHOOKS_RECEIVE_WEBHOOK)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("receive_webhook failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if guardrail_block_detail is not None:
        # A block-action guardrail rejected the delivery at the trigger
        # boundary. The ``guardrail_blocked`` TriggerEvent and the stored raw
        # payload were committed with the transaction above; the delivery is
        # reject-and-retry — never acked, no run, no dedup slot.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=guardrail_block_detail,
        )

    run_id = run.id
    # FAR-213 webhook ack-after-validate semantics: the delivery is validated
    # (including the ingestion guardrail pass) BEFORE a success ack. A
    # guardrail-blocked run (terminal eval_failed / eval_blocked) is created
    # only so the failure is visible in the run list — it is NEVER dispatched
    # and must not get a false "accepted" ack, so ack with a non-success 422.
    # The run + TriggerEvent rows stay committed (the trigger event records the
    # delivery attempt against the blocked run id).
    if run.error_code == "eval_blocked":
        _log.info("webhooks.receive_webhook.guardrail_blocked run=%s", run_id)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Delivery rejected: payload violates a bound guardrail (run created as eval_failed for visibility)",
        )
    background_tasks.add_task(_dispatch_webhook_run, str(run_id), str(org_id))

    return {"run_id": str(run_id), "status": "accepted"}


@router.post(
    "/{trigger_id}/webhook/replay/{event_id}",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"description": "Bad request"},
        401: {"description": "Unauthorized"},
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        422: {"description": "Unprocessable Entity"},
        429: {"description": "Too Many Requests"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("webhooks.replay_webhook")
async def replay_webhook(
    trigger_id: uuid.UUID,
    event_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
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

    trigger: Trigger | None = None
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
            await set_rls_execution_context(session)

            if principal is None:
                # ADR 017: unauthenticated replay requires a valid HMAC signature
                # over the stored payload (same check as receive_webhook).
                hmac_signature = request.headers.get("X-Modulo-Webhook-Secret")
                modulo_timestamp = request.headers.get("X-Modulo-Timestamp")
                ts = verify_timestamp(modulo_timestamp)
                cfg = trigger.config_json or {}
                hmac_secret_raw: str | None = cfg.get("hmac_secret")
                if hmac_secret_raw is None:
                    raise HmacValidationError()
                try:
                    hmac_secret = decode_stored_secret(hmac_secret_raw, get_settings().fernet_key)
                except Exception:
                    _log.exception("webhooks.hmac_secret_decrypt_failed trigger=%s", trigger_id)
                    hmac_secret = hmac_secret_raw
                payload_row = await session.execute(
                    select(WebhookPayload).where(
                        WebhookPayload.trigger_event_id == event_id,
                        WebhookPayload.organisation_id == org_id,
                    )
                )
                stored = payload_row.scalar_one_or_none()
                if stored is None:
                    raise ReplayNotFoundError(event_id)
                if not verify_hmac(stored.raw_body, hmac_secret, hmac_signature, timestamp=ts):
                    raise HmacValidationError()

            try:
                # Pause pre-check AFTER principal auth / trigger load, BEFORE the
                # snapshot — a paused org's replay is dropped with a committed
                # ``paused`` TriggerEvent reusing the ORIGINAL event's hash.
                if trigger.active:
                    await ensure_triggers_resumable(session, org_id, trigger_id=trigger_id, trigger_type="webhook")

                snapshot = await create_snapshot_from_live_graph(
                    session, pipeline_id=trigger.pipeline_id, account_id=None
                )
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
            except TriggersPausedError:
                from modulo.db.models.trigger_event import TriggerEvent as _TriggerEventModel

                # Safe event write: an orphan trigger whose org row was
                # HARD-deleted must not attempt the TriggerEvent INSERT (it
                # would violate the organisations FK -> 503). Fail-closed with
                # no event row and no crash.
                if not await _org_row_exists(session, org_id):
                    _log.warning(
                        "webhooks.replay_webhook: org %s missing — skipping paused event write for trigger %s",
                        org_id,
                        trigger_id,
                    )
                    return {"status": "paused"}
                orig_result = await session.execute(
                    select(_TriggerEventModel).where(
                        _TriggerEventModel.id == event_id,
                        _TriggerEventModel.organisation_id == org_id,
                    )
                )
                orig = orig_result.scalar_one_or_none()
                if orig is None:
                    raise ReplayNotFoundError(event_id) from None
                paused_event = _TriggerEventModel(
                    organisation_id=org_id,
                    trigger_id=trigger.id,
                    trigger_type="webhook",
                    raw_payload_hash=orig.raw_payload_hash,
                    validation_result="paused",
                )
                session.add(paused_event)
                await session.flush()
                return {"status": "paused"}
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND) from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND) from exc
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
    except SnapshotLockNotAvailableError:
        _log.info(
            "webhooks.replay_webhook.snapshot_lock_busy trigger=%s pipeline=%s",
            trigger_id,
            trigger.pipeline_id if trigger is not None else None,
        )
        return {"run_id": None, "status": "queued", "detail": "Pipeline busy — queued for retry"}
    except ProgrammingError:
        _log.exception("webhooks.replay_webhook")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("webhooks.replay_webhook")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("replay_webhook failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    run_id = run.id
    # FAR-213 webhook ack-after-validate semantics (see receive_webhook): a
    # guardrail-blocked replayed delivery is acked with a non-success 422, never
    # a false "accepted" — the run + TriggerEvent rows stay committed.
    if run.error_code == "eval_blocked":
        _log.info("webhooks.replay_webhook.guardrail_blocked run=%s", run_id)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Delivery rejected: payload violates a bound guardrail (run created as eval_failed for visibility)",
        )
    background_tasks.add_task(_dispatch_webhook_run, str(run_id), str(org_id))

    return {"run_id": str(run_id), "status": "accepted"}


@router.post("/cleanup-expired", status_code=status.HTTP_200_OK)
@handle_db_errors("webhooks.cleanup_expired")
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
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("webhooks.cleanup_expired")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("Cleanup job failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cleanup job failed",
        ) from None
    return result
