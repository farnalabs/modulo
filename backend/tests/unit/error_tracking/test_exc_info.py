"""Coverage for the ``exc_info=True`` exception handlers added across
``modulo.core.error_tracking`` (PR #97).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core import error_tracking as et


def _redis_mock(method: str) -> MagicMock:
    client = MagicMock()
    setattr(client, method, AsyncMock(side_effect=RuntimeError("boom")))
    return client


async def test_fire_once_allowed_redis_failure() -> None:
    signal = "sig"
    client = _redis_mock("set")
    with patch.object(et, "_log") as log:
        result = await et._fire_once_allowed(client, "org", "rg", signal)
    assert result is True
    log.warning.assert_called_once_with("error_tracking.fire_once_redis_failed signal=%s", signal, exc_info=True)


async def test_missed_fire_cooldown_ok_redis_failure() -> None:
    trigger_id = "trig"
    client = _redis_mock("set")
    with patch.object(et, "_log") as log:
        result = await et._missed_fire_cooldown_ok(client, "org", trigger_id)
    assert result is True
    log.warning.assert_called_once_with(
        "error_tracking.missed_fire_cooldown_redis_failed trigger=%s", trigger_id, exc_info=True
    )


async def test_check_missed_fire_alerts_redis_close_failure() -> None:
    client = MagicMock()
    client.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch.object(et, "AsyncRedis", MagicMock(from_url=lambda *a, **k: client)),
        patch.object(et, "get_settings", return_value=MagicMock(redis_url="redis://localhost")),
        patch.object(et, "_log") as log,
    ):
        result = await et.check_missed_fire_alerts(MagicMock(), org_id=None)
    assert result == 0
    log.warning.assert_called_once_with("error_tracking.missed_fire_redis_close_failed", exc_info=True)
