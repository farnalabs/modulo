"""Post-terminal ``hitl_awaiting`` emission guard (defect 5).

The sole ``EVENT_HITL_AWAITING`` call site is
``PipelineExecutor._dispatch_hitl_awaiting`` (``core/pipeline_engine/executor.py``),
which funnels through ``Notifier.dispatch_event`` -> ``_dispatch_inline`` — the
single choke point every emission (and every resume/re-dispatch re-entry)
passes through. A run that is ALREADY terminal when the emission fires
(operator-cancelled, gate-expiry terminalised, failed, ...) must not produce a
fresh "waiting for human review" webhook or in-app row; that is what made the
daily cron's ``hitl.awaiting`` pile grow.

Drives the REAL notifier against a real SQLite session so the guard is
exercised end to end (run-state read -> suppression decision -> webhook +
in-app legs).

PROVE-THE-FIX: ``test_terminal_run_suppresses_the_emission`` and its
payload twin fail without the guard — the in-app row and the endpoint
dispatch both happen.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.core.notifier import (
    EVENT_HITL_AWAITING,
    EVENT_RUN_FAILED,
    DispatchResult,
    Notifier,
    _payload_run_id,
)
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.notification import Notification, NotificationPreference
from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.models.run import Run

_KEY = Fernet.generate_key().decode()
_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OWNER = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TERMINAL_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_LIVE_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000a2")

_TABLES = [
    Account.__table__,
    Run.__table__,
    Notification.__table__,
    NotificationPreference.__table__,
    NotificationEndpoint.__table__,
    NotificationDeliveryLog.__table__,
]


def _make_run(run_id: uuid.UUID, *, status: str, run_number: int) -> Run:
    now = datetime.now(UTC)
    return Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        trigger_type="cron",
        status=status,
        run_number=run_number,
        input_hash="a" * 64,
        langgraph_thread_id=f"thread-{run_id}",
        started_at=now - timedelta(hours=2),
    )


class _Harness:
    """Real-SQLite notifier with one terminal run, one live run and one
    subscribed webhook endpoint."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'hitl_suppress.db'}", echo=False
        )
        self.notifier = Notifier(self.engine, _KEY)
        self.endpoint_dispatch = AsyncMock(
            return_value=DispatchResult(
                endpoint_id=uuid.uuid4(),
                status="delivered",
                attempt_count=1,
                response_code=200,
            )
        )

    async def seed(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        maker = async_sessionmaker(self.engine, expire_on_commit=False, autobegin=False)
        now = datetime.now(UTC)
        async with maker() as session, session.begin():
            session.add(
                Account(
                    id=_OWNER,
                    email="owner@example.com",
                    display_name="Owner",
                    auth_provider="local",
                    preferences={},
                    active=True,
                    is_system_admin=False,
                    is_break_glass=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(_make_run(_TERMINAL_RUN, status="cancelled", run_number=1))
            session.add(_make_run(_LIVE_RUN, status="awaiting_human", run_number=2))
            session.add(
                NotificationEndpoint(
                    organisation_id=_ORG,
                    url="https://hooks.example.com/notify",
                    events=["hitl_awaiting", "run_failed"],
                    description="hitl webhook",
                    account_id=_OWNER,
                    auto_disabled=False,
                    consecutive_dead_letter_count=0,
                )
            )

    async def notification_count(self) -> int:
        maker = async_sessionmaker(self.engine, expire_on_commit=False, autobegin=False)
        async with maker() as session, session.begin():
            result = await session.execute(select(func.count(Notification.id)))
            return int(result.scalar_one())

    async def dispatch(self, *, run_id: uuid.UUID | None, event_type: str = EVENT_HITL_AWAITING) -> list[Any]:
        payload: dict[str, Any] = {
            "run_id": str(run_id) if run_id is not None else str(_LIVE_RUN),
            "gate_id": "gate-1",
            "pipeline_name": "Improve Security",
            "error_code": "node_failed",
        }
        with (
            patch.object(self.notifier, "_dispatch_endpoint_pinned", self.endpoint_dispatch),
            patch("modulo.core.notifier._fire_notification_sse_broadcast"),
        ):
            kwargs: dict[str, Any] = {}
            if run_id is not None:
                kwargs["run_id"] = run_id
            return list(await self.notifier.dispatch_event(_ORG, event_type, payload, **kwargs))

    async def aclose(self) -> None:
        await self.notifier.close()
        await self.engine.dispose()


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncGenerator[_Harness, None]:
    item = _Harness(tmp_path)
    await item.seed()
    try:
        yield item
    finally:
        await item.aclose()


async def test_terminal_run_suppresses_the_emission(harness: _Harness) -> None:
    """PROVE-THE-FIX: a terminal run produces NO fresh hitl_awaiting at all."""
    results = await harness.dispatch(run_id=_TERMINAL_RUN)

    assert not results, "a terminal run must not dispatch to any endpoint"
    assert await harness.notification_count() == 0
    harness.endpoint_dispatch.assert_not_called()


async def test_terminal_run_suppression_reads_the_payload_run_id(harness: _Harness) -> None:
    """The guard also fires when the emitter passes the run id only in the payload."""
    with (
        patch.object(harness.notifier, "_dispatch_endpoint_pinned", harness.endpoint_dispatch),
        patch("modulo.core.notifier._fire_notification_sse_broadcast"),
    ):
        results = await harness.notifier.dispatch_event(
            _ORG,
            EVENT_HITL_AWAITING,
            {"run_id": str(_TERMINAL_RUN), "gate_id": "gate-2", "pipeline_name": "Improve Security"},
        )

    assert not list(results)
    assert await harness.notification_count() == 0
    harness.endpoint_dispatch.assert_not_called()


async def test_live_run_still_emits(harness: _Harness) -> None:
    """Control: a non-terminal run dispatches the webhook AND writes the row."""
    results = await harness.dispatch(run_id=_LIVE_RUN)

    assert len(results) == 1
    assert results[0].status == "delivered"
    harness.endpoint_dispatch.assert_awaited_once()
    assert await harness.notification_count() == 1


async def test_missing_run_row_dispatches_as_before(harness: _Harness) -> None:
    """Fails OPEN: an unreadable/deleted run must never swallow a live alert."""
    results = await harness.dispatch(run_id=uuid.uuid4())

    assert len(results) == 1
    assert await harness.notification_count() == 1


async def test_other_event_types_are_unaffected(harness: _Harness) -> None:
    """The guard is scoped to ``hitl_awaiting`` — a terminal run still emits run_failed."""
    results = await harness.dispatch(run_id=_TERMINAL_RUN, event_type=EVENT_RUN_FAILED)

    assert len(results) == 1
    assert await harness.notification_count() == 1


# ---------------------------------------------------------------------------
# Run-id extraction branches (``_payload_run_id``)
# ---------------------------------------------------------------------------


def test_payload_run_id_accepts_a_uuid_instance() -> None:
    """An emitter that hands over a real ``uuid.UUID`` is used verbatim."""
    run_id = uuid.uuid4()
    assert _payload_run_id({"run_id": run_id}) == run_id


def test_payload_run_id_ignores_an_unparseable_string() -> None:
    """A non-UUID string means "no linked run" and never raises."""
    assert _payload_run_id({"run_id": "not-a-uuid"}) is None


def test_payload_run_id_ignores_absent_and_non_string_values() -> None:
    """An absent key or a non-string value falls through to ``None``."""
    assert _payload_run_id({}) is None
    assert _payload_run_id({"run_id": 12345}) is None


# ---------------------------------------------------------------------------
# Read-failure arms of ``_run_is_terminal``
# ---------------------------------------------------------------------------


async def test_run_state_db_error_fails_open(harness: _Harness) -> None:
    """An unreadable run row must dispatch as before — fail OPEN.

    The ``except Exception`` arm is the safety net that keeps a DB blip from
    silently swallowing a live review request.
    """
    with patch("modulo.core.notifier.set_rls_org", side_effect=RuntimeError("db blip")):
        assert await harness.notifier._run_is_terminal(_ORG, _TERMINAL_RUN) is False


async def test_run_state_cancellation_propagates(harness: _Harness) -> None:
    """A cancelled read is re-raised, not swallowed into a fail-open dispatch.

    ``CancelledError`` is a ``BaseException``; the guard re-raises it explicitly
    so the caller's cancellation is honoured.
    """
    with (
        patch("modulo.core.notifier.set_rls_org", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await harness.notifier._run_is_terminal(_ORG, _TERMINAL_RUN)
