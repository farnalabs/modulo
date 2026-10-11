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
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.constants import (
    MSG_DB_OPERATION_FAILED,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_INTERNAL_SERVER_ERROR,
    MSG_TRIGGER_NOT_FOUND,
)
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.db_error_reporting import log_service_unavailable
from modulo.api.dependencies import (
    _get_engine,
    get_current_tenant_user_optional,
    get_db_session,
    get_or_create_engine,
    get_system_db_session,
    require_permission,
    system_engine_is_fallback,
)
from modulo.api.routes.runs import pipeline_not_runnable_http
from modulo.api.trigger_busy import BUSY_ACK_DETAIL, record_backpressure_delivery, record_busy_delivery
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import PermissionDenied, assert_org_role
from modulo.auth.secret_storage import decode_stored_secret_scoped
from modulo.core.audit_coverage import audited, audited_system, bind_audit_actor_source, bind_audit_org
from modulo.core.dispatch import dispatch_run
from modulo.core.error_tracking import ErrorIngestionService
from modulo.core.exceptions import PipelineNotRunnableError, SnapshotLockNotAvailableError, TriggersPausedError
from modulo.core.logging_config import org_id_var

# Deprecated private aliases — kept importable so legacy patch targets and
# callers referencing the underscore names keep working (M5 public-API fix).
from modulo.core.trigger_engine import (  # noqa: F401
    CiFailureCoalescedError,
    ConcurrentRunLimitError,
    DuplicateWebhookError,
    EventNotAcceptedError,
    HmacValidationError,
    PipelineBackpressureError,
    PipelineRateLimitError,
    ReplayNotFoundError,
    TimestampExpiredError,
    TriggerBusyError,
    TriggerConfigInvalidError,
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
from modulo.db.crud.trigger import load_trigger_and_org_global
from modulo.db.models.organisation import Organisation
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.webhook import WebhookPayload
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.db.settings_resolver import ensure_triggers_resumable
from modulo.settings import get_settings
from modulo.version import get_version

_CODE_WEBHOOKS_RECEIVE_WEBHOOK = "webhooks.receive_webhook"
_CODE_WEBHOOKS_REPLAY_WEBHOOK = "webhooks.replay_webhook"

_CODE_WEBHOOKS_CLEANUP_EXPIRED = "webhooks.cleanup_expired"

# FAR-1034 handled-no-op ack. Deliberately a 2xx "handled" message, never a
# "rejected" 4xx: the delivery was suppressed, not rejected, so a retry-on-4xx
# sender must NOT loop on the very duplicates the gate exists to quiet.
CI_FAILURE_COALESCED_ACK = (
    "Duplicate CI-failure event for the same PR + head SHA within the "
    "coalesce window — suppressed (no new Branch Fixer run)."
)


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


def _bind_webhook_org_context(request: Request, org_id: uuid.UUID) -> None:
    """Publish the trigger's organisation where ERROR capture can see it (FAR-1484).

    Webhook ingress resolves its organisation ONLY at the trigger bootstrap
    (``load_trigger_and_org_global``) — the unauthenticated HMAC path has no
    principal at all — so without this bind every ERROR logged from that point
    on (the HMAC-secret decrypt failure, snapshot/DB errors, the
    ``db_transient`` 503s of the 2026-09-04 incident) was dropped by
    ``ErrorTrackingLogHandler`` for want of an org.

    Both carriers, same contract as ``bind_principal_context`` (FAR-1417):

    * ``org_id_var`` — read synchronously by the handler inside this request's
      task context, covering the route body AND the ``handle_db_errors``
      wrapper that logs escaping errors.
    * ``request.state.organisation_id`` — the ASGI scope is shared with every
      middleware layer, so an OUTER ``CatchAllMiddleware`` can re-bind from it
      after an unhandled failure.

    Failures BEFORE the bootstrap (bad JSON, system-role fallback, trigger
    not found) have no organisation to resolve and keep the announced
    ``no_org_context`` drop — never fabricate one here.
    """
    org = str(org_id)
    org_id_var.set(org)
    request.state.organisation_id = org
    # FAR-1516: the same published org feeds the actor-less audit dependency —
    # this is the point where a valid trigger has named its tenant.
    bind_audit_org(request, org_id)


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


async def _verify_webhook_auth(
    *,
    request: Request,
    trigger_id: uuid.UUID,
    trigger: Trigger,
    session: AsyncSession,
    org_id: uuid.UUID,
    raw_body: bytes,
    hmac_signature: str | None,
    modulo_timestamp: str,
) -> None:
    """Route-level timestamp + HMAC validation for an incoming webhook.

    Belt-and-braces ahead of the engine's own check. Only triggers configured
    with an ``hmac_secret`` are validated here — HMAC-less triggers accept
    unauthenticated deliveries by design (FAR-1472). Raises the SAME typed
    errors the inline path raised: ``TimestampExpiredError`` (400) and
    ``HmacValidationError`` (401), surfaced by the route's outer handler.

    An HMAC-secret DECRYPT failure falls back to the raw stored secret with the
    exception LOGGED (never silent) — the stored value may itself be the
    plaintext secret, so refusing outright would break legitimate triggers.

    FAR-1516 ordering is preserved: the ``signature_verified`` actor-source
    promotion fires only AFTER the HMAC check passes, and always BEFORE the
    snapshot / dedup / engine work (the run is not created until the delivery
    has authenticated).
    """
    # Route-level timestamp + HMAC validation (belt-and-braces ahead of
    # the engine's own check). Only triggers configured with an
    # hmac_secret are validated here — HMAC-less triggers accept
    # unauthenticated deliveries by design. Failure events for these
    # typed errors are rolled back with the request (documented
    # pre-existing limitation). config_json shape was validated by the
    # shared bootstrap helper (TriggerConfigInvalidError → 400 in the
    # route), so ``.get`` here cannot AttributeError.
    cfg = trigger.config_json or {}
    hmac_secret_raw: str | None = cfg.get("hmac_secret")
    hmac_secret: str | None = None
    if hmac_secret_raw is not None:
        try:
            hmac_secret = await decode_stored_secret_scoped(
                session, hmac_secret_raw, get_settings().fernet_key, org_id=org_id
            )
        except Exception:
            _log.exception("webhooks.hmac_secret_decrypt_failed trigger=%s", trigger_id)
            hmac_secret = hmac_secret_raw
    if hmac_secret is not None:
        ts = verify_timestamp(modulo_timestamp)
        if not verify_hmac(raw_body, hmac_secret, hmac_signature, timestamp=ts):
            raise HmacValidationError
        # FAR-1516: provenance promotion — this delivery authenticated,
        # so record signature_verified instead of the default.
        bind_audit_actor_source(request, "signature_verified")
    else:
        _log.warning(
            "webhooks.receive_webhook: unauthenticated delivery accepted "
            "(no hmac_secret configured) — trigger %s org %s",
            trigger_id,
            org_id,
        )


async def _record_rejected_delivery(
    *,
    kind: Literal["backpressure", "busy"],
    trigger_id: uuid.UUID,
    org_id: uuid.UUID | None,
    raw_body: bytes,
    raw_payload: dict[str, Any],
    reason: str = "",
) -> bool:
    """FAR-604: re-record a refused delivery in a FRESH transaction.

    Covers the two refused-delivery families whose in-request writes do NOT
    survive the refusal: ``busy`` (the engine raises ``TriggerBusyError``
    BEFORE any TriggerEvent is written) and ``backpressure`` (the engine's
    ``backpressure_skipped`` write rolls back with the unwound main
    transaction — deliberately NOT swallowed inside the begin-block, which
    would commit the dedup insert and break the sender-retry contract).

    MUST run AFTER the main request transaction has unwound — the call site
    is the route's outer handler ladder, never the in-transaction block. The
    underlying recorder (``record_busy_delivery`` /
    ``record_backpressure_delivery``) opens its own session from the shared
    engine and never raises; this helper is the same shape.

    Returns ``True`` when a re-record was attempted (the org was resolved),
    ``False`` when there is nothing to record against. Never raises.
    """
    if org_id is None:
        return False
    if kind == "busy":
        await record_busy_delivery(
            trigger_id=trigger_id,
            org_id=org_id,
            trigger_type="webhook",
            payload_hash=sha256_hex(raw_body),
            raw_body=raw_body,
            raw_payload=raw_payload,
        )
    else:
        await record_backpressure_delivery(
            trigger_id=trigger_id,
            org_id=org_id,
            trigger_type="webhook",
            payload_hash=sha256_hex(raw_body),
            reason=reason,
            raw_body=raw_body,
            raw_payload=raw_payload,
        )
    return True


def _dispatch_or_ack(
    *,
    run: Any,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID | None,
    background_tasks: BackgroundTasks,
    guardrail_block_detail: str | None,
    ci_failure_coalesced: bool,
    event_not_accepted: bool,
) -> dict[str, Any]:
    """Post-transaction outcome of a received webhook delivery.

    Runs only AFTER the main transaction has committed (or rolled back) — the
    engine's in-transaction catches above decided the commit. Maps the three
    commit-then-reject/handle outcomes and the FAR-213 ack-after-validate
    semantics onto the HTTP answer:

    * block-action guardrail at intake → 400 reject-and-retry (the
      ``guardrail_blocked`` event + stored payload are already committed);
    * FAR-1034 CI-failure coalesce → 202 handled no-op ack (see
      ``CI_FAILURE_COALESCED_ACK`` for why this is not a 4xx);
    * event type not accepted → 400 reject-and-retry;
    * FAR-213 guardrail-blocked run (terminal ``eval_blocked``) → 422, never a
      false ``accepted`` — the run + TriggerEvent rows stay committed so the
      blocked delivery is visible in the run list;
    * otherwise enqueue the background dispatch and ack 202.

    ``run`` is ``None`` exactly when one of the three flags above fired (no
    run was created); the flag branches return/raise before it is read.
    """
    if guardrail_block_detail is not None:
        # A block-action guardrail rejected the delivery at the trigger
        # boundary. The ``guardrail_blocked`` TriggerEvent and the stored raw
        # payload were committed with the transaction above; the delivery is
        # reject-and-retry — never acked, no run, no dedup slot.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=guardrail_block_detail,
        )
    if ci_failure_coalesced:
        # FAR-1034 handled no-op: the ``coalesced`` TriggerEvent and the
        # coalesce dedup marker row were committed with the transaction above
        # (caught in-transaction). Ack the sender with the handled 2xx — see
        # CI_FAILURE_COALESCED_ACK for why this is not a 4xx.
        _log.info("webhooks.receive_webhook.ci_failure_coalesced trigger=%s", trigger_id)
        return {"run_id": None, "status": "coalesced", "detail": CI_FAILURE_COALESCED_ACK}
    if event_not_accepted:
        # The ``event_type_not_accepted`` TriggerEvent was committed with the
        # transaction above (caught in-transaction, mirroring the guardrail
        # path). The delivery is reject-and-retry — the sender must adjust the
        # payload to match the trigger's acceptance config.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Event type not accepted by trigger configuration",
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


async def _verify_replay_auth(
    *,
    request: Request,
    trigger_id: uuid.UUID,
    trigger: Trigger,
    session: AsyncSession,
    org_id: uuid.UUID,
    event_id: uuid.UUID,
) -> None:
    """ADR 047 unauthenticated-replay auth: HMAC over the STORED payload.

    Replay skips live-payload HMAC (the request body is empty) but an
    unauthenticated caller must still present a valid signature — same
    ``X-Modulo-Webhook-Secret`` + ``X-Modulo-Timestamp`` headers as
    ``receive_webhook``, verified over the STORED raw body of the original
    event. Raises the same typed errors: ``TimestampExpiredError`` (400),
    ``HmacValidationError`` (401, including the no-hmac_secret-configured
    case), ``ReplayNotFoundError`` (404).

    An HMAC-secret decrypt failure falls back to the raw stored secret with
    the exception LOGGED (never silent). FAR-1516: on success the actor
    source is promoted to ``signature_verified`` — same ordering contract as
    ``_verify_webhook_auth`` (promotion only after the check passes, always
    before the snapshot / engine work).
    """
    # ADR 047: unauthenticated replay requires a valid HMAC signature
    # over the stored payload (same check as receive_webhook).
    hmac_signature = request.headers.get("X-Modulo-Webhook-Secret")
    modulo_timestamp = request.headers.get("X-Modulo-Timestamp")
    ts = verify_timestamp(modulo_timestamp)
    # config_json shape was validated by the shared bootstrap
    # helper (TriggerConfigInvalidError → 400 in the route) — this read
    # runs for authenticated replays too, so the guard cannot be
    # skipped on any path.
    cfg = trigger.config_json or {}
    hmac_secret_raw: str | None = cfg.get("hmac_secret")
    if hmac_secret_raw is None:
        raise HmacValidationError
    try:
        hmac_secret = await decode_stored_secret_scoped(
            session, hmac_secret_raw, get_settings().fernet_key, org_id=org_id
        )
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
        raise HmacValidationError
    # FAR-1516: provenance promotion — the replay authenticated by
    # HMAC over the stored payload.
    bind_audit_actor_source(request, "signature_verified")


async def _replay_paused_path(
    *,
    session: AsyncSession,
    trigger_id: uuid.UUID,
    trigger: Trigger,
    org_id: uuid.UUID,
    event_id: uuid.UUID,
) -> dict[str, Any]:
    """Paused-org replay: commit a ``paused`` TriggerEvent and return the ack.

    The paused event reuses the ORIGINAL event's payload hash. Safe-event-write
    contract: an orphan trigger whose org row was HARD-deleted must not attempt
    the TriggerEvent INSERT (it would violate the organisations FK -> 503) —
    fail-closed with no event row and no crash. Runs INSIDE the request
    transaction (called from the in-transaction ``TriggersPausedError`` catch),
    so the paused event commits with it; raises ``ReplayNotFoundError`` when
    the source event itself is gone (the outer ladder maps it to 404).
    """
    if not await _org_row_exists(session, org_id):
        _log.warning(
            "webhooks.replay_webhook: org %s missing — skipping paused event write for trigger %s",
            org_id,
            trigger_id,
        )
        return {"status": "paused"}
    orig_result = await session.execute(
        select(TriggerEvent).where(
            TriggerEvent.id == event_id,
            TriggerEvent.organisation_id == org_id,
        )
    )
    orig = orig_result.scalar_one_or_none()
    if orig is None:
        raise ReplayNotFoundError(event_id) from None
    paused_event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type="webhook",
        raw_payload_hash=orig.raw_payload_hash,
        validation_result="paused",
    )
    session.add(paused_event)
    await session.flush()
    return {"status": "paused"}


async def _replay_busy_path(
    *,
    trigger_id: uuid.UUID,
    trigger: Trigger | None,
    org_id: uuid.UUID | None,
    event_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Concurrent same-trigger replay: record the lock loser, then ack 202.

    The engine serializes on its advisory lock and raises ``TriggerBusyError``
    BEFORE any TriggerEvent is written; the main transaction rolls back, so the
    busy replay is recorded HERE — AFTER the unwind — in a fresh transaction
    (a ``concurrency_limit_reached`` event carrying the ORIGINAL event's
    payload hash). The 2xx ack is honest because of that recording.

    Returns the busy-ack dict, or ``None`` when the org could not be resolved
    (TriggerBusyError can only fire after the bootstrap helper resolved the
    org — the caller then refuses loudly rather than false-acks).
    """
    _log.info(
        "webhooks.replay_webhook.trigger_busy trigger=%s pipeline=%s",
        trigger_id,
        trigger.pipeline_id if trigger is not None else None,
    )
    if org_id is None:
        return None
    await record_busy_delivery(
        trigger_id=trigger_id,
        org_id=org_id,
        trigger_type="webhook",
        source_event_id=event_id,
    )
    return {"run_id": None, "status": "queued", "detail": BUSY_ACK_DETAIL}


def _replay_ack(
    *,
    run: Any,
    org_id: uuid.UUID | None,
    background_tasks: BackgroundTasks,
    event_not_accepted: bool,
) -> dict[str, Any]:
    """Post-transaction outcome of a replayed delivery.

    Mirror of ``_dispatch_or_ack`` for the replay route: event-type rejection
    (400, the ``event_type_not_accepted`` event already committed), the FAR-213
    ack-after-validate guardrail-blocked 422 (run + TriggerEvent rows stay
    committed, never a false ``accepted``), else background dispatch + 202.

    ``run`` is ``None`` exactly when ``event_not_accepted`` fired (no run was
    created); that branch raises before it is read.
    """
    if event_not_accepted:
        # The ``event_type_not_accepted`` TriggerEvent was committed with the
        # transaction above (caught in-transaction, mirroring the guardrail
        # path). The delivery is reject-and-retry — the sender must adjust the
        # payload to match the trigger's acceptance config.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Event type not accepted by trigger configuration",
        )
    run_id = run.id
    # FAR-213 webhook ack-after-validate semantics (see _dispatch_or_ack): a
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


# FAR-1472 exemption (kept in audit_coverage_baseline.txt): the principal here
# is OPTIONAL - HMAC-less triggers accept unauthenticated calls by design, and
# an HMAC-verified caller carries no Modulo credential. audited() takes a
# required principal resolver (it needs an account_id for
# audit_events.actor_user_id) and get_current_tenant_user_optional can return
# None, so there is nothing to attribute the event to. Actor-less variant
# needed to cover webhook ingestion.
@router.post(
    "/{trigger_id}/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    # FAR-1516: HMAC is per-trigger (some triggers are public by design), so the
    # actor-less variant records a SYSTEM actor as unauthenticated by default and
    # the handler promotes to signature_verified once the HMAC check passes.
    dependencies=[Depends(audited_system("webhook_received", "trigger", actor_source="unauthenticated"))],
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
    system_session: AsyncSession = Depends(get_system_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    _engine: AsyncEngine = Depends(_get_engine),
) -> dict[str, Any]:
    """Receive an incoming webhook and enqueue a pipeline run.

    Requires X-Modulo-Timestamp header (Unix seconds, ±300s window).
    Requires X-Modulo-Webhook-Secret header if trigger has hmac_secret configured.

    ADR 047 exempt-channel: this route is CSRF-exempt via the audited
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
    org_id: uuid.UUID | None = None
    guardrail_block_detail: str | None = None
    ci_failure_coalesced = False
    event_not_accepted = False
    # Declared here so the post-transaction helper call can reference it; it
    # stays None exactly when one of the in-transaction catches fired (no run
    # was created) — those paths return/raise before it is read.
    run: Any = None

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

    if system_engine_is_fallback():
        # No modulo_system role provisioned: the BYPASSRLS bootstrap read
        # would silently match zero rows and every delivery would 404. Refuse
        # loudly (same contract as admin_rotation's system-role check) — 503
        # is distinguishable from a genuine 404.
        log_service_unavailable(
            "system_bootstrap_degraded",
            route=_CODE_WEBHOOKS_RECEIVE_WEBHOOK,
            detail="system database not provisioned; trigger delivery unavailable",
            level=logging.ERROR,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="System database not provisioned; trigger delivery unavailable",
        )

    try:
        async with session.begin():
            from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

            # Bootstrap via the shared system-session helper (FAR-523): the
            # trigger read must precede any RLS org context on Postgres; the
            # helper 404s a principal referencing another org's trigger.
            trigger, org_id = await load_trigger_and_org_global(
                system_session, trigger_id, principal.organisation_id if principal else None
            )

            # FAR-1484: the org is resolved HERE (derived from the trigger row,
            # so it always exists once the bootstrap returned). Bind before any
            # further work so every ERROR from this point on — HMAC decrypt,
            # snapshot, DB — is attributed instead of dropped.
            _bind_webhook_org_context(request, org_id)
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)

            # Route-level timestamp + HMAC validation (see
            # _verify_webhook_auth; raises the same typed errors). Ordered
            # after the bootstrap and BEFORE the snapshot/dedup work so an
            # unauthenticated delivery never touches pipeline_snapshots or
            # webhook_dedup_hashes.
            await _verify_webhook_auth(
                request=request,
                trigger_id=trigger_id,
                trigger=trigger,
                session=session,
                org_id=org_id,
                raw_body=raw_body,
                hmac_signature=hmac_signature,
                modulo_timestamp=modulo_timestamp,
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
            except CiFailureCoalescedError:
                # FAR-1034: the engine wrote the ``coalesced`` TriggerEvent
                # (and the coalesce dedup marker row) INSIDE this transaction
                # and then raised, because the delivery was HANDLED as a no-op
                # — NOT rejected. Catching here (mirroring the
                # GuardrailBlockedAtIntakeError pattern) lets the transaction
                # COMMIT so the audit event and the suppression marker survive,
                # then the 202 handled-ack below is returned AFTER the
                # transaction. This is deliberate: unlike the canonical
                # byte-dedup path (DuplicateWebhookError, rolled back so a
                # truly identical re-send re-evaluates), the coalesce marker is
                # the whole point — it MUST persist to keep suppressing within
                # the window.
                ci_failure_coalesced = True
            except GuardrailBlockedAtIntakeError as exc:
                # The engine wrote the ``guardrail_blocked`` TriggerEvent and
                # stored the raw payload INSIDE this transaction. Catch here
                # (mirroring the paused pattern) so the transaction COMMITS and
                # the event survives, then surface the 400 after the
                # transaction — the delivery is reject-and-retry, NOT
                # acked-as-accepted, and no run was created.
                guardrail_block_detail = exc.detail
            except EventNotAcceptedError:
                # The engine wrote the ``event_type_not_accepted``
                # TriggerEvent INSIDE this transaction. Catch here (mirroring
                # the GuardrailBlockedAtIntakeError pattern) so the
                # transaction COMMITS and the audit event survives — an
                # invisible rejection is worse than a typed 400. The 400 is
                # surfaced AFTER the transaction (reject-and-retry, not ack).
                event_not_accepted = True
    except TriggerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_TRIGGER_NOT_FOUND) from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_TRIGGER_NOT_FOUND) from exc
    except TriggerConfigInvalidError as exc:
        _log.warning(
            "webhooks.receive_webhook.trigger_config_invalid",
            extra={"trigger_id": str(trigger_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trigger configuration is invalid",
        ) from exc
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
    except PipelineBackpressureError as exc:
        # FAR-604: the pipeline's pending queue is over the depth/age
        # backpressure limits — refuse the delivery so the sender retries
        # instead of ratcheting the queue further.
        #
        # The engine's ``backpressure_skipped`` event write happened INSIDE the
        # (now unwound) main transaction, so it rolled back with the refusal —
        # re-record the event + raw payload in a FRESH transaction
        # (_record_rejected_delivery, the TriggerBusyError post-unwind pattern)
        # BEFORE raising the 429, or the skip is invisible, contradicting the
        # "auditable, never silent" contract. Deliberately NOT caught inside
        # the begin-block: swallowing there would COMMIT the dedup insert, so
        # a byte-identical sender retry would be deduped instead of
        # re-evaluated.
        _log.info(
            "webhooks.receive_webhook.backpressure_refused trigger=%s pipeline=%s reason=%s",
            trigger_id,
            trigger.pipeline_id if trigger is not None else None,
            exc.reason,
        )
        await _record_rejected_delivery(
            kind="backpressure",
            trigger_id=trigger_id,
            org_id=org_id,
            raw_body=raw_body,
            raw_payload=raw_payload,
            reason=exc.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except PipelineRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except PipelineNotRunnableError as exc:
        # FAR-1528: the delivery is refused at the create_run choke point when
        # the target pipeline is archived/soft-deleted — 409 Conflict, never a
        # generic 500. Deliberately caught OUTSIDE the
        # begin-block (the PipelineBackpressureError pattern): the snapshot
        # created above rolls back with the refusal, and the sender gets a
        # clear, non-retryable answer instead of a crash.
        _log.info(
            "webhooks.receive_webhook.pipeline_not_runnable trigger=%s pipeline=%s state=%s",
            trigger_id,
            exc.pipeline_id,
            exc.state,
        )
        raise pipeline_not_runnable_http(exc) from None
    except SnapshotLockNotAvailableError as exc:
        from modulo.db.crud.pipeline_snapshot import SNAPSHOT_LOCK_ATTEMPTS

        _log.warning(
            "webhooks.receive_webhook.snapshot_lock_busy trigger=%s pipeline=%s attempts=%s (FAR-527)",
            trigger_id,
            trigger.pipeline_id if trigger is not None else None,
            SNAPSHOT_LOCK_ATTEMPTS,
        )
        log_service_unavailable(
            "snapshot_lock_unavailable",
            exc,
            route=_CODE_WEBHOOKS_RECEIVE_WEBHOOK,
            detail=f"snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Pipeline snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts — "
                "retry the webhook delivery"
            ),
        ) from exc
    except TriggerBusyError:
        # Concurrent same-trigger deliveries serialize on the engine's
        # advisory lock. The loser is NOT executed and NOT auto-queued: the
        # engine raises BEFORE any TriggerEvent is written and the main
        # transaction rolls back, so the busy delivery is recorded here —
        # AFTER the unwind — in a fresh transaction
        # (_record_rejected_delivery: a ``concurrency_limit_reached`` event +
        # the raw payload, making it visible in the event log and replayable).
        # The 2xx ack is honest because of that recording: 2xx suppresses
        # sender retries (Slack) BY DESIGN, and replay is the recovery path,
        # not a sender retry.
        _log.info(
            "webhooks.receive_webhook.trigger_busy trigger=%s pipeline=%s",
            trigger_id,
            trigger.pipeline_id if trigger is not None else None,
        )
        recorded = await _record_rejected_delivery(
            kind="busy",
            trigger_id=trigger_id,
            org_id=org_id,
            raw_body=raw_body,
            raw_payload=raw_payload,
        )
        if recorded:
            return {"run_id": None, "status": "queued", "detail": BUSY_ACK_DETAIL}
        # Defensive: TriggerBusyError can only fire after the bootstrap
        # helper resolved the org. If it somehow did not, there is nothing to
        # record against — refuse so the sender retries (never a false ack).
        _log.error("webhooks.receive_webhook.trigger_busy_unresolvable trigger=%s", trigger_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Pipeline busy — delivery not accepted; retry",
            headers={"Retry-After": "5"},
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_WEBHOOKS_RECEIVE_WEBHOOK)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "webhooks.receive_webhook")
        log_service_unavailable(
            "db_transient",
            exc,
            route=_CODE_WEBHOOKS_RECEIVE_WEBHOOK,
            detail="transient database error; webhook delivery failed closed",
        )
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

    # Post-transaction outcome: the three commit-then-reject/handle paths
    # (guardrail-blocked 400, FAR-1034 coalesced 202 ack, event-type 400),
    # the FAR-213 guardrail-blocked 422, or the background dispatch + 202 ack.
    return _dispatch_or_ack(
        run=run,
        trigger_id=trigger_id,
        org_id=org_id,
        background_tasks=background_tasks,
        guardrail_block_detail=guardrail_block_detail,
        ci_failure_coalesced=ci_failure_coalesced,
        event_not_accepted=event_not_accepted,
    )


# FAR-1472 exemption (kept in audit_coverage_baseline.txt): same gap as
# receive_webhook - the replay's principal is optional (an HMAC-only caller
# carries no Modulo credential), so audited(principal_dep=...) has no
# guaranteed actor. Actor-less core variant needed to cover replay.
@router.post(
    "/{trigger_id}/webhook/replay/{event_id}",
    status_code=status.HTTP_202_ACCEPTED,
    # FAR-1516: replay admits either a tenant principal or an HMAC-signed
    # caller — default to unauthenticated provenance and let the handler
    # promote once one of those bases is established.
    dependencies=[Depends(audited_system("webhook_replayed", "trigger", actor_source="unauthenticated"))],
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
@handle_db_errors(_CODE_WEBHOOKS_REPLAY_WEBHOOK)
async def replay_webhook(
    trigger_id: uuid.UUID,
    event_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
    principal: TenantPrincipal | None = Depends(get_current_tenant_user_optional),
    _engine: AsyncEngine = Depends(_get_engine),
) -> dict[str, Any]:
    """Re-fire a webhook run from a previous TriggerEvent log entry.

    Replays the original raw payload through the trigger pipeline, skipping
    HMAC and timestamp validation but preserving dedup and flood protection.

    ADR 047: replay is a mutating run-creation channel and is NOT exempt. A
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
        # FAR-1516: provenance promotion — the caller authenticated as a tenant
        # principal (recorded before the org is resolved, read at teardown).
        bind_audit_actor_source(request, "authenticated")

    trigger: Trigger | None = None
    org_id: uuid.UUID | None = None
    event_not_accepted = False
    # Declared here so the post-transaction helper call can reference it; it
    # stays None exactly when event_not_accepted fired (no run was created) —
    # that path raises before it is read.
    run: Any = None
    if system_engine_is_fallback():
        # No modulo_system role provisioned: the BYPASSRLS bootstrap read
        # would silently match zero rows and every delivery would 404. Refuse
        # loudly (same contract as admin_rotation's system-role check) — 503
        # is distinguishable from a genuine 404.
        log_service_unavailable(
            "system_bootstrap_degraded",
            route=_CODE_WEBHOOKS_REPLAY_WEBHOOK,
            detail="system database not provisioned; trigger delivery unavailable",
            level=logging.ERROR,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="System database not provisioned; trigger delivery unavailable",
        )

    try:
        async with session.begin():
            from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

            # Bootstrap via the shared system-session helper (FAR-523): the
            # trigger read must precede any RLS org context on Postgres; the
            # helper 404s a principal referencing another org's trigger.
            trigger, org_id = await load_trigger_and_org_global(
                system_session, trigger_id, principal.organisation_id if principal else None
            )

            # FAR-1484: the org is resolved HERE (derived from the trigger row,
            # so it always exists once the bootstrap returned). Bind before any
            # further work so every ERROR from this point on — HMAC decrypt,
            # snapshot, DB — is attributed instead of dropped.
            _bind_webhook_org_context(request, org_id)
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)

            if principal is None:
                # ADR 047: unauthenticated replay requires a valid HMAC
                # signature over the STORED payload (see _verify_replay_auth;
                # raises the same typed errors).
                await _verify_replay_auth(
                    request=request,
                    trigger_id=trigger_id,
                    trigger=trigger,
                    session=session,
                    org_id=org_id,
                    event_id=event_id,
                )

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
                # The paused event write commits with THIS transaction (the
                # helper is called from inside the begin-block); see
                # _replay_paused_path for the safe-event-write contract.
                return await _replay_paused_path(
                    session=session,
                    trigger_id=trigger_id,
                    trigger=trigger,
                    org_id=org_id,
                    event_id=event_id,
                )
            except EventNotAcceptedError:
                # The engine wrote the ``event_type_not_accepted``
                # TriggerEvent INSIDE this transaction. Catch here (mirroring
                # the GuardrailBlockedAtIntakeError / TriggersPausedError
                # patterns) so the transaction COMMITS and the audit event
                # survives — an invisible rejection is worse than a typed 400.
                # The 400 is surfaced AFTER the transaction (reject-and-retry,
                # not ack).
                event_not_accepted = True
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_TRIGGER_NOT_FOUND) from exc
    except TriggerInactiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_TRIGGER_NOT_FOUND) from exc
    except PipelineNotRunnableError as exc:
        # FAR-1528: the replay is refused at the create_run choke point when
        # the target pipeline has since been archived/soft-deleted — 409
        # Conflict, never a generic 500. Caught outside the
        # begin-block so the snapshot rolls back with the refusal.
        _log.info(
            "webhooks.replay_webhook.pipeline_not_runnable trigger=%s pipeline=%s state=%s",
            trigger_id,
            exc.pipeline_id,
            exc.state,
        )
        raise pipeline_not_runnable_http(exc) from None
    except TriggerConfigInvalidError as exc:
        _log.warning(
            "webhooks.replay_webhook.trigger_config_invalid",
            extra={"trigger_id": str(trigger_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trigger configuration is invalid",
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
    except SnapshotLockNotAvailableError as exc:
        from modulo.db.crud.pipeline_snapshot import SNAPSHOT_LOCK_ATTEMPTS

        _log.warning(
            "webhooks.replay_webhook.snapshot_lock_busy trigger=%s pipeline=%s attempts=%s (FAR-527)",
            trigger_id,
            trigger.pipeline_id if trigger is not None else None,
            SNAPSHOT_LOCK_ATTEMPTS,
        )
        log_service_unavailable(
            "snapshot_lock_unavailable",
            exc,
            route=_CODE_WEBHOOKS_REPLAY_WEBHOOK,
            detail=f"snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Pipeline snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts — "
                "retry the webhook delivery"
            ),
        ) from exc
    except TriggerBusyError:
        # Concurrent same-trigger replays serialize on the engine's advisory
        # lock; the loser is recorded AFTER the unwind in a fresh transaction
        # (see _replay_busy_path) and only then acked 2xx.
        ack = await _replay_busy_path(
            trigger_id=trigger_id,
            trigger=trigger,
            org_id=org_id,
            event_id=event_id,
        )
        if ack is not None:
            return ack
        # Defensive: TriggerBusyError can only fire after the bootstrap
        # helper resolved the org — refuse loudly rather than false-ack.
        _log.error("webhooks.replay_webhook.trigger_busy_unresolvable trigger=%s", trigger_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Pipeline busy — delivery not accepted; retry",
            headers={"Retry-After": "5"},
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_WEBHOOKS_REPLAY_WEBHOOK)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_WEBHOOKS_REPLAY_WEBHOOK)
        log_service_unavailable(
            "db_transient",
            exc,
            route=_CODE_WEBHOOKS_REPLAY_WEBHOOK,
            detail="transient database error; webhook replay failed closed",
        )
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

    # Post-transaction outcome: the event-type 400, the FAR-213
    # guardrail-blocked 422, or the background dispatch + 202 ack.
    return _replay_ack(
        run=run,
        org_id=org_id,
        background_tasks=background_tasks,
        event_not_accepted=event_not_accepted,
    )


@router.post(
    "/cleanup-expired",
    status_code=status.HTTP_200_OK,
    dependencies=[
        Depends(audited("webhook_events_cleaned_up", "webhook_event", principal_dep=get_current_tenant_user))
    ],
)
@handle_db_errors(_CODE_WEBHOOKS_CLEANUP_EXPIRED)
async def cleanup_expired(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.cleanup"),
) -> dict[str, int]:
    """Delete expired dedup hashes and webhook payloads.

    Acquires a Postgres advisory lock to prevent concurrent cleanup across workers.
    Safe to call from cron every 5 minutes (with a ``runner`` credential).

    ADR 047: swept with ``trigger.cleanup`` (``runner`` minimum) — this route
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
        _log.exception(_CODE_WEBHOOKS_CLEANUP_EXPIRED)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_WEBHOOKS_CLEANUP_EXPIRED)
        log_service_unavailable(
            "db_transient",
            exc,
            route=_CODE_WEBHOOKS_CLEANUP_EXPIRED,
            detail="transient database error; dedup/payload cleanup failed closed",
        )
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
