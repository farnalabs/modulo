"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.error_tracking`` (PR #97).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import error_tracking as et
from tests.unit.api.mock_session import make_session_scope


def _redis_mock(method: str) -> MagicMock:
    client = MagicMock()
    setattr(client, method, AsyncMock(side_effect=RuntimeError("boom")))
    return client


def _failure_records(caplog: object, key: str) -> list[object]:
    return [r for r in caplog.records if key in r.message]  # type: ignore[attr-defined]


async def test_fire_once_allowed_redis_failure(caplog) -> None:
    signal = "sig"
    client = _redis_mock("set")
    with caplog.at_level(logging.WARNING, logger="modulo.core.error_tracking"):
        result = await et._fire_once_allowed(client, "org", "rg", signal)
    assert result is True
    records = _failure_records(caplog, "error_tracking.fire_once_redis_failed")
    assert len(records) == 1
    assert records[0].exc_info is not None


async def test_missed_fire_cooldown_ok_redis_failure(caplog) -> None:
    trigger_id = "trig"
    client = _redis_mock("set")
    with caplog.at_level(logging.WARNING, logger="modulo.core.error_tracking"):
        result = await et._missed_fire_cooldown_ok(client, "org", trigger_id)
    assert result is True
    records = _failure_records(caplog, "error_tracking.missed_fire_cooldown_redis_failed")
    assert len(records) == 1
    assert records[0].exc_info is not None


async def test_check_missed_fire_alerts_redis_close_failure(caplog) -> None:
    client = MagicMock()
    client.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch.object(et, "AsyncRedis", MagicMock(from_url=lambda *a, **k: client)),
        patch.object(et, "get_settings", return_value=MagicMock(redis_url="redis://localhost")),
        caplog.at_level(logging.WARNING, logger="modulo.core.error_tracking"),
    ):
        # ``aengine.connect()`` must yield a connection whose ``execute``
        # returns a real Result (sync readers) - a bare MagicMock chain leaks
        # AsyncMock coroutines through the async context manager.
        engine = MagicMock()
        engine.connect.return_value = make_session_scope()
        result = await et.check_missed_fire_alerts(engine, org_id=None)
    assert result == 0
    records = _failure_records(caplog, "error_tracking.missed_fire_redis_close_failed")
    assert len(records) == 1
    assert records[0].exc_info is not None
