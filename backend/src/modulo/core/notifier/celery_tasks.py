"""Celery task for notification dispatch with retry and dead-letter tracking.

This module defines the ``DispatchNotificationTask`` and a convenience
``enqueue_dispatch`` function that the ``Notifier`` calls when Celery mode
is enabled.

Usage
-----
    from modulo.core.notifier.celery_tasks import enqueue_dispatch

    await enqueue_dispatch(org_id, event_type, payload, run_id=run_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from celery import Celery, Task  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import create_async_engine

from modulo.core.notifier import MAX_RETRIES, Notifier
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_ENGINE: Any = None


def _get_engine() -> Any:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(get_settings().database_url)
    return _ENGINE


# ---------------------------------------------------------------------------
# Lazy Celery app reference (same pattern as cron_scheduler / polling)
# ---------------------------------------------------------------------------

_APP: Celery | None = None


def get_celery_app() -> Celery:
    global _APP
    if _APP is None:
        from modulo.celery_app import celery_app as app

        _APP = app
    return _APP


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


class DispatchNotificationTask(Task):  # type: ignore[misc]
    """Celery task that dispatches a notification event to subscribed endpoints.

    Runs inside a Celery worker process. Opens its own DB engine and
    creates a ``Notifier`` instance to perform the dispatch.

    Retries up to ``MAX_RETRIES`` times with a 5-second default delay.
    """

    name = "modulo.notifier.dispatch"
    autoretry_for = (Exception,)
    max_retries = MAX_RETRIES
    default_retry_delay = 5

    def run(
        self,
        org_id: str,
        event_type: str,
        payload_json: str,
        run_id: str | None = None,
        retain_payload: bool = False,
        team_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Dispatch a notification event via the Notifier (sync Celery entry)."""
        return asyncio.run(
            _dispatch_notification(
                org_id=uuid.UUID(org_id),
                event_type=event_type,
                payload=json.loads(payload_json),
                run_id=uuid.UUID(run_id) if run_id else None,
                retain_payload=retain_payload,
                team_id=uuid.UUID(team_id) if team_id else None,
            )
        )


async def _dispatch_notification(
    *,
    org_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    run_id: uuid.UUID | None = None,
    retain_payload: bool = False,
    team_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Core dispatch logic — runs inside ``asyncio.run()`` inside the Celery task."""
    settings = get_settings()
    engine = _get_engine()
    notifier = Notifier(engine, settings.fernet_key)
    results = await notifier.dispatch_event(
        org_id,
        event_type,
        payload,
        run_id=run_id,
        retain_payload=retain_payload,
        team_id=team_id,
    )
    return [
        {
            "endpoint_id": str(r.endpoint_id),
            "status": r.status,
            "attempt_count": r.attempt_count,
            "response_code": r.response_code,
            "last_error": r.last_error,
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Public API — enqueues or runs inline
# ---------------------------------------------------------------------------


async def enqueue_dispatch(
    org_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: uuid.UUID | None = None,
    retain_payload: bool = False,
    team_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Enqueue a notification dispatch to Celery, or run inline if Celery is unavailable.

    This is the entry point called by ``Notifier.dispatch_event()`` when
    Celery mode is enabled.
    """
    try:
        app = get_celery_app()
        app.send_task(
            "modulo.notifier.dispatch",
            args=[
                str(org_id),
                event_type,
                json.dumps(payload, default=str),
                str(run_id) if run_id else None,
                retain_payload,
                str(team_id) if team_id else None,
            ],
        )
        _log.debug(
            "notifier.enqueued_celery_task",
            extra={"event_type": event_type, "org_id": str(org_id)},
        )
        return [
            {
                "endpoint_id": "celery-enqueued",
                "status": "enqueued",
                "attempt_count": 0,
                "response_code": None,
                "last_error": None,
            }
        ]
    except Exception:
        _log.warning(
            "notifier.celery_unavailable_falling_back_to_inline",
            extra={"event_type": event_type, "org_id": str(org_id)},
        )
        settings = get_settings()
        engine = _get_engine()
        notifier = Notifier(engine, settings.fernet_key)
        return await notifier.dispatch_event(
            org_id,
            event_type,
            payload,
            run_id=run_id,
            retain_payload=retain_payload,
            team_id=team_id,
        )
