"""Shared cases and builders for the ``cleanup_jobs`` unit tests.

``cleanup_old_webhook_events`` (30-day window, filters on ``created_at``) and
``cleanup_old_trigger_events`` (90-day window, filters on ``received_at``) are
the same select-then-delete pass, and their SAQ wrappers are the same drain
loop over a different cleanup callable. Rather than keeping a byte-identical
suite per job, every shared behaviour is parameterised over ``JOB_CASES`` so a
change to one job's contract cannot silently drift from the other's.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.saq_worker as sw
from modulo.core.cleanup_jobs import trigger_events_cleanup as trigger_events_module
from modulo.core.cleanup_jobs import webhook_dedup_cleanup as webhook_dedup_module
from modulo.core.cleanup_jobs.trigger_events_cleanup import cleanup_old_trigger_events
from modulo.core.cleanup_jobs.webhook_dedup_cleanup import cleanup_old_webhook_events
from modulo.db.models.trigger_event import TriggerEvent


@dataclass(frozen=True)
class JobCase:
    """One cleanup job: its core pass, its SAQ wrapper and its retention shape."""

    name: str
    cleanup_fn: Callable[..., Any]
    cron_fn: Callable[..., Any]
    module: ModuleType
    retention_days: int
    batch_size: int
    filter_column: str

    @property
    def patch_target(self) -> str:
        """Dotted path the SAQ wrapper imports its cleanup callable from."""
        return f"{self.module.__name__}.{self.cleanup_fn.__name__}"

    @property
    def other_timestamp_column(self) -> str:
        return "created_at" if self.filter_column == "received_at" else "received_at"

    @property
    def seed_age_days(self) -> int:
        """Comfortably past the retention window (the window plus a week)."""
        return self.retention_days + 7


JOB_CASES = [
    pytest.param(
        JobCase(
            name="webhook-dedup",
            cleanup_fn=cleanup_old_webhook_events,
            cron_fn=sw.webhook_dedup_cleanup,
            module=webhook_dedup_module,
            retention_days=webhook_dedup_module.DEFAULT_RETENTION_DAYS,
            batch_size=webhook_dedup_module.BATCH_SIZE,
            filter_column="created_at",
        ),
        id="webhook-dedup",
    ),
    pytest.param(
        JobCase(
            name="trigger-events",
            cleanup_fn=cleanup_old_trigger_events,
            cron_fn=sw.trigger_events_cleanup,
            module=trigger_events_module,
            retention_days=trigger_events_module.DEFAULT_RETENTION_DAYS,
            batch_size=trigger_events_module.BATCH_SIZE,
            filter_column="received_at",
        ),
        id="trigger-events",
    ),
]


def patch_settings(modulo_db: str = "postgres"):
    """Patch ``saq_worker.get_settings`` to report the given database dialect."""
    return patch.object(sw, "get_settings", return_value=MagicMock(modulo_db=modulo_db))


def make_session(ids: list[uuid.UUID] | None = None) -> AsyncMock:
    """Return a mock async session pre-wired for a single cleanup pass.

    The first ``execute`` returns the SELECT result wrapping *ids*; when *ids*
    is non-empty a second ``execute`` returns a DELETE result with a rowcount.
    """
    session = AsyncMock()
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = ids or []
    if ids:
        delete_result = MagicMock()
        delete_result.rowcount = len(ids)
        session.execute = AsyncMock(side_effect=[select_result, delete_result])
    else:
        session.execute = AsyncMock(return_value=select_result)
    session.commit = AsyncMock()
    return session


def make_factory_with_session() -> tuple[MagicMock, MagicMock]:
    """Return a mock sessionmaker (via ``async with``) plus its session.

    ``session`` is a plain MagicMock (not AsyncMock) so ``session.begin()``
    returns the context-manager mock synchronously — the cron opens an explicit
    per-batch transaction. An AsyncMock's ``begin()`` would return a bare
    coroutine that ``async with`` cannot enter.
    """
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    factory = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = context
    return factory, session


def make_trigger_event(age_days: float) -> TriggerEvent:
    """Return a ``trigger_events`` row stamped *age_days* old on both timestamps.

    Both timestamp columns are stamped so the row is "old" for either job's
    retention column; the server default only covers ``created_at``, so an
    explicitly old ``received_at`` is required for the trigger-events job.
    """
    stamp = datetime.now(UTC) - timedelta(days=age_days)
    return TriggerEvent(
        organisation_id=uuid.uuid4(),
        trigger_id=uuid.uuid4(),
        trigger_type="webhook",
        raw_payload_hash=uuid.uuid4().hex * 2,
        created_at=stamp,
        received_at=stamp,
        validation_result="accepted",
    )
