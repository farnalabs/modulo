"""FAR-1484 — ``fire_due_triggers`` must attribute per-org tick ERRORs to that org.

``fire_due_triggers`` is an org-less SYSTEM cron at the JOB level — the SAQ
``before_process`` hook correctly binds ``None`` for it — but its loop scans
ONE organisation per iteration, and every ERROR logged inside that iteration
(``cron read failed (org ...)``, enqueue/advance/catch-up failures) has a
resolvable org. Before FAR-1484 nothing bound ``org_id_var`` there, so those
records were dropped with only a ``no_org_context`` WARNING.

These tests drive the REAL ``fire_due_triggers`` (real scan/error handlers,
real ``ErrorTrackingLogHandler``) and never set ``org_id_var`` themselves:

1. a failed per-org cron read is forwarded with the org of the tick it
   happened in, and the caller's context is clean afterwards;
2. a failure BEFORE the loop (the org-less pause read) has no organisation —
   it keeps the announced drop, pinning the bind at the resolution point.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.core import cron_helpers as ch
from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

ORG = uuid.UUID("00000000-0000-0000-0000-000000000081")


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit`` (see the wiring test)."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self.orgs: list[str | None] = []

    async def __call__(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.orgs.append(org_id_var.get())

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _pause_result(org_id: uuid.UUID) -> MagicMock:
    r = MagicMock()
    r.all.return_value = [(org_id, False, "active")]
    return r


def _empty_result() -> MagicMock:
    """Exhausted-sequence fallback: every later scan reads zero rows."""
    r = MagicMock()
    r.all.return_value = []
    r.fetchall.return_value = []
    r.scalars.return_value = []
    r.first.return_value = None
    r.scalar_one_or_none.return_value = None
    r.__iter__.return_value = iter([])
    return r


class _SeqSession:
    """Sequence-driven session: entries are results, or an exception to raise.

    RLS ``set_config`` plumbing never consumes a step; once the declared steps
    run out, every further read returns zero rows (the remaining scans are
    simply empty).
    """

    def __init__(self, steps: list[Any]) -> None:
        self._steps = list(steps)
        self.executed: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    def begin_nested(self) -> _Begin:
        return _Begin()

    def get_bind(self) -> Any:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        return bind

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        rendered = str(stmt)
        self.executed.append(rendered)
        if "set_config" in rendered:
            return MagicMock()
        step = self._steps.pop(0) if self._steps else _empty_result()
        if isinstance(step, BaseException):
            raise step
        return step

    def add(self, obj: object) -> None:
        return None

    async def flush(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_rate_limit_state() -> Iterator[None]:
    """The handler forwards at most one record per org per 5s window."""
    ErrorTrackingLogHandler._last_write_time.clear()
    yield
    ErrorTrackingLogHandler._last_write_time.clear()


@pytest.fixture
def sink() -> _RecordingSink:
    return _RecordingSink()


@pytest.fixture
def capture(sink: _RecordingSink) -> Iterator[None]:
    """Attach a REAL ``ErrorTrackingLogHandler`` (with a recording sink) to the root logger."""
    handler = ErrorTrackingLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new=sink):
            yield
    finally:
        root.removeHandler(handler)


def _settings() -> MagicMock:
    return MagicMock(
        saq_runs_queue="runs",
        redis_url="redis://localhost:6379/0",
        saq_redis_pool_size=5,
    )


async def _drain() -> None:
    """Let the handler's forwarding task run before the sink is read."""
    for _ in range(3):
        await asyncio.sleep(0)


async def _drive_await(session: _SeqSession) -> dict[str, Any]:
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    with (
        patch.object(ch, "_open_factory", return_value=factory),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis") as redis_cls,
        patch.object(ch, "RedisQueue", MagicMock()),
    ):
        redis_cls.from_url.return_value = redis_client
        return await ch.fire_due_triggers()


async def test_per_org_read_failure_is_attributed_to_that_org(
    capture: None,
    sink: _RecordingSink,
) -> None:
    """The per-org bind: a real ``cron read failed (org ...)`` ERROR persists.

    The org comes from the real loop (the tick's org id) — this test never
    touches ``org_id_var`` — and the bind is scoped: after the tick the
    caller's context is clean.
    """
    session = _SeqSession(
        [
            _org_result([ORG]),
            _pause_result(ORG),
            SQLAlchemyError("cron scan down"),
        ]
    )

    summary = await _drive_await(session)
    await _drain()

    assert summary["orgs_scanned"] == 1
    assert any(f"cron read failed (org {ORG})" in message for message in sink.messages)
    assert sink.orgs == [str(ORG)]
    # Scoped to the tick: the caller's context never saw the bind.
    assert org_id_var.get() is None


async def test_pause_read_failure_before_the_loop_keeps_the_announced_drop(
    capture: None,
    sink: _RecordingSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The org-collection/pause phase runs before ANY org tick — no org exists there.

    ``_read_pause_by_org`` logs the failure at ERROR and re-raises (the tick
    fails so the SAQ system cron retries). With no organisation resolved yet,
    the record must be the announced drop — never attributed to a fabricated
    org, never silent.
    """
    session = _SeqSession(
        [
            _org_result([ORG]),
            SQLAlchemyError("pause read down"),
        ]
    )

    with pytest.raises(SQLAlchemyError):
        await _drive_await(session)
    await _drain()

    assert not sink.messages
    assert any("no_org_context" in record.getMessage() for record in caplog.records)
    assert org_id_var.get() is None
