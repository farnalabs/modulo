"""Shared writer for the ``paused`` TriggerEvent on the delivery intake paths.

The webhook receive path (``webhooks.py``) and the Slack app_mention path
(``slack.py``) both record a ``paused`` TriggerEvent when the org pause
kill-switch refuses a delivery, and both guard that INSERT against an orphan
trigger whose organisation row was HARD-deleted (the INSERT would violate the
organisations FK -> 503). The two hand-copied bodies were byte-identical apart
from ``trigger_type`` and the log scope, so they live here once:

.. code-block:: python

    await write_paused_trigger_event(
        session,
        trigger=trigger,
        trigger_id=trigger_id,
        org_id=org_id,
        raw_body=raw_body,
        trigger_type="webhook",
        log_scope="webhooks.receive_webhook",
    )

The write commits with the CALLER's transaction — it only ``add``s and
``flush``es, never commits — so the paused event rolls with the delivery
transaction exactly as the hand-copied bodies did.

Like its sibling ``_org_settings``, it lives beside the route modules and
imports nothing route-specific, so both routes share it without a cycle.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.trigger_engine import sha256_hex
from modulo.db.models.organisation import Organisation
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent

_log = logging.getLogger(__name__)

# One message for both intake paths; ``log_scope`` keeps the operator-facing
# prefix each route logged before the shared writer existed.
_PAUSED_EVENT_SKIPPED_MESSAGE: Final = "%s: org %s missing — skipping paused event write for trigger %s"


async def org_row_exists(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether the org row still exists.

    Orphan-org guard for the ``paused`` TriggerEvent write: a trigger whose org
    row was HARD-deleted must not attempt the INSERT (it would violate the
    organisations FK -> 503). Fail-closed with no event row and no crash.
    """
    org_exists = await session.execute(select(Organisation.id).where(Organisation.id == org_id))
    return org_exists.scalar_one_or_none() is not None


async def write_paused_trigger_event(
    session: AsyncSession,
    *,
    trigger: Trigger,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    raw_body: bytes,
    trigger_type: str,
    log_scope: str,
) -> None:
    """Commit the ``paused`` TriggerEvent (skipped when the org row is gone)."""
    if not await org_row_exists(session, org_id):
        _log.warning(_PAUSED_EVENT_SKIPPED_MESSAGE, log_scope, org_id, trigger_id)
        return
    paused_event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type=trigger_type,
        raw_payload_hash=sha256_hex(raw_body),
        validation_result="paused",
    )
    session.add(paused_event)
    await session.flush()
