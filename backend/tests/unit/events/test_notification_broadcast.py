"""FAR-250: notifier post-commit Redis broadcast — exactly one per created notification.

The notifier's in-app block commits, THEN fires a fire-and-forget
``broadcast_redis_only`` (the single Redis message per create; the listener's
own Redis leg is suppressed for notifier-owned sessions). Rollback/failure in
the in-app block must emit nothing (rollback-no-phantom), and the payload
carries only ``{notification_id, category, created_at}`` — never content.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import modulo.core.notifier as notifier_mod
from modulo.core.events.notification_events import NOTIFIER_SESSION_KEY, _isoformat
from modulo.core.notifier import Notifier
from tests.unit.api.mock_session import configure_mock_session
from tests.unit.events._helpers import drain_background_tasks

_ORG = uuid.UUID("8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70")


def _notification() -> MagicMock:
    n = MagicMock()
    n.id = uuid.uuid4()
    n.category = "run_failed"
    n.created_at = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    return n


async def _drain_broadcast_tasks() -> None:
    await drain_background_tasks(notifier_mod._sse_broadcast_tasks)


def _session_factory_double() -> MagicMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


class TestFireBroadcast:
    async def test_publishes_exactly_once_without_content(self) -> None:
        fake_bus = AsyncMock()
        notification = _notification()
        with patch("modulo.core.events.event_bus.get_event_bus", return_value=fake_bus):
            notifier_mod._fire_notification_sse_broadcast(_ORG, notification)
            await _drain_broadcast_tasks()

        fake_bus.broadcast_redis_only.assert_awaited_once()
        org_str, event = fake_bus.broadcast_redis_only.await_args.args
        assert org_str == str(_ORG)
        # Envelope + exactly the three allowed notification fields + event_id.
        assert set(event) == {
            "type",
            "id",
            "action",
            "version",
            "org_id",
            "notification_id",
            "category",
            "created_at",
            "event_id",
        }
        assert event["type"] == "notification"
        assert event["notification_id"] == str(notification.id)
        assert event["category"] == "run_failed"
        assert event["created_at"] == notification.created_at.isoformat()
        assert event["event_id"].endswith(":created")

    def test_no_running_loop_skips_without_raising(self, caplog: pytest.LogCaptureFixture) -> None:
        fake_bus = AsyncMock()
        with (
            patch("modulo.core.events.event_bus.get_event_bus", return_value=fake_bus),
            caplog.at_level("WARNING", logger="modulo.core.notifier"),
        ):
            notifier_mod._fire_notification_sse_broadcast(_ORG, _notification())

        fake_bus.broadcast_redis_only.assert_not_awaited()
        assert "notifier.sse_broadcast_skipped_no_loop" in caplog.text


class _DispatchWiring:
    """Objects a dispatch_event test can assert on after the call."""

    def __init__(self, notifier: Notifier, session: Any) -> None:
        self.notifier = notifier
        self.session = session
        self.mapper: MagicMock
        self.fake_bus: AsyncMock


@contextmanager
def _wired_dispatch(
    mapper: MagicMock,
    fake_bus: AsyncMock,
) -> Iterator[_DispatchWiring]:
    """Patch the four inline-wiring seams (endpoints, session, mapper, bus).

    The exact environment every ``TestDispatchInlineWiring`` test shares;
    tests add further patches as an enclosing ``with`` where they need them.
    """
    notifier = Notifier(MagicMock(), Fernet.generate_key().decode())
    wiring = _DispatchWiring(notifier=notifier, session=None)
    wiring.mapper = mapper
    wiring.fake_bus = fake_bus
    factory = _session_factory_double()
    with (
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])),
        patch.object(notifier, "_session_factory", factory),
        patch("modulo.core.notifier.event_mapper.NotificationEventMapper", return_value=mapper),
        patch("modulo.core.events.event_bus.get_event_bus", return_value=fake_bus),
    ):
        wiring.session = factory.return_value.__aenter__.return_value
        yield wiring


class TestDispatchInlineWiring:
    async def test_successful_dispatch_fires_single_broadcast(self) -> None:
        notification = _notification()
        mapper = MagicMock()
        mapper.create_from_event = AsyncMock(return_value=notification)
        fake_bus = AsyncMock()

        with _wired_dispatch(mapper, fake_bus) as wiring:
            results = await wiring.notifier.dispatch_event(_ORG, "run_failed", {"run_id": str(uuid.uuid4())})

        await _drain_broadcast_tasks()
        assert results == []
        # Exactly ONE Redis message for the create...
        fake_bus.broadcast_redis_only.assert_awaited_once()
        event = fake_bus.broadcast_redis_only.await_args.args[1]
        assert event["notification_id"] == str(notification.id)
        # ...and the session was marked notifier-owned so the listener's own
        # Redis leg stays suppressed (the other half of no-double-publish).
        # ``info`` is a real dict on the contract-correct session double, so
        # assert the effect the production write produced.
        assert wiring.session.info[NOTIFIER_SESSION_KEY] is True

    async def test_mapper_failure_fires_no_broadcast(self) -> None:
        """Rollback of the in-app transaction: no phantom Redis message."""
        mapper = MagicMock()
        mapper.create_from_event = AsyncMock(side_effect=RuntimeError("db down"))
        fake_bus = AsyncMock()

        with (
            _wired_dispatch(mapper, fake_bus) as wiring,
            patch("modulo.core.notifier._log.exception"),
        ):
            results = await wiring.notifier.dispatch_event(_ORG, "run_failed", {"run_id": str(uuid.uuid4())})

        await _drain_broadcast_tasks()
        assert results == []
        fake_bus.broadcast_redis_only.assert_not_awaited()

    async def test_unknown_event_type_returns_none_from_mapper_no_broadcast(self) -> None:
        """Mapper returns None (unrecognised event) -> no notification, no broadcast."""
        mapper = MagicMock()
        mapper.create_from_event = AsyncMock(return_value=None)
        fake_bus = AsyncMock()

        with _wired_dispatch(mapper, fake_bus) as wiring:
            await wiring.notifier.dispatch_event(_ORG, "totally_unrecognised_event", {})

        await _drain_broadcast_tasks()
        fake_bus.broadcast_redis_only.assert_not_awaited()


class TestSseBroadcastDoneCallback:
    """FAR-250: the fire-and-forget task callback must never leak or crash."""

    async def test_cancelled_task_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        async def _forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(_forever())
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        notifier_mod._sse_broadcast_tasks.add(task)

        with caplog.at_level("WARNING", logger="modulo.core.notifier"):
            notifier_mod._on_sse_broadcast_done(task)

        assert task not in notifier_mod._sse_broadcast_tasks
        assert "notifier.sse_broadcast_task_cancelled" in caplog.text

    async def test_failed_task_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        async def _boom() -> None:
            raise RuntimeError("redis down")

        task = asyncio.create_task(_boom())
        with contextlib.suppress(RuntimeError):
            await task
        notifier_mod._sse_broadcast_tasks.add(task)

        with caplog.at_level("WARNING", logger="modulo.core.notifier"):
            notifier_mod._on_sse_broadcast_done(task)

        assert task not in notifier_mod._sse_broadcast_tasks
        assert "notifier.sse_broadcast_failed" in caplog.text


def test_isoformat_none_datetime_and_string_passthrough() -> None:
    """_isoformat handles None, datetime, and already-serialised string values."""
    assert _isoformat(None) is None
    dt = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    assert _isoformat(dt) == dt.isoformat()
    # Non-datetime, non-None values (e.g. a pre-serialised string) pass through.
    assert _isoformat("2026-09-23T12:00:00+00:00") == "2026-09-23T12:00:00+00:00"


def test_notification_event_fields_falls_back_to_now_when_created_at_missing() -> None:
    """A target whose created_at is not yet populated falls back to now().

    The fallback must be a real UTC timestamp — ``is not None`` alone would
    pass for any junk value the helper could return.
    """
    from modulo.core.events.notification_events import notification_event_fields

    target = MagicMock()
    target.created_at = None
    target.category = "run_failed"

    before = datetime.now(UTC)
    fields = notification_event_fields(target, "n-1")
    after = datetime.now(UTC)

    fallback = datetime.fromisoformat(str(fields["created_at"]))
    assert fallback.tzinfo is not None, "fallback created_at must be timezone-aware"
    assert before <= fallback <= after
    assert fields["category"] == "run_failed"
    assert fields["notification_id"] == "n-1"
    assert fields["event_id"] == "notification:n-1:created"


def test_notification_event_fields_keeps_populated_created_at() -> None:
    """The committed created_at (INSERT..RETURNING) is passed through unchanged.

    This is the production path — the fallback branch only fires when the
    server default has not been fetched back yet, so the populated case must
    NOT be re-stamped with "now".
    """
    from modulo.core.events.notification_events import notification_event_fields

    committed = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    target = MagicMock()
    target.created_at = committed
    target.category = "run_failed"

    fields = notification_event_fields(target, "n-1")

    assert fields["created_at"] == committed.isoformat()
    assert fields["event_id"] == "notification:n-1:created"
