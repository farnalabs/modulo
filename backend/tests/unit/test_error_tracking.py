"""Unit tests for the Redis-backed SAQ alert cooldowns (error_tracking).

Covers the missed-fire alert cooldown migration (SAQ follow-up, retro item 5):
the atomic ``SET NX EX`` check-and-mark, the cooldown suppression path, and
the FAIL-OPEN path (a Redis error must never suppress an alert).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.error_tracking as et

ORG = uuid.uuid4()


# ---------------------------------------------------------------------------
# _missed_fire_cooldown_ok — the atomic SET NX EX primitive
# ---------------------------------------------------------------------------


class TestMissedFireCooldown:
    async def test_fresh_trigger_not_in_cooldown(self) -> None:
        """SET NX returns True when the key was newly set -> alert may fire."""
        redis_client = AsyncMock()
        redis_client.set.return_value = True
        assert await et._missed_fire_cooldown_ok(redis_client, str(ORG), "trig-1") is True
        redis_client.set.assert_awaited_once_with(
            f"saq:alert:cooldown:missed_fire:{ORG}:trig-1",
            "1",
            nx=True,
            ex=et._MISSED_FIRE_COOLDOWN_SECONDS,
        )

    async def test_trigger_within_cooldown_window_suppressed(self) -> None:
        """SET NX returns None when the key already exists (window active) ->
        no alert."""
        redis_client = AsyncMock()
        redis_client.set.return_value = None
        assert await et._missed_fire_cooldown_ok(redis_client, str(ORG), "trig-1") is False

    async def test_redis_error_fails_open_to_alerting(self) -> None:
        """A Redis failure FAILS OPEN (returns True so the alert fires) and is
        logged — the cooldown must never suppress a real alert."""
        redis_client = AsyncMock()
        redis_client.set.side_effect = RuntimeError("redis down")
        with patch.object(et._log, "warning") as log_warn:
            assert await et._missed_fire_cooldown_ok(redis_client, str(ORG), "trig-1") is True
        log_warn.assert_called_once()
        assert "cooldown_redis_failed" in log_warn.call_args.args[0]

    async def test_cancelled_error_is_not_swallowed(self) -> None:
        redis_client = AsyncMock()
        redis_client.set.side_effect = __import__("asyncio").CancelledError()
        with pytest.raises(__import__("asyncio").CancelledError):
            await et._missed_fire_cooldown_ok(redis_client, str(ORG), "trig-1")


# ---------------------------------------------------------------------------
# check_missed_fire_alerts — cooldown suppression + fail-open at the probe level
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, org_ids: list[uuid.UUID]) -> None:
        self._org_ids = org_ids

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        result = MagicMock()
        result.all.return_value = [(oid,) for oid in self._org_ids]
        return result


class _FakeSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        result = MagicMock()
        result.all.return_value = self._rows
        return result


class _FakeFactory:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._rows)


def _stale_trigger() -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_type="cron",
        cron_expression="0 * * * *",  # hourly — period >= 1h
        cron_timezone="UTC",
        config_json={},
        last_fired_at=now - timedelta(hours=2),  # missed by > period + grace
        created_at=now - timedelta(days=1),
    )


async def _run_probe(*, cooldown_ok: bool) -> tuple[int, Any]:
    rows = [_stale_trigger()]
    aengine = MagicMock()
    aengine.connect.return_value = _FakeConn([ORG])
    redis_client = AsyncMock()
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client
    settings = MagicMock()
    settings.redis_url = "redis://localhost:6379/0"

    with (
        patch.object(et, "get_settings", return_value=settings),
        patch.object(et, "AsyncRedis", redis_cls),
        patch.object(et, "_missed_fire_cooldown_ok", new_callable=AsyncMock, return_value=cooldown_ok),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=_FakeFactory(rows)),
        patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
        patch.object(et, "create_error_event", new_callable=AsyncMock) as create,
    ):
        emitted = await et.check_missed_fire_alerts(aengine, org_id=ORG)
    return emitted, create


class TestMissedFireProbeCooldown:
    async def test_cooldown_active_suppresses_alert(self) -> None:
        """Within the cooldown window the probe emits NO alert."""
        emitted, create = await _run_probe(cooldown_ok=False)
        assert emitted == 0
        create.assert_not_awaited()

    async def test_cooldown_clear_alerts(self) -> None:
        """Outside the cooldown window the probe emits exactly one alert."""
        emitted, create = await _run_probe(cooldown_ok=True)
        assert emitted == 1
        create.assert_awaited_once()
        event_kwargs = create.await_args.kwargs
        assert event_kwargs["source"] == "saq"
        assert event_kwargs["level"] == "error"

    async def test_redis_failure_fails_open_to_alert(self) -> None:
        """A Redis failure inside the probe FAILS OPEN: the alert still fires
        (``_missed_fire_cooldown_ok`` returns True) and is logged — never
        suppressed by an unavailable Redis."""
        rows = [_stale_trigger()]
        aengine = MagicMock()
        aengine.connect.return_value = _FakeConn([ORG])
        redis_client = AsyncMock()
        redis_client.set.side_effect = RuntimeError("redis down")
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        settings = MagicMock()
        settings.redis_url = "redis://localhost:6379/0"

        with (
            patch.object(et, "get_settings", return_value=settings),
            patch.object(et, "AsyncRedis", redis_cls),
            patch.object(et._log, "warning") as log_warn,
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=_FakeFactory(rows)),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch.object(et, "create_error_event", new_callable=AsyncMock) as create,
        ):
            emitted = await et.check_missed_fire_alerts(aengine, org_id=ORG)

        assert emitted == 1  # fail-open: the alert still fired
        create.assert_awaited_once()
        log_warn.assert_called_once()
        assert "cooldown_redis_failed" in log_warn.call_args.args[0]
