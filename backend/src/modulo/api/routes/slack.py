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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.db_error_reporting import log_service_unavailable
from modulo.api.dependencies import (
    _get_engine,
    get_current_tenant_user_optional,
    get_db_session,
    get_or_create_engine,
    get_system_db_session,
    system_engine_is_fallback,
)
from modulo.api.routes._trigger_events import write_paused_trigger_event
from modulo.api.routes.runs import pipeline_not_runnable_http
from modulo.api.trigger_busy import BUSY_ACK_DETAIL, _record_refused_delivery, record_busy_delivery
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import SecretStorageError
from modulo.core.audit_coverage import audited_system, bind_audit_org
from modulo.core.dispatch import dispatch_run
from modulo.core.error_tracking import ErrorIngestionService
from modulo.core.exceptions import (
    PipelineNotRunnableError,
    SnapshotLockNotAvailableError,
    TriggersPausedError,
)
from modulo.core.run_provenance import run_provenance_fields
from modulo.core.trigger_engine import (
    DuplicateWebhookError,
    PipelineRateLimitError,
    TriggerBusyError,
    TriggerConfigInvalidError,
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
from modulo.core.trigger_secrets import resolve_trigger_secret_scoped
from modulo.db.crud.pipeline_snapshot import SNAPSHOT_LOCK_ATTEMPTS, create_snapshot_from_live_graph
from modulo.db.crud.trigger import load_trigger_and_org_global
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.db.settings_resolver import ensure_triggers_resumable
from modulo.settings import get_settings
from modulo.version import get_version

_CODE_SLACK_RECEIVE_EVENT = "slack.receive_event"

# FAR-1916 (slack mirror): the refused-delivery outcome recorded when an
# app_mention delivery hits the pipeline's configured rate limit. Reuses the
# engine's own ``rate_limited`` vocabulary value (the engine writes it
# in-transaction at both raise sites, and that transaction rolls back with the
# refusal), so the refusal is stored with its raw payload and needs no schema
# change. Mirrors the webhook receive path's constants exactly.
_RATE_LIMIT_REFUSAL_VALIDATION_RESULT = "rate_limited"
_RATE_LIMIT_REFUSAL_ERROR_DETAIL = "Pipeline rate limit reached — delivery not executed"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/triggers", tags=["slack"])

_trigger_engine = TriggerEngine()


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


@asynccontextmanager
async def _map_trigger_delivery_errors(*, trigger_id: uuid.UUID) -> AsyncIterator[None]:
    """Map the trigger/signature error family shared by both delivery stages.

    The bootstrap+verify stage (:func:`_verify_slack_delivery`) and the
    in-transaction dispatch stage (:func:`_run_slack_pipeline`) both wrap work
    that can raise the same typed errors — the bootstrap helper 404s/400s/401s,
    and the engine's mid-dispatch re-read raises the same family. Defining the
    ladder ONCE keeps the two stages byte-identical and avoids the duplicated
    code block the SonarCloud duplication gate rejected.
    """
    try:
        yield
    except TriggerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found") from exc
    except TriggerConfigInvalidError as exc:
        _log.warning(
            "slack.receive_event.trigger_config_invalid",
            extra={"trigger_id": str(trigger_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trigger configuration is invalid",
        ) from exc
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


async def _verify_slack_delivery(
    *,
    session: AsyncSession,
    system_session: AsyncSession,
    request: Request,
    trigger_id: uuid.UUID,
    principal: TenantPrincipal | None,
    raw_body: bytes,
    slack_signature: str | None,
    slack_timestamp: str | None,
) -> tuple[Trigger, uuid.UUID]:
    """Bootstrap the trigger row and verify the Slack delivery signature.

    Runs inside the route's open transaction. The trigger read must precede
    any RLS org context on Postgres (FAR-523 bootstrap helper); the org
    context is then pinned and the route-level timestamp + signature
    validation runs ahead of any snapshot/dedup work so an unauthenticated
    delivery never touches pipeline tables. FAR-1516 ordering is preserved:
    the ``signature_verified`` audit actor-source promotion fires only AFTER
    the check passes.

    The trigger/signature error family is mapped by the shared
    :func:`_map_trigger_delivery_errors` context manager (the same ladder the
    dispatch stage uses); the ProgrammingError/SQLAlchemyError/HTTPException/
    Exception tail stays on the route (post-unwind).
    """
    async with _map_trigger_delivery_errors(trigger_id=trigger_id):
        # Bootstrap via the shared system-session helper (FAR-523): the
        # trigger read must precede any RLS org context on Postgres; the
        # helper 404s a principal referencing another org's trigger.
        trigger, org_id = await load_trigger_and_org_global(
            system_session, trigger_id, principal.organisation_id if principal else None
        )
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)

        # config_json shape was validated by the shared bootstrap helper
        # (TriggerConfigInvalidError → 400 by the shared error map), so ``.get``
        # here cannot AttributeError on external ingress.
        cfg = trigger.config_json or {}
        # FAR-1732/1735: the stored signing_secret is a Fernet token (this
        # route's own path encrypts at write). The shared resolver decrypts
        # decryptable values via the scope-bound helper and accepts a
        # NON-decryptable, NON-token value as legacy pre-0306 plaintext — a
        # genuinely plaintext value is returned UNCHANGED by the resolver
        # (warn-only shim); it does NOT raise (only scope/key/decrypt errors
        # do).
        signing_secret: str | None
        signing_secret_raw = cfg.get("signing_secret")
        if signing_secret_raw is None:
            signing_secret = None
        else:
            try:
                signing_secret = await resolve_trigger_secret_scoped(
                    session,
                    stored_raw=signing_secret_raw,
                    fernet_key=get_settings().fernet_key,
                    org_id=org_id,
                    trigger_id=trigger_id,
                    config_key="signing_secret",
                    context="slack_route",
                )
            except SecretStorageError:
                # Fail closed: ciphertext we cannot decrypt must never be
                # sent to Slack's signature check as if it were the secret.
                _log.exception("slack_route.signing_secret_decrypt_failed trigger=%s", trigger_id)
                raise SlackSignatureError("Slack signing secret decryption failed") from None

        # Route-level signature + timestamp validation (belt-and-braces ahead
        # of the engine's own check — required for the challenge handshake).
        if signing_secret is None:
            raise SlackSignatureError("Slack signing secret is not configured")
        verify_slack_timestamp(slack_timestamp)
        if not verify_slack_signature(raw_body, signing_secret, slack_timestamp, slack_signature):
            raise SlackSignatureError("Slack X-Slack-Signature is missing or invalid")
        # FAR-1516: signature verified — publish the trigger's org so the
        # audit event records signature_verified provenance for a real tenant.
        bind_audit_org(request, org_id)
        return trigger, org_id


async def _create_slack_snapshot(
    *,
    session: AsyncSession,
    trigger: Trigger,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
) -> PipelineSnapshot:
    """Create the run's pipeline snapshot (pause pre-check first).

    Runs inside the route's open transaction. The pause pre-check fires
    BEFORE the snapshot work so a paused delivery never touches
    ``pipeline_snapshots``; ``TriggersPausedError`` propagates to
    ``_run_slack_pipeline``'s in-transaction arm, which writes the paused
    TriggerEvent (when the org row still exists) and commits it with this
    transaction.
    """
    # Pause pre-check BEFORE the snapshot and dedup work.
    if trigger.active:
        await ensure_triggers_resumable(session, org_id, trigger_id=trigger_id, trigger_type="slack_app_mention")

    snapshot = await create_snapshot_from_live_graph(session, pipeline_id=trigger.pipeline_id, account_id=None)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create pipeline snapshot for slack trigger",
        )
    return snapshot


async def _record_paused_slack_delivery(
    *,
    session: AsyncSession,
    trigger: Trigger,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    raw_body: bytes,
) -> None:
    """Write the paused TriggerEvent; commits with the caller's transaction.

    Safe-event-write guard (same contract as the webhook route): an orphan
    trigger whose org row was HARD-deleted must not attempt the TriggerEvent
    INSERT (it would violate the organisations FK -> 503). Fail-closed with
    no event row and no crash.

    The shared writer in :mod:`modulo.api.routes._trigger_events` holds that
    logic once for both intake paths; only the trigger type and log scope are
    Slack-specific here.
    """
    await write_paused_trigger_event(
        session,
        trigger=trigger,
        trigger_id=trigger_id,
        org_id=org_id,
        raw_body=raw_body,
        trigger_type="slack_app_mention",
        log_scope="slack.receive_event",
    )


async def _run_slack_pipeline(
    *,
    session: AsyncSession,
    trigger: Trigger,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    raw_body: bytes,
    raw_payload: dict[str, Any],
    slack_signature: str | None,
    slack_timestamp: str | None,
) -> Run | None:
    """Dispatch an authenticated Slack app_mention delivery (in-transaction).

    Runs inside the route's open transaction AFTER signature verification.
    Returns the created ``Run``, or ``None`` when the org pause kill-switch
    fired (the paused TriggerEvent — when writable — commits with this
    transaction).

    The trigger/signature error family is mapped by the shared
    :func:`_map_trigger_delivery_errors` context manager (the same ladder the
    bootstrap stage uses). Three families deliberately PROPAGATE to the route's
    ladder instead of being caught here:

    * ``TriggerBusyError`` — the engine raises BEFORE any TriggerEvent is
      written and the main transaction must ROLL BACK; the route records the
      busy delivery in a fresh transaction AFTER the unwind (a return from
      inside this transaction would commit the snapshot instead).
    * ``PipelineRateLimitError`` — the engine writes its ``rate_limited``
      TriggerEvent INSIDE this transaction before raising, so that write rolls
      back with the refusal; the route re-records the refusal post-unwind in a
      fresh transaction (FAR-1916 slack mirror), never an in-transaction arm.
    * the ``ProgrammingError`` / ``SQLAlchemyError`` / ``HTTPException`` /
      ``Exception`` tail — same post-unwind placement as the original
      route-level ladder (rollback first, then the mapped response).
    """
    try:
        async with _map_trigger_delivery_errors(trigger_id=trigger_id):
            snapshot = await _create_slack_snapshot(
                session=session, trigger=trigger, trigger_id=trigger_id, org_id=org_id
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
        return run
    except TriggersPausedError:
        # The SINGLE writer of the ``paused`` TriggerEvent; it commits with
        # this transaction (deliberately NOT after the unwind).
        await _record_paused_slack_delivery(
            session=session, trigger=trigger, trigger_id=trigger_id, org_id=org_id, raw_body=raw_body
        )
        return None
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
    # EventNotAcceptedError is deliberately not caught here: the Slack path
    # (handle_app_mention) never calls _enforce_event_acceptance, so this
    # error cannot be raised from the Slack trigger route today.
    # PipelineRateLimitError is deliberately not caught here (FAR-1916 slack
    # mirror): the engine writes its ``rate_limited`` TriggerEvent INSIDE this
    # transaction before raising, so catching it inside the begin-block would
    # either commit that write with the unwound transaction or lose it. The
    # route's outer ladder re-records the refusal in a FRESH transaction AFTER
    # the unwind and only then raises the 429.
    except PipelineNotRunnableError as exc:
        # FAR-1528: the app-mention delivery is refused at the create_run
        # choke point when the target pipeline is archived/soft-deleted — 409
        # Conflict, never a generic 500. The HTTPException raised here
        # unwinds the route's transaction, so the snapshot rolls back with
        # the refusal (same rollback as the original catch-outside-begin arm).
        _log.info(
            "slack.receive_event.pipeline_not_runnable trigger=%s pipeline=%s state=%s",
            trigger_id,
            exc.pipeline_id,
            exc.state,
        )
        raise pipeline_not_runnable_http(exc) from None
    except SnapshotLockNotAvailableError as exc:
        # FAR-527 parity: the app-mention snapshot creation can lose the
        # advisory-lock race under concurrent deliveries. Surface it as a
        # retryable 503, never a generic 500 (matches the webhook + MCP paths).
        _log.warning(
            "slack.receive_event.snapshot_lock_busy trigger=%s pipeline=%s attempts=%s (FAR-527)",
            trigger_id,
            trigger.pipeline_id,
            SNAPSHOT_LOCK_ATTEMPTS,
        )
        log_service_unavailable(
            "snapshot_lock_unavailable",
            exc,
            route=_CODE_SLACK_RECEIVE_EVENT,
            detail=f"snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Pipeline snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts — retry the Slack delivery"
            ),
        ) from exc


async def _ack_slack_busy_delivery(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID | None,
    raw_body: bytes,
) -> dict[str, Any]:
    """Record a refused busy delivery post-unwind and build the 202 ack.

    MUST run AFTER the route transaction has unwound: the engine raises
    ``TriggerBusyError`` before writing any event and the main transaction
    rolls back, so the busy delivery is recorded here in a fresh transaction
    (``record_busy_delivery`` opens its own session and never raises). Slack
    suppresses retries on 2xx BY DESIGN, so the recording is what makes the
    202 ack honest — the delivery is visible in the event log, never
    silently lost.
    """
    if org_id is not None:
        await record_busy_delivery(
            trigger_id=trigger_id,
            org_id=org_id,
            trigger_type="slack_app_mention",
            payload_hash=sha256_hex(raw_body),
        )
        return {"run_id": None, "status": "queued", "detail": BUSY_ACK_DETAIL}
    # Defensive: TriggerBusyError can only fire after the bootstrap
    # helper resolved the org — refuse loudly rather than false-ack.
    _log.error("slack.receive_event.trigger_busy_unresolvable trigger=%s", trigger_id)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Pipeline busy — delivery not accepted; retry",
        headers={"Retry-After": "5"},
    ) from None


async def _record_slack_rate_limit_refusal(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID | None,
    raw_body: bytes,
    raw_payload: dict[str, Any],
) -> None:
    """Re-record an app_mention refused by the pipeline's configured rate limit.

    MUST run AFTER the route transaction has unwound: the engine writes its
    ``rate_limited`` TriggerEvent INSIDE that transaction at both raise sites,
    so the write rolls back with the refusal (FAR-1916 slack mirror). Records
    the refusal + raw payload in a FRESH transaction via the shared
    ``_record_refused_delivery`` writer (the same post-unwind recorder the
    webhook receive path uses), or a rate-limited delivery is refused but
    invisible in the event log.

    ``org_id`` is always resolved by the time the engine can raise
    ``PipelineRateLimitError`` (it runs after ``_verify_slack_delivery``), so
    the guard is defensive only: with no tenant to scope the event to there is
    nothing to record, and the recording is best-effort — log the loss rather
    than let it turn the refusal response into a 500.
    """
    if org_id is None:
        _log.error("slack.receive_event.rate_limited_unresolvable trigger=%s", trigger_id)
        return
    await _record_refused_delivery(
        trigger_id=trigger_id,
        org_id=org_id,
        trigger_type="slack_app_mention",
        validation_result=_RATE_LIMIT_REFUSAL_VALIDATION_RESULT,
        error_detail=_RATE_LIMIT_REFUSAL_ERROR_DETAIL,
        payload_hash=sha256_hex(raw_body),
        raw_body=raw_body,
        raw_payload=raw_payload,
    )


# FAR-1472 exemption (kept in audit_coverage_baseline.txt): the principal here
# is OPTIONAL - the event authenticates with Slack's X-Slack-Signature, not a
# Modulo credential, so there is no guaranteed actor for
# audited(principal_dep=...) to resolve. Actor-less core variant needed to
# cover Slack event ingestion.
@router.post(
    "/{trigger_id}/slack",
    status_code=status.HTTP_202_ACCEPTED,
    # FAR-1516: Slack ingress has no tenant principal — the actor-less variant
    # records a SYSTEM actor; the handler publishes the trigger's org only
    # AFTER the Slack signature verifies, so a forged delivery is never
    # recorded as signature_verified.
    dependencies=[
        Depends(
            audited_system(
                "slack_event_received",
                "trigger",
                actor_source="signature_verified",
            )
        )
    ],
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
    system_session: AsyncSession = Depends(get_system_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    _engine: AsyncEngine = Depends(_get_engine),
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

    if system_engine_is_fallback():
        # No modulo_system role provisioned: the BYPASSRLS bootstrap read
        # would silently match zero rows and every delivery would 404. Refuse
        # loudly (same contract as admin_rotation's system-role check) — 503
        # is distinguishable from a genuine 404.
        _log.error("slack.system_bootstrap_degraded")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="System database not provisioned; trigger delivery unavailable",
        )

    org_id: uuid.UUID | None = None
    # Declared here so the post-transaction busy-ack path and the accepted
    # ack below can reference it; it stays None exactly when an in-transaction
    # path returned early (challenge / paused / busy) — those paths return
    # before it is read.
    run: Any = None
    try:
        async with session.begin():
            # Bootstrap + fail-closed signature verification (_verify_slack_delivery
            # maps TriggerNotFoundError/TriggerInactiveError -> 404,
            # TriggerConfigInvalidError -> 400, SlackTimestampExpiredError -> 400,
            # SlackSignatureError -> 401 — the original inline ladder's order,
            # preserved as a verbatim subsequence).
            trigger, org_id = await _verify_slack_delivery(
                session=session,
                system_session=system_session,
                request=request,
                trigger_id=trigger_id,
                principal=principal,
                raw_body=raw_body,
                slack_signature=slack_signature,
                slack_timestamp=slack_timestamp,
            )

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

            run = await _run_slack_pipeline(
                session=session,
                trigger=trigger,
                trigger_id=trigger_id,
                org_id=org_id,
                raw_body=raw_body,
                raw_payload=raw_payload,
                slack_signature=slack_signature,
                slack_timestamp=slack_timestamp,
            )
            if run is None:
                # Org pause kill-switch — the paused TriggerEvent (when
                # writable) was committed with this transaction.
                return {"status": "paused"}
    except TriggerBusyError:
        # Concurrent same-trigger deliveries serialize on the engine's
        # advisory lock. The loser is NOT executed and NOT auto-queued: the
        # engine raises BEFORE any TriggerEvent is written and the main
        # transaction rolls back, so the busy delivery is recorded here —
        # AFTER the unwind — in a fresh transaction (a
        # ``concurrency_limit_reached`` event). Slack suppresses retries on
        # 2xx BY DESIGN, so the recording is what makes the 202 ack honest —
        # the delivery is visible in the event log, never silently lost.
        _log.info("slack.receive_event.trigger_busy trigger=%s", trigger_id)
        return await _ack_slack_busy_delivery(trigger_id=trigger_id, org_id=org_id, raw_body=raw_body)
    except PipelineRateLimitError as exc:
        # FAR-1916 (slack mirror): the pipeline's configured rate limit refused
        # the app_mention delivery. The engine writes the ``rate_limited``
        # TriggerEvent INSIDE the main transaction (both raise sites), so that
        # write rolls back with the unwound transaction — re-record the refusal
        # + raw payload in a FRESH transaction AFTER the unwind (via the
        # ``_record_slack_rate_limit_refusal`` helper wrapping the shared
        # ``_record_refused_delivery`` writer the webhook receive path uses), or
        # a rate-limited delivery is refused but invisible in the event log.
        # Slack app_mention deliveries are not
        # replayable, so there is no ``source_event_id`` to forward: the route
        # passes the raw body + parsed payload it already holds. The 429
        # status/detail contract is unchanged, and unlike the busy refusal this
        # arm has never carried a Retry-After — this fix deliberately does not
        # add one.
        _log.info(
            "slack.receive_event.rate_limited trigger=%s pipeline=%s",
            trigger_id,
            exc.pipeline_id,
        )
        await _record_slack_rate_limit_refusal(
            trigger_id=trigger_id,
            org_id=org_id,
            raw_body=raw_body,
            raw_payload=raw_payload,
        )
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "slack.receive_slack_event")
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

    # FAR-1141 / ADR-042: the ack is a claim-ready run surface, so it carries the
    # run's execution origin ('dispatched' / NULL) — composed from the shared
    # run-provenance serializer (FAR-1565), never a hand-rolled copy.
    return {
        "run_id": str(run_id),
        "status": "accepted",
        **run_provenance_fields(run),
    }
