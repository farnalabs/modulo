"""FAR-250: notifier post-commit Redis broadcast — exactly one per created notification.

The notifier's in-app block commits, THEN fires a fire-and-forget
``broadcast_redis_only`` (the single Redis message per create; the listener's
own Redis leg is suppressed for notifier-owned sessions). Rollback/failure in
the in-app block must emit nothing (rollback-no-phantom), and the payload
carries only ``{notification_id, category, created_at}`` — never content.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import modulo.core.notifier as notifier_mod
from modulo.core.events.notification_events import NOTIFIER_SESSION_KEY
from modulo.core.notifier import Notifier

_ORG = uuid.UUID("8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70")


def _notification() -> MagicMock:
    n = MagicMock()
    n.id = uuid.uuid4()
    n.category = "run_failed"
    n.created_at = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    return n


async def _drain_broadcast_tasks() -> None:
    for _ in range(200):
        if not notifier_mod._sse_broadcast_tasks:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("sse broadcast tasks did not drain")


def _session_factory_double() -> MagicMock:
    session = AsyncMock()
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


class TestDispatchInlineWiring:
    async def test_successful_dispatch_fires_single_broadcast(self) -> None:
        notifier = Notifier(MagicMock(), Fernet.generate_key().decode())
        notification = _notification()
        mapper = MagicMock()
        mapper.create_from_event = AsyncMock(return_value=notification)
        factory = _session_factory_double()
        session = factory.return_value.__aenter__.return_value
        fake_bus = AsyncMock()

        with (
            patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])),
            patch.object(notifier, "_session_factory", factory),
            patch("modulo.core.notifier.event_mapper.NotificationEventMapper", return_value=mapper),
            patch("modulo.core.events.event_bus.get_event_bus", return_value=fake_bus),
        ):
            results = await notifier.dispatch_event(_ORG, "run_failed", {"run_id": str(uuid.uuid4())})

        await _drain_broadcast_tasks()
        assert results == []
        # Exactly ONE Redis message for the create...
        fake_bus.broadcast_redis_only.assert_awaited_once()
        event = fake_bus.broadcast_redis_only.await_args.args[1]
        assert event["notification_id"] == str(notification.id)
        # ...and the session was marked notifier-owned so the listener's own
        # Redis leg stays suppressed (the other half of no-double-publish).
        session.info.__setitem__.assert_any_call(NOTIFIER_SESSION_KEY, True)

    async def test_mapper_failure_fires_no_broadcast(self) -> None:
        """Rollback of the in-app transaction: no phantom Redis message."""
        notifier = Notifier(MagicMock(), Fernet.generate_key().decode())
        mapper = MagicMock()
        mapper.create_from_event = AsyncMock(side_effect=RuntimeError("db down"))
        factory = _session_factory_double()
        fake_bus = AsyncMock()

        with (
            patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])),
            patch.object(notifier, "_session_factory", factory),
            patch("modulo.core.notifier.event_mapper.NotificationEventMapper", return_value=mapper),
            patch("modulo.core.events.event_bus.get_event_bus", return_value=fake_bus),
            patch("modulo.core.notifier._log.exception"),
        ):
            results = await notifier.dispatch_event(_ORG, "run_failed", {"run_id": str(uuid.uuid4())})

        await _drain_broadcast_tasks()
        assert results == []
        fake_bus.broadcast_redis_only.assert_not_awaited()

    async def test_unknown_event_type_returns_none_from_mapper_no_broadcast(self) -> None:
        """Mapper returns None (unrecognised event) -> no notification, no broadcast."""
        notifier = Notifier(MagicMock(), Fernet.generate_key().decode())
        mapper = MagicMock()
        mapper.create_from_event = AsyncMock(return_value=None)
        factory = _session_factory_double()
        fake_bus: Any = AsyncMock()

        with (
            patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])),
            patch.object(notifier, "_session_factory", factory),
            patch("modulo.core.notifier.event_mapper.NotificationEventMapper", return_value=mapper),
            patch("modulo.core.events.event_bus.get_event_bus", return_value=fake_bus),
        ):
            await notifier.dispatch_event(_ORG, "totally_unrecognised_event", {})

        await _drain_broadcast_tasks()
        fake_bus.broadcast_redis_only.assert_not_awaited()
