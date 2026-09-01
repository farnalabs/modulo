"""Best-effort recording of trigger deliveries refused as BUSY.

The trigger engine raises ``TriggerBusyError`` from its advisory-lock
acquisition BEFORE any TriggerEvent is written, and the route's main
transaction rolls back — so without a post-unwind write the busy delivery
would vanish entirely (the engine's own contract is "a TriggerEvent is always
written (pass or fail)"). Recording it here is what makes the routes' 2xx ack
honest: senders such as Slack suppress retries on 2xx BY DESIGN, so the
delivery must not be lost — it is recorded in the event log (visible in the
runs/events UI) and, for webhook deliveries, its raw payload is stored so it
can be replayed.
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

# Mirrors the engine's replay-payload TTL (dedup TTL + 1 hour).
_BUSY_PAYLOAD_TTL_SECONDS = 300 + 3600


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
                validation_result=BUSY_VALIDATION_RESULT,
                error_detail=BUSY_ERROR_DETAIL,
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
