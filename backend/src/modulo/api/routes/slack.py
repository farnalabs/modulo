"""Slack Events API ``app_mention`` trigger endpoint.

URL: POST /api/v1/triggers/{trigger_id}/slack

Auth: Slack Signs your requests — ``X-Slack-Signature`` (HMAC-SHA256 over
``v0:<timestamp>:<body>`` with the trigger's ``signing_secret``) and
``X-Slack-Request-Timestamp`` (validated within a ±300s replay window).

Two payload classes are handled:

* ``url_verification`` — Slack's initial setup handshake. Returns the echoed
  ``challenge`` value (HTTP 200) after signature verification.
* ``event_callback`` with ``event.type == 'app_mention'`` — the actual
  trigger. Creates a pipeline run and returns HTTP 202.

All delivery attempts are logged as TriggerEvent rows regardless of outcome.
"""

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    _get_engine,
    get_current_tenant_user_optional,
    get_db_session,
    get_or_create_engine,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.core.dispatch import dispatch_run
from modulo.core.error_tracking import ErrorIngestionService
from modulo.core.exceptions import TriggersPausedError
from modulo.core.trigger_engine import (
    DuplicateWebhookError,
    PipelineRateLimitError,
    TriggerEngine,
    TriggerInactiveError,
    TriggerNotFoundError,
    sha256_hex,
)
from modulo.core.trigger_engine.slack_app_mention import (
    SlackAppMentionParseError,
    SlackChallengeNotFoundError,
    SlackEventTypeError,
    SlackSignatureError,
    SlackTimestampExpiredError,
    extract_challenge,
    handle_app_mention,
    verify_slack_signature,
    verify_slack_timestamp,
)
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.db.settings_resolver import ensure_triggers_resumable
from modulo.settings import get_settings
from modulo.version import get_version

_CODE_SLACK_RECEIVE_EVENT = "slack.receive_event"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/triggers", tags=["slack"])

_trigger_engine = TriggerEngine()


async def _org_row_exists(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether the org row still exists (orphan-org guard for the paused
    TriggerEvent write — same contract as the webhook route)."""
    org_exists = await session.execute(select(Organisation.id).where(Organisation.id == org_id))
    return org_exists.scalar_one_or_none() is not None


async def _ingest_slack_dispatch_error(run_id: str, org_id: str, detail: str) -> None:
    """Best-effort error ingestion for a failed background dispatch. Never raises."""
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
                    "message": f"slack trigger dispatch failed for run {run_id}: {detail}",
                    "source": "saq",
                    "context_json": {"function": "slack_dispatch", "run_id": run_id},
                    "environment": getattr(get_settings(), "environment", "development"),
                    "version": get_version(),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("slack.ingest_dispatch_error_failed run=%s", run_id)


async def _dispatch_slack_run(run_id: str, org_id: str) -> None:
    """Dispatch a slack-created run via fail-fast SAQ enqueue (background)."""
    try:
        outcome, _job_id = await dispatch_run(str(run_id), str(org_id), queue="runs", fail_fast=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _ingest_slack_dispatch_error(str(run_id), str(org_id), f"dispatch raised: {exc}")
        return
    if outcome == "enqueue_failed":
        await _ingest_slack_dispatch_error(str(run_id), str(org_id), "SAQ enqueue failed")


async def _load_trigger_and_org(
    session: AsyncSession,
    trigger_id: uuid.UUID,
    principal: TenantPrincipal | None,
) -> tuple[Trigger, uuid.UUID]:
    """Load the trigger row and resolve its org_id (principal or pipeline)."""
    trigger_row = await session.execute(select(Trigger).where(Trigger.id == trigger_id))
    trigger = trigger_row.scalar_one_or_none()
    if trigger is None:
        raise TriggerNotFoundError(trigger_id=trigger_id)

    org_id = principal.organisation_id if principal else None
    if org_id is None:
        pipe = await session.execute(select(Pipeline).where(Pipeline.id == trigger.pipeline_id))
        pipeline = pipe.scalar_one_or_none()
        if pipeline:
            org_id = pipeline.organisation_id
    if org_id is None:
        raise HTTPException(status_code=401, detail="Could not resolve organization")
    return trigger, org_id


@router.post(
    "/{trigger_id}/slack",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"description": "Bad request"},
        401: {"description": "Unauthorized"},
        404: {"description": "Not Found"},
        429: {"description": "Too Many Requests"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_SLACK_RECEIVE_EVENT)
async def receive_slack_event(
    trigger_id: uuid.UUID,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    engine: AsyncEngine = Depends(_get_engine),
) -> dict[str, Any]:
    """Receive a Slack Events API delivery (app_mention) and enqueue a run.

    Requires ``X-Slack-Request-Timestamp`` (Unix seconds, ±300s window) and
    ``X-Slack-Signature`` (constant-time HMAC over ``v0:<ts>:<body>``).

    URL verification: Slack sends ``{"type":"url_verification","challenge":"..."}``
    during setup. The signature is verified first, then the challenge is echoed
    back as ``{"challenge": ...}`` with HTTP 200.

    Returns 202 on event acceptance, 200 for a verification challenge, 401 on
    signature failure, 400 on duplicate/parse failure.
    """
    raw_body = await request.body()
    slack_signature = request.headers.get("X-Slack-Signature")
    slack_timestamp = request.headers.get("X-Slack-Request-Timestamp")

    try:
        raw_payload: dict[str, Any] = await request.json()
        if not isinstance(raw_payload, dict):
            raise TypeError("not a JSON object")
    except Exception as exc:
        _log.exception(_CODE_SLACK_RECEIVE_EVENT)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object",
        ) from exc

    try:
        async with session.begin():
            trigger, org_id = await _load_trigger_and_org(session, trigger_id, principal)
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)

            cfg = trigger.config_json or {}
            signing_secret: str | None = cfg.get("signing_secret")

            # Route-level signature + timestamp validation (belt-and-braces ahead
            # of the engine's own check — required for the challenge handshake).
            if signing_secret is None:
                raise SlackSignatureError("Slack signing secret is not configured")
            verify_slack_timestamp(slack_timestamp)
            if not verify_slack_signature(raw_body, signing_secret, slack_timestamp, slack_signature):
                raise SlackSignatureError("Slack X-Slack-Signature is missing or invalid")

            # URL verification handshake — echo the challenge back.
            if raw_payload.get("type") == "url_verification":
                try:
                    challenge = extract_challenge(raw_payload)
                except SlackChallengeNotFoundError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid URL verification payload",
                    ) from exc
                response.status_code = status.HTTP_200_OK
                return {"challenge": challenge}

            try:
                # Pause pre-check BEFORE the snapshot and dedup work.
                if trigger.active:
                    await ensure_triggers_resumable(
                        session, org_id, trigger_id=trigger_id, trigger_type="slack_app_mention"
                    )

                from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

                snapshot = await create_snapshot_from_live_graph(
                    session, pipeline_id=trigger.pipeline_id, account_id=None
                )
                if snapshot is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to create pipeline snapshot for slack trigger",
                    )

                run, _, _input_payload = await handle_app_mention(
                    session,
                    trigger_id=trigger_id,
                    org_id=org_id,
                    raw_body=raw_body,
                    raw_payload=raw_payload,
                    slack_signature=slack_signature,
                    slack_timestamp=slack_timestamp,
                    snapshot_id=snapshot.id,
                )
            except TriggersPausedError:
                if not await _org_row_exists(session, org_id):
                    _log.warning(
                        "slack.receive_event: org %s missing — skipping paused event write for trigger %s",
                        org_id,
                        trigger_id,
                    )
                    return {"status": "paused"}
                paused_event = TriggerEvent(
                    organisation_id=org_id,
                    trigger_id=trigger.id,
                    trigger_type="slack_app_mention",
                    raw_payload_hash=sha256_hex(raw_body),
                    validation_result="paused",
                )
                session.add(paused_event)
                await session.flush()
                return {"status": "paused"}
    except TriggerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except SlackTimestampExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Slack-Request-Timestamp is outside the ±300s replay window",
        ) from exc
    except SlackSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Slack signature verification failed",
        ) from exc
    except SlackEventTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except SlackAppMentionParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed app_mention payload",
        ) from exc
    except DuplicateWebhookError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate Slack event",
        ) from exc
    except PipelineRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except ProgrammingError:
        _log.exception(_CODE_SLACK_RECEIVE_EVENT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SLACK_RECEIVE_EVENT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("receive_slack_event failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    run_id = run.id
    background_tasks.add_task(_dispatch_slack_run, str(run_id), str(org_id))

    return {"run_id": str(run_id), "status": "accepted"}
