"""FAR-250: SAQ worker startup hook — event-bus wiring, fail-open.

The hook must:
* be wired into ``_base_worker_settings`` (flows through both
  ``Worker(**settings())`` in ``run_system_web`` and the plain
  ``python -m saq ...runs_settings`` CLI);
* invoke ``register_listeners()`` + ``configure_event_bus(RedisEventBroker)``;
* spawn the connect+subscribe background task (workers subscribe, never relay);
* NEVER raise — a Redis blip at boot must not crash the worker
  (``policy="always"`` crash-loop risk); broker init raising must leave the
  worker settings buildable and the hook non-fatal.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.saq_worker as sw
from modulo.core.events import worker_subscription as ws


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "saq_redis_pool_size": 50,
        "saq_worker_concurrency": 5,
        "redis_url": "redis://localhost:6379/0",
        "database_url": "postgresql+asyncpg://localhost/test",
        "modulo_db": "postgres",
        "saq_worker_db_pool_size": 2,
        "saq_auth_password": "pw",
        "saq_auth_username": "admin",
        "fernet_key": "x" * 44,
        "modulo_library_sync_interval_seconds": 300,
    }
    base.update(overrides)
    return MagicMock(**base)


@pytest.fixture(autouse=True)
def _stub_network_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module off the network (sync Redis probe + DB probe)."""
    sync_client = MagicMock()
    sync_client.ping.return_value = True
    monkeypatch.setattr("redis.Redis.from_url", MagicMock(return_value=sync_client))
    monkeypatch.setattr("sqlalchemy.create_engine", MagicMock(return_value=MagicMock()))


class TestSettingsWireStartupHook:
    def test_base_worker_settings_includes_startup_hook(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_build_queue", return_value=MagicMock()),
        ):
            settings = sw._base_worker_settings("runs", [])
        assert settings["startup"] is sw._startup_event_bus_hook

    def test_runs_settings_includes_startup_hook(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_build_queue", return_value=MagicMock()),
        ):
            settings = sw.runs_settings()
        assert settings["startup"] is sw._startup_event_bus_hook

    def test_system_settings_includes_startup_hook(self) -> None:
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_build_queue", return_value=MagicMock()),
        ):
            settings = sw.system_settings()
        assert settings["startup"] is sw._startup_event_bus_hook

    def test_settings_still_build_when_broker_init_would_raise(self) -> None:
        """Broker init raising must not prevent settings construction.

        (Settings construction never invokes the hook — the hook itself is
        fail-open too, asserted in TestStartupHook below.)
        """
        with (
            patch.object(sw, "get_settings", return_value=_settings()),
            patch.object(sw, "_build_queue", return_value=MagicMock()),
            patch(
                "modulo.core.events.redis_broker.RedisEventBroker",
                side_effect=RuntimeError("redis unreachable"),
            ),
        ):
            settings = sw._base_worker_settings("runs", [])
        assert settings["startup"] is sw._startup_event_bus_hook


class TestStartupHook:
    async def test_invokes_register_configure_and_spawns_subscription(self) -> None:
        with (
            patch("modulo.core.events.register_listeners") as reg,
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock) as cfg,
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("modulo.core.events.worker_subscription.spawn_worker_event_subscription") as spawn,
        ):
            await sw._startup_event_bus_hook({})

        reg.assert_called_once()
        cfg.assert_awaited_once()
        broker_arg = cfg.await_args.kwargs["redis_broker"]
        assert broker_arg is not None
        spawn.assert_called_once_with(broker_arg)

    async def test_fail_open_when_configure_event_bus_raises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            patch("modulo.core.events.register_listeners") as reg,
            patch(
                "modulo.core.events.configure_event_bus",
                new_callable=AsyncMock,
                side_effect=RuntimeError("redis down"),
            ),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("modulo.core.events.worker_subscription.spawn_worker_event_subscription") as spawn,
            caplog.at_level("WARNING", logger="modulo.core.saq_worker"),
        ):
            await sw._startup_event_bus_hook({})  # must not raise

        reg.assert_called_once()
        spawn.assert_not_called()
        assert "saq_worker.event_bus_configure_failed" in caplog.text

    async def test_fail_open_when_broker_init_raises(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            patch("modulo.core.events.register_listeners"),
            patch(
                "modulo.core.events.redis_broker.RedisEventBroker",
                side_effect=RuntimeError("broker init boom"),
            ),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock) as cfg,
            patch.object(sw, "get_settings", return_value=_settings()),
            caplog.at_level("WARNING", logger="modulo.core.saq_worker"),
        ):
            await sw._startup_event_bus_hook({})  # must not raise

        cfg.assert_not_awaited()
        assert "saq_worker.event_bus_configure_failed" in caplog.text

    async def test_register_listeners_failure_still_configures_bus(self) -> None:
        with (
            patch("modulo.core.events.register_listeners", side_effect=RuntimeError("mapper boom")),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock) as cfg,
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("modulo.core.events.worker_subscription.spawn_worker_event_subscription") as spawn,
            patch("modulo.core.saq_worker._log.exception"),
        ):
            await sw._startup_event_bus_hook({})  # must not raise

        cfg.assert_awaited_once()
        spawn.assert_called_once()

    async def test_skips_bus_configuration_without_redis_url(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            patch("modulo.core.events.register_listeners"),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock) as cfg,
            patch.object(sw, "get_settings", return_value=_settings(redis_url="")),
            caplog.at_level("INFO", logger="modulo.core.saq_worker"),
        ):
            await sw._startup_event_bus_hook({})

        cfg.assert_not_awaited()
        assert "saq_worker.event_bus_skipped_no_redis_url" in caplog.text

    async def test_spawn_failure_is_fail_open(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            patch("modulo.core.events.register_listeners"),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch(
                "modulo.core.events.worker_subscription.spawn_worker_event_subscription",
                side_effect=RuntimeError("cannot spawn"),
            ),
            caplog.at_level("WARNING", logger="modulo.core.saq_worker"),
        ):
            await sw._startup_event_bus_hook({})  # must not raise

        assert "saq_worker.event_subscription_spawn_failed" in caplog.text


class _BlockingPubSub:
    """Pattern subscription that confirms, then holds open until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def listen(self):
        yield {"type": "psubscribe", "pattern": "modulo:events:resource:*"}
        self.started.set()
        await asyncio.Event().wait()  # hold until task cancellation

    async def unsubscribe(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class TestWorkerEventSubscription:
    async def test_retries_subscribe_until_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """First subscribe attempt fails, second succeeds — no crash, backoff, retry."""
        monkeypatch.setattr(ws, "_BACKOFF_BASE_SECONDS", 0.01)
        ok = _BlockingPubSub()
        broker = MagicMock()
        broker.connect = AsyncMock()
        broker.subscribe_pattern = AsyncMock(side_effect=[RuntimeError("first attempt fails"), ok])

        with caplog.at_level("INFO", logger="modulo.core.events.worker_subscription"):
            task = asyncio.create_task(ws.run_worker_event_subscription(broker))
            try:
                await asyncio.wait_for(ok.started.wait(), timeout=5)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        assert broker.subscribe_pattern.await_count == 2
        assert "worker_event_subscription.failed" in caplog.text
        assert "worker_event_subscription.subscribed" in caplog.text

    async def test_cancellation_propagates(self) -> None:
        ok = _BlockingPubSub()
        broker = MagicMock()
        broker.connect = AsyncMock()
        broker.subscribe_pattern = AsyncMock(return_value=ok)

        task = asyncio.create_task(ws.run_worker_event_subscription(broker))
        await asyncio.wait_for(ok.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ok.closed  # pubsub cleaned up in finally
