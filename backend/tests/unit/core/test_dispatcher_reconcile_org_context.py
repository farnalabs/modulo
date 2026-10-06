"""FAR-1501 — ``dispatcher_reconcile`` must attribute per-org tick ERRORs to that org.

``dispatcher_reconcile`` is an org-less SYSTEM cron at the job level — the SAQ
``before_process`` hook correctly binds ``None`` for it — but its per-org loop
scans ONE organisation per iteration, and every ERROR logged inside that
iteration (``dispatcher_reconcile: read failed (org ...)``, terminalizer
failures, per-row re-enqueue failures) has a resolvable org that
``ErrorTrackingLogHandler`` can attribute. Before FAR-1501 nothing bound
``org_id_var`` there, so those records were dropped with only a
``no_org_context`` WARNING.

These tests drive the REAL ``dispatcher_reconcile`` (real org collection, real
per-org ``_reconcile_org`` pass, real ``ErrorTrackingLogHandler``) and never
set ``org_id_var`` themselves:

1. two orgs whose reconcile reads both fail are forwarded with each ERROR
   carrying the org of the tick it happened in — proving both attribution AND
   that the second org's tick never inherits the first org's context;
2. a failure BEFORE the loop (the org collection) has no organisation — it
   keeps the announced drop, pinning the bind at the resolution point.
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

ORG1 = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ORG2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


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


class _ReconcileSession:
    """Session double: serves the org-index read ONCE, then every other
    statement fails — the per-org terminalizer scan inside ``_reconcile_org``
    must surface that as ``read failed (org ...)`` (the ERROR under test).

    ``get_bind`` reports postgresql so the REAL ``_set_rls_org`` exercises the
    production ``set_config`` path; those statements are answered without
    consuming any scan state.
    """

    def __init__(self, org_ids: list[uuid.UUID], *, fail_org_index: bool = False) -> None:
        self._org_ids = org_ids
        self._fail_org_index = fail_org_index
        self._org_index_served = False
        self.executed: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    def get_bind(self) -> MagicMock:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        return bind

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        rendered = str(stmt)
        self.executed.append(rendered)
        if "set_config" in rendered:
            return MagicMock()
        if not self._org_index_served:
            self._org_index_served = True
            if self._fail_org_index:
                raise SQLAlchemyError("org collection down")
            result = MagicMock()
            result.scalars.return_value = list(self._org_ids)
            return result
        raise SQLAlchemyError("reconcile scan down")


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
        saq_reenqueue_window=60,
        saq_job_heartbeat=30,
        saq_claimed_nodeless_minutes=30,
        saq_nodeless_early_detect_minutes=None,
        hitl_review_cancel_grace_seconds=3600,
        saq_run_claim_cap=20,
        dispatcher_reconcile_budget_seconds=95,
        dispatcher_reconcile_terminalize_max_per_tick=25,
        dispatcher_reconcile_facts_max_per_tick=50,
        dispatcher_reconcile_max_rows_per_tick=500,
    )


async def _drain() -> None:
    """Let the handler's forwarding task run before the sink is read."""
    for _ in range(3):
        await asyncio.sleep(0)


async def _drive() -> dict[str, Any]:
    session = _ReconcileSession([ORG1, ORG2])
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    with (
        patch.object(ch, "_open_system_factory", return_value=factory),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis") as redis_cls,
        patch.object(ch, "RedisQueue", MagicMock()),
        # Post-loop org-less phases (compensating sweeps, OTel gauges) are
        # cross-org by design — not the surface under test here.
        patch.object(ch, "_run_reconcile_sweeps", new_callable=AsyncMock),
        patch.object(ch, "_update_reconcile_telemetry", new_callable=AsyncMock),
    ):
        redis_cls.from_url.return_value = redis_client
        return await ch.dispatcher_reconcile()


async def test_per_org_read_failures_are_attributed_to_their_own_orgs(
    capture: None,
    sink: _RecordingSink,
) -> None:
    """Each per-org ``read failed (org ...)`` ERROR carries the org of its tick.

    Two orgs both fail their reconcile read. This test never touches
    ``org_id_var``: the org on each record comes solely from the real
    ``_bound_org`` bind around the per-org loop — so the SECOND record proves
    the second tick did not inherit the first org's context, and the trailing
    ``None`` proves the bind is scoped to the tick (caller context is clean).
    """
    assert org_id_var.get() is None

    summary = await _drive()
    await _drain()

    assert summary["status"] == "ok"
    messages = sink.messages
    assert any(f"read failed (org {ORG1})" in message for message in messages)
    assert any(f"read failed (org {ORG2})" in message for message in messages)
    # Exactly one forwarded record per org, each bound to ITS OWN tick:
    assert sink.orgs == [str(ORG1), str(ORG2)]
    # Scoped to the tick — the caller's context never saw the bind.
    assert org_id_var.get() is None


async def test_org_collection_failure_before_the_loop_keeps_the_announced_drop(
    capture: None,
    sink: _RecordingSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The org collection runs BEFORE any org tick — no org exists there.

    With no organisation resolved yet, the ERROR must be the announced drop —
    never attributed to a fabricated org, never silently forwarded.
    """
    session = _ReconcileSession([ORG1, ORG2], fail_org_index=True)
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    with (
        patch.object(ch, "_open_system_factory", return_value=factory),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis") as redis_cls,
        patch.object(ch, "RedisQueue", MagicMock()),
        patch.object(ch, "_run_reconcile_sweeps", new_callable=AsyncMock),
        patch.object(ch, "_update_reconcile_telemetry", new_callable=AsyncMock),
    ):
        redis_cls.from_url.return_value = redis_client
        with pytest.raises(SQLAlchemyError):
            await ch.dispatcher_reconcile()
    await _drain()

    assert not sink.messages
    assert any("no_org_context" in record.getMessage() for record in caplog.records)
    assert org_id_var.get() is None
