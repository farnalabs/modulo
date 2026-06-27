"""Celery app configuration for Modulo.

Exposes a shared ``celery_app`` instance configured from application settings.
Run the beat scheduler with::

    celery -A modulo.celery_app beat --loglevel=info

Run a worker with::

    celery -A modulo.celery_app worker --loglevel=info
"""

from celery import Celery

from modulo.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "modulo",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["modulo.core.cron_scheduler", "modulo.core.trigger_engine.polling"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_scheduler="modulo.core.composite_scheduler:CompositeScheduler",
)
