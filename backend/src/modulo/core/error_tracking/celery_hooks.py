"""Celery task failure handler — captures failed tasks to ErrorIngestionService."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import traceback as tb_module
import uuid
from typing import Any

from modulo.core.error_tracking import ErrorIngestionService
from modulo.version import get_version

_log = logging.getLogger(__name__)

_SERVICE: ErrorIngestionService | None = None
_SERVICE_LOCK = threading.Lock()
_SYSTEM_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _get_service() -> ErrorIngestionService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = ErrorIngestionService()
    return _SERVICE


def celery_task_failure_handler(
    sender: Any = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    einfo: Any = None,
    **_kw: Any,
) -> None:
    """Capture Celery task failures and send to error tracking."""
    try:
        asyncio.get_running_loop()
        # Already in an event loop — run in a separate thread
        thread = threading.Thread(
            target=_run_async_ingest,
            args=(sender, task_id, exception, args, kwargs, einfo),
            daemon=True,
        )
        thread.start()
    except RuntimeError:
        # No running event loop — safe to use asyncio.run
        try:
            asyncio.run(_async_ingest(sender, task_id, exception, args, kwargs, einfo))
        except Exception:
            _log.exception("celery_hooks.ingest_failed")


def _run_async_ingest(
    sender: Any,
    task_id: str | None,
    exception: BaseException | None,
    args: tuple[Any, ...] | None,
    kwargs: dict[str, Any] | None,
    einfo: Any,
) -> None:
    try:
        asyncio.run(_async_ingest(sender, task_id, exception, args, kwargs, einfo))
    except Exception:
        _log.exception("celery_hooks.ingest_failed")


async def _async_ingest(
    sender: Any,
    task_id: str | None,
    exception: BaseException | None,
    args: tuple[Any, ...] | None,
    kwargs: dict[str, Any] | None,
    einfo: Any,
) -> None:
    from modulo.api.dependencies import (
        get_or_create_engine,
        get_or_create_session_factory,
    )
    from modulo.db.rls import set_rls_org
    from modulo.settings import get_settings

    settings = get_settings()
    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    task_name = getattr(sender, "name", str(sender or "unknown"))
    exc_type = type(exception).__name__ if exception else "Unknown"
    exc_msg = str(exception) if exception else ""
    message = f"{exc_type}: {exc_msg}"

    stacktrace = None
    if exception is not None:
        stacktrace = "".join(tb_module.format_exception(type(exception), exception, exception.__traceback__))
    elif einfo is not None:
        stacktrace = str(einfo)

    args_summary = str(args)[:200] if args else ""
    kwargs_keys = sorted(kwargs.keys()) if kwargs else []

    org_id = None
    if kwargs and "org_id" in kwargs:
        try:
            org_id = uuid.UUID(str(kwargs["org_id"]))
        except (ValueError, TypeError):
            org_id = _SYSTEM_ORG_ID

    effective_org_id = org_id or _SYSTEM_ORG_ID

    event_data: dict[str, Any] = {
        "level": "error",
        "message": message,
        "source": "celery",
        "stacktrace": stacktrace,
        "context_json": {
            "task_name": task_name,
            "task_id": task_id,
            "args_summary": args_summary,
            "kwargs_keys": kwargs_keys,
        },
        "environment": os.environ.get("MODULO_ENV", "development"),
        "version": get_version(),
    }

    service = _get_service()
    async with factory() as session, session.begin():
        await set_rls_org(session, effective_org_id)
        await service.ingest(session, effective_org_id, event_data)
