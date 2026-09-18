"""Best-effort recording of trigger deliveries refused BEFORE any event survives.

Two refused-delivery families share the post-unwind writer
(:func:`_record_refused_delivery`):

* BUSY — the trigger engine raises ``TriggerBusyError`` from its advisory-lock
  acquisition BEFORE any TriggerEvent is written, and the route's main
  transaction rolls back — so without a post-unwind write the busy delivery
  would vanish entirely (the engine's own contract is "a TriggerEvent is always
  written (pass or fail)"). Recording it here is what makes the routes' 2xx ack
  honest: senders such as Slack suppress retries on 2xx BY DESIGN, so the
  delivery must not be lost — it is recorded in the event log (visible in the
  runs/events UI) and, for webhook deliveries, its raw payload is stored so it
  can be replayed.
* BACKPRESSURE (FAR-604 D3) — the engine writes a ``backpressure_skipped``
  event IN-TRANSACTION before raising ``PipelineBackpressureError``, but the
  route's refusal unwinds that transaction (a deliberate design: swallowing
  inside the begin-block would commit the dedup insert and break the
  sender-retry contract). The route re-records the event + raw payload here,
  AFTER the unwind, so the skip is auditable, never silent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from modulo.api.dependencies import get_or_create_engine
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.models.webhook import WebhookPayload
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# The not-run outcome recorded for a busy delivery. Reuses the existing
# ``concurrency_limit_reached`` vocabulary value (the engine writes the same
# value when the run-level concurrent cap is hit) — no schema change needed.
# The webhook replay path accepts it alongside ``accepted`` so a busy-refused
# delivery can be re-fired from the event log.
BUSY_VALIDATION_RESULT = "concurrency_limit_reached"
BUSY_ERROR_DETAIL = "Trigger busy — concurrent dispatch in progress; delivery not executed"
BUSY_ACK_DETAIL = "Pipeline busy — delivery recorded; replay it from the trigger event log"

# The not-run outcome recorded for a backpressured delivery (FAR-604 D3). The
# engine writes the SAME value in-transaction before raising
# ``PipelineBackpressureError`` — but that transaction rolls back with the
# refusal, so the route re-records the event here post-unwind (auditable,
# never silent).
BACKPRESSURE_VALIDATION_RESULT = "backpressure_skipped"
BACKPRESSURE_ERROR_DETAIL = "Delivery refused: pipeline pending queue over backpressure limits"

# Mirrors the engine's replay-payload TTL (dedup TTL + 1 hour).
_BUSY_PAYLOAD_TTL_SECONDS = 300 + 3600


async def _record_refused_delivery(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    trigger_type: str,
    validation_result: str,
    error_detail: str,
    payload_hash: str | None = None,
    source_event_id: uuid.UUID | None = None,
    raw_body: bytes | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    """Write a refused-delivery ``TriggerEvent`` in a FRESH app-session transaction.

    Shared post-unwind writer for the refused-delivery family (busy —
    :func:`record_busy_delivery` — and backpressure —
    :func:`record_backpressure_delivery`). Must run AFTER the main request
    transaction has unwound. A fresh session from the shared engine, the RLS
    org pinned, and NEVER raises (a recording failure must not turn the
    refusal response into a 500; the log carries the loss instead).

    Args:
        trigger_id: the trigger the delivery targeted (FK-safe: the shared
            bootstrap helper already resolved the row).
        org_id: the trigger's organisation (RLS pin for the fresh session).
        trigger_type: e.g. ``webhook`` or ``slack_app_mention``.
        validation_result: the refused-outcome vocabulary value.
        error_detail: human-readable refusal reason for the event log.
        payload_hash: hash of the refused raw body, when the route holds it.
        source_event_id: for busy REPLAYS — the re-fired original event whose
            payload hash is carried onto the busy audit row.
        raw_body: webhook-delivery raw body; when given, stored as a
            ``WebhookPayload`` linked to the event so the delivery is
            replayable (the engine raises before its own payload store runs).
        raw_payload: parsed JSON payload to store alongside ``raw_body``.
    """
    try:
        factory = async_sessionmaker(
            get_or_create_engine(get_settings()),
            expire_on_commit=False,
            autobegin=False,
        )
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            if payload_hash is None and source_event_id is not None:
                # A busy replay re-fires an original event: carry its payload
                # hash so the busy row is a faithful audit entry.
                orig = await session.execute(
                    select(TriggerEvent.raw_payload_hash).where(
                        TriggerEvent.id == source_event_id,
                        TriggerEvent.organisation_id == org_id,
                    )
                )
                payload_hash = orig.scalar_one_or_none() or ""
            event = TriggerEvent(
                organisation_id=org_id,
                trigger_id=trigger_id,
                trigger_type=trigger_type,
                raw_payload_hash=payload_hash or "",
                validation_result=validation_result,
                error_detail=error_detail,
            )
            session.add(event)
            await session.flush()
            if raw_body is not None:
                session.add(
                    WebhookPayload(
                        organisation_id=org_id,
                        trigger_event_id=event.id,
                        raw_body=raw_body,
                        raw_payload=raw_payload or {},
                        expires_at=datetime.now(UTC) + timedelta(seconds=_BUSY_PAYLOAD_TTL_SECONDS),
                    )
                )
                await session.flush()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "trigger_busy.record_delivery_failed trigger=%s org=%s",
            trigger_id,
            org_id,
        )


async def record_busy_delivery(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    trigger_type: str,
    payload_hash: str | None = None,
    source_event_id: uuid.UUID | None = None,
    raw_body: bytes | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    """Write the busy ``TriggerEvent`` in a FRESH app-session transaction.

    Must run AFTER the main request transaction has unwound: the engine
    raises ``TriggerBusyError`` before writing any event, and the main
    transaction rolls back. Mirrors the dispatch-error ingestion pattern —
    a fresh session from the shared engine, the RLS org pinned, and NEVER
    raises (a recording failure must not turn the busy ack into a 500; the
    log carries the loss instead).

    Args:
        trigger_id: the trigger the delivery targeted (FK-safe: the shared
            bootstrap helper already resolved the row).
        org_id: the trigger's organisation (RLS pin for the fresh session).
        trigger_type: e.g. ``webhook`` or ``slack_app_mention``.
        payload_hash: hash of the refused raw body, when the route holds it.
        source_event_id: for busy REPLAYS — the re-fired original event whose
            payload hash is carried onto the busy audit row.
        raw_body: webhook-delivery raw body; when given, stored as a
            ``WebhookPayload`` linked to the busy event so the delivery is
            replayable (the engine raises before its own payload store runs).
        raw_payload: parsed JSON payload to store alongside ``raw_body``.
    """
    await _record_refused_delivery(
        trigger_id=trigger_id,
        org_id=org_id,
        trigger_type=trigger_type,
        validation_result=BUSY_VALIDATION_RESULT,
        error_detail=BUSY_ERROR_DETAIL,
        payload_hash=payload_hash,
        source_event_id=source_event_id,
        raw_body=raw_body,
        raw_payload=raw_payload,
    )


async def record_backpressure_delivery(
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    trigger_type: str,
    payload_hash: str | None = None,
    reason: str = "",
    raw_body: bytes | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    """Write the ``backpressure_skipped`` TriggerEvent in a FRESH transaction.

    FAR-604 D3 post-unwind recorder: the engine writes the same
    ``backpressure_skipped`` event IN-TRANSACTION before raising
    ``PipelineBackpressureError``, but that write rolls back with the
    refusal — without this re-record the skip would be invisible,
    contradicting the "auditable, never silent" contract. Runs AFTER the main
    request transaction has unwound; never raises (a recording failure must
    not turn the 429 refusal into a 500; the log carries the loss instead).

    Args:
        trigger_id: the trigger the delivery targeted (FK-safe: the shared
            bootstrap helper already resolved the row).
        org_id: the trigger's organisation (RLS pin for the fresh session).
        trigger_type: e.g. ``webhook``.
        payload_hash: hash of the refused raw body, when the route holds it.
        reason: the backpressure reason token (``queue_depth=…`` /
            ``oldest_age=…``) recorded in the event's error_detail.
        raw_body: webhook-delivery raw body; when given, stored as a
            ``WebhookPayload`` linked to the event so the delivery is
            replayable from the event log even though no run was created.
        raw_payload: parsed JSON payload to store alongside ``raw_body``.
    """
    detail = BACKPRESSURE_ERROR_DETAIL
    if reason:
        detail = f"{BACKPRESSURE_ERROR_DETAIL} ({reason})"
    await _record_refused_delivery(
        trigger_id=trigger_id,
        org_id=org_id,
        trigger_type=trigger_type,
        validation_result=BACKPRESSURE_VALIDATION_RESULT,
        error_detail=detail,
        payload_hash=payload_hash,
        raw_body=raw_body,
        raw_payload=raw_payload,
    )
