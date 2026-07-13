"""Celery app configuration for Modulo.

Exposes a lazily-initialised ``celery_app`` instance configured from
application settings.  Importing this module no longer requires Celery
to be installed — guarded imports handle the optional dependency.

Run the beat scheduler with::

    celery -A modulo.celery_app beat --loglevel=info

Run a worker with::

    celery -A modulo.celery_app worker --loglevel=info
"""

import logging
from typing import Any

try:
    from celery import Celery
    from celery.signals import task_failure
except ImportError:
    import typing

    if typing.TYPE_CHECKING:
        from celery import Celery
        from celery.signals import task_failure
    Celery = None
    task_failure = None

_log = logging.getLogger(__name__)

_celery_app_instance = None


def get_celery_app() -> Any:
    """Return a configured Celery application instance (lazily built).

    Returns ``None`` when Celery is not installed or when ``redis_url``
    is not configured.
    """
    global _celery_app_instance

    if _celery_app_instance is not None:
        return _celery_app_instance

    if Celery is None:
        _log.warning("Celery is not installed — Celery-based scheduler and workers are unavailable")
        return None

    from modulo.settings import get_settings

    settings = get_settings()
    if not settings.redis_url:
        _log.info("redis_url is not configured — Celery-based scheduler disabled, using in-process scheduler")
        return None

    app = Celery(
        "modulo",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=[
            "modulo.core.cron_scheduler",
            "modulo.core.trigger_engine.polling",
            "modulo.core.reports.scheduler",
            "modulo.core.cleanup_jobs.webhook_dedup_cleanup",
            "modulo.core.notifier.celery_tasks",
        ],
    )

    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        beat_scheduler="modulo.core.composite_scheduler:CompositeScheduler",
    )

    if task_failure is not None:
        from modulo.core.error_tracking.celery_hooks import celery_task_failure_handler

        task_failure.connect(celery_task_failure_handler)

    _celery_app_instance = app
    return app


# Module-level convenience alias for code that calls ``celery_app`` directly.
# Kept for backward compatibility with existing imports like:
#   from modulo.celery_app import celery_app
celery_app = None
