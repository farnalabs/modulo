"""Celery app configuration for Modulo.

The ``celery_app`` module-level attribute is used by the Celery CLI::

    celery -A modulo.celery_app beat --loglevel=info
    celery -A modulo.celery_app worker --loglevel=info

When Redis is not configured, ``celery_app`` is ``None`` and cron/polling
triggers use in-process asyncio schedulers (started automatically by the
application lifespan).
"""

import logging

from modulo.settings import get_settings

_log = logging.getLogger(__name__)


def _create_celery_app():
    """Initialise and return a Celery app, or None if Redis is unavailable."""
    settings = get_settings()
    if not settings.redis_url:
        return None

    try:
        from celery import Celery

        app = Celery(
            "modulo",
            broker=settings.redis_url,
            backend=settings.redis_url,
            include=["modulo.core.cron_scheduler", "modulo.core.trigger_engine.polling"],
        )
        app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            result_serializer="json",
            timezone="UTC",
            enable_utc=True,
            beat_scheduler="modulo.core.cron_scheduler:DatabaseCronScheduler",
        )
        _log.info("Celery app initialised with Redis at %s", settings.redis_url)
        return app
    except ImportError:
        _log.warning("Celery package not installed — run `pip install modulo[redis]` for Celery support")
        return None
    except Exception as exc:
        _log.warning("Failed to initialise Celery app: %s", exc)
        return None


# Module-level attribute for Celery CLI compatibility.
# Lazy-init: the first access creates the app if Redis is configured.
celery_app = _create_celery_app()


def get_celery_app():
    """Return the shared Celery app, or ``None`` if Redis is not configured.

    Unlike the module-level ``celery_app`` (initialised once at import time),
    this function can be called after settings are reloaded.
    """
    global celery_app
    if celery_app is not None:
        return celery_app
    celery_app = _create_celery_app()
    return celery_app
