"""Celery CLI entry point — provides a module-level ``app`` for ``celery -A``.

Usage::

    celery -A modulo.cli_celery beat --loglevel=info
    celery -A modulo.cli_celery worker --loglevel=info

The ``modulo.celery_app`` module cannot be used directly with ``celery -A``
because its module-level ``celery_app`` attribute is set to ``None`` to
prevent eager initialization at import time (Redis/Database may not be
available).

This module calls ``get_celery_app()`` eagerly, so it is ONLY suitable for
CLI use where Redis and the database are guaranteed to be available.
"""

from modulo.celery_app import get_celery_app

app = get_celery_app()
