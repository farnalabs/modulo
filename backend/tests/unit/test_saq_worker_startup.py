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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.saq_worker as sw
from modulo.core.events import worker_subscription as ws
from modulo.db import capacity as db_capacity


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

    async def test_installs_event_loop_stall_attribution(self) -> None:
        """FAR-1771: the shared startup hook is the worker-side install
        point for stall attribution (both worker flavours flow through it)."""
        with (
            patch("modulo.core.events.register_listeners"),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("modulo.core.events.worker_subscription.spawn_worker_event_subscription"),
            patch(
                "modulo.core.health_alerts.install_event_loop_stall_attribution",
                return_value=True,
            ) as install,
        ):
            await sw._startup_event_bus_hook({})

        install.assert_called_once_with()

    async def test_attribution_install_failure_is_fail_open(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Attribution is observability, never a boot requirement: an
        install failure logs and the worker still wires its event pipeline."""
        with (
            patch("modulo.core.events.register_listeners") as reg,
            patch(
                "modulo.core.health_alerts.install_event_loop_stall_attribution",
                side_effect=RuntimeError("sampler boom"),
            ),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock) as cfg,
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("modulo.core.events.worker_subscription.spawn_worker_event_subscription") as spawn,
            caplog.at_level("WARNING", logger="modulo.core.saq_worker"),
        ):
            await sw._startup_event_bus_hook({})  # must not raise

        reg.assert_called_once()
        cfg.assert_awaited_once()
        spawn.assert_called_once()
        assert "saq_worker.event_loop_stall_attribution_install_failed" in caplog.text

    async def test_attribution_install_cancellation_propagates(self) -> None:
        """Cancellation is NOT a fail-open install failure: a CancelledError
        from the install must re-raise (the worker's own cancellation
        contract) so shutdown still propagates, rather than being logged and
        swallowed as an observability failure."""
        with (
            patch(
                "modulo.core.health_alerts.install_event_loop_stall_attribution",
                side_effect=asyncio.CancelledError,
            ),
            patch("modulo.core.events.register_listeners") as reg,
            pytest.raises(asyncio.CancelledError),
        ):
            await sw._startup_event_bus_hook({})

        reg.assert_not_called()


class TestCapacityProbeInstall:
    """FAR-1992: the worker startup hook is one of the TWO production wiring
    points for the platform capacity probe (the other is the API lifespan).

    Worker-created runs (cron/polling triggers) run the same capacity gate, so
    the probe must be installed in this process too. Every install path is
    fail-open, so a deleted/typo'd call leaves the suite green while the feature
    silently disappears - these tests assert the installed port itself.
    """

    async def test_startup_hook_installs_the_capacity_platform_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = object()
        factory = MagicMock(return_value=probe)
        monkeypatch.setattr("modulo.core.capacity_providers.build_platform_capacity_probe", factory)
        monkeypatch.setattr(db_capacity, "_platform_probe", None)

        with (
            patch("modulo.core.events.register_listeners"),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("modulo.core.events.worker_subscription.spawn_worker_event_subscription"),
        ):
            await sw._startup_event_bus_hook({})

        assert db_capacity._platform_probe is probe
        factory.assert_called_once()

    async def test_probe_is_installed_before_the_no_redis_early_return(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The install must precede the Redis steps' early returns, otherwise a
        # worker deployed without Redis (or with a broken broker) would never
        # get the probe and its cron-created runs would silently skip the gate.
        probe = object()
        monkeypatch.setattr(
            "modulo.core.capacity_providers.build_platform_capacity_probe",
            MagicMock(return_value=probe),
        )
        monkeypatch.setattr(db_capacity, "_platform_probe", None)

        with (
            patch("modulo.core.events.register_listeners"),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock) as cfg,
            patch.object(sw, "get_settings", return_value=_settings(redis_url="")),
        ):
            await sw._startup_event_bus_hook({})

        cfg.assert_not_awaited()
        assert db_capacity._platform_probe is probe

    async def test_factory_failure_is_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            "modulo.core.capacity_providers.build_platform_capacity_probe",
            MagicMock(side_effect=RuntimeError("factory boom")),
        )
        monkeypatch.setattr(db_capacity, "_platform_probe", None)

        with (
            patch("modulo.core.events.register_listeners"),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock) as cfg,
            patch.object(sw, "get_settings", return_value=_settings()),
            patch("modulo.core.events.worker_subscription.spawn_worker_event_subscription"),
            caplog.at_level("WARNING", logger="modulo.core.saq_worker"),
        ):
            await sw._startup_event_bus_hook({})  # must not raise

        assert db_capacity._platform_probe is None
        cfg.assert_awaited_once()
        assert "saq_worker.capacity_platform_probe_failed" in caplog.text


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


class _ReturningPubSub:
    """Pattern subscription whose listen() returns cleanly after one frame.

    Subsequent listen() calls block forever so the resubscribe loop does not
    spin (the real broker always returns a fresh pubsub whose listen() blocks).
    """

    def __init__(self) -> None:
        self.closed = False
        self._calls = 0

    async def listen(self):
        self._calls += 1
        if self._calls > 1:
            await asyncio.Event().wait()
        yield {"type": "psubscribe", "pattern": "modulo:events:resource:*"}

    async def unsubscribe(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class _UnsubRaisesPubSub:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def unsubscribe(self) -> None:
        raise self._exc

    async def aclose(self) -> None:
        return None


class _NoAclosePubSub:
    def __init__(self, close_exc: BaseException | None = None) -> None:
        self.closed = False
        self._exc = close_exc

    async def unsubscribe(self) -> None:
        return None

    async def close(self) -> None:
        if self._exc is not None:
            raise self._exc
        self.closed = True


class _NoUnsubPubSub:
    """Pubsub with neither unsubscribe nor close (exercises the getattr guards)."""

    async def aclose(self) -> None:
        return None


class _NoCloseAtAllPubSub:
    async def unsubscribe(self) -> None:
        return None


class TestWorkerEventSubscriptionEdges:
    async def test_listen_returning_cleanly_resubscribes_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(ws, "_BACKOFF_BASE_SECONDS", 0.01)
        broker = MagicMock()
        broker.connect = AsyncMock()
        broker.subscribe_pattern = AsyncMock(return_value=_ReturningPubSub())

        with caplog.at_level("WARNING", logger="modulo.core.events.worker_subscription"):
            task = asyncio.create_task(ws.run_worker_event_subscription(broker))
            for _ in range(200):
                if "worker_event_subscription.listen_ended" in caplog.text:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert "worker_event_subscription.listen_ended" in caplog.text

    async def test_close_pubsub_unsubscribe_exception_swallowed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("DEBUG", logger="modulo.core.events.worker_subscription"):
            await ws._close_pubsub(_UnsubRaisesPubSub(RuntimeError("boom")))
        assert "worker_event_subscription.unsubscribe_failed" in caplog.text

    async def test_close_pubsub_unsubscribe_cancellation_propagates(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await ws._close_pubsub(_UnsubRaisesPubSub(asyncio.CancelledError()))

    async def test_close_pubsub_falls_back_to_close_without_aclose(self) -> None:
        ps = _NoAclosePubSub()
        await ws._close_pubsub(ps)
        assert ps.closed is True

    async def test_close_pubsub_close_exception_swallowed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("DEBUG", logger="modulo.core.events.worker_subscription"):
            await ws._close_pubsub(_NoAclosePubSub(RuntimeError("close boom")))
        assert "worker_event_subscription.close_failed" in caplog.text

    async def test_close_pubsub_without_unsubscribe_or_close_is_noop(self) -> None:
        assert await ws._close_pubsub(_NoUnsubPubSub()) is None  # must not raise
        assert await ws._close_pubsub(_NoCloseAtAllPubSub()) is None  # must not raise


class TestSpawnWorkerEventSubscription:
    def test_spawn_without_running_loop_returns_none(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="modulo.core.events.worker_subscription"):
            assert ws.spawn_worker_event_subscription(MagicMock()) is None
        assert "worker_event_subscription.spawn_skipped_no_loop" in caplog.text

    async def test_spawn_returns_held_task_and_discards_on_done(self) -> None:
        broker = MagicMock()

        async def _block() -> None:
            await asyncio.Event().wait()

        broker.connect = _block
        broker.subscribe_pattern = AsyncMock()

        task = ws.spawn_worker_event_subscription(broker)
        assert task is not None
        await asyncio.sleep(0.01)
        assert task in ws._held_tasks

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The done callback removed the finished task from the held set.
        assert task not in ws._held_tasks

    async def test_on_task_done_logs_failure(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def _boom() -> None:
            raise RuntimeError("subscription boom")

        task = asyncio.create_task(_boom())
        with contextlib.suppress(RuntimeError):
            await task
        ws._held_tasks.add(task)

        with caplog.at_level("WARNING", logger="modulo.core.events.worker_subscription"):
            ws._on_task_done(task)

        assert task not in ws._held_tasks
        assert "worker_event_subscription.task_failed" in caplog.text


class TestStartupHookCancellationPropagates:
    async def test_register_listeners_cancellation_propagates(self) -> None:
        with (
            patch("modulo.core.events.register_listeners", side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await sw._startup_event_bus_hook({})

    async def test_configure_event_bus_cancellation_propagates(self) -> None:
        with (
            patch("modulo.core.events.register_listeners"),
            patch(
                "modulo.core.events.configure_event_bus",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            patch.object(sw, "get_settings", return_value=_settings()),
            pytest.raises(asyncio.CancelledError),
        ):
            await sw._startup_event_bus_hook({})

    async def test_spawn_cancellation_propagates(self) -> None:
        with (
            patch("modulo.core.events.register_listeners"),
            patch("modulo.core.events.configure_event_bus", new_callable=AsyncMock),
            patch.object(sw, "get_settings", return_value=_settings()),
            patch(
                "modulo.core.events.worker_subscription.spawn_worker_event_subscription",
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await sw._startup_event_bus_hook({})


async def test_on_task_done_successful_task_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    async def _ok() -> None:
        return None

    task = asyncio.create_task(_ok())
    await task
    ws._held_tasks.add(task)

    with caplog.at_level("WARNING", logger="modulo.core.events.worker_subscription"):
        ws._on_task_done(task)

    assert task not in ws._held_tasks
    assert "worker_event_subscription.task_failed" not in caplog.text


class TestRunSystemWebStartupFailure:
    """FAR-2099: a system worker that cannot start must terminate non-zero.

    ``run_system_web`` serves the SAQ web UI in the same process as the system
    crons. If the worker fails to start (a fail-closed startup hook — the
    licence trust-anchor bootstrap — is the case that matters), the process must
    EXIT: an HTTP endpoint that answers while ``fire_due_triggers`` never runs
    is a degraded state an orchestrator reads as healthy.
    """

    #: Bounds the fake serving loop so a regression fails instead of hanging.
    _WATCHDOG_SECONDS = 5.0

    @classmethod
    def _fake_run_app(cls, app: Any, *, host: str, port: int, loop: asyncio.AbstractEventLoop) -> None:  # noqa: ARG003
        """Stand-in for ``aiohttp.web.run_app``, mirroring its loop contract.

        ``run_app`` drives the loop through ``run_until_complete`` on its own
        serving task, so a loop stopped before that task completes surfaces as
        ``RuntimeError`` — the production behaviour the caller handles.
        """
        timed_out: list[bool] = []

        async def _serve_forever() -> None:
            await asyncio.Event().wait()

        def _watchdog() -> None:
            timed_out.append(True)
            loop.stop()

        handle = loop.call_later(cls._WATCHDOG_SECONDS, _watchdog)
        try:
            loop.run_until_complete(_serve_forever())
        except RuntimeError:
            if timed_out:
                raise AssertionError(
                    "run_system_web left the web app serving after the worker failed to start"
                ) from None
            raise
        else:
            if timed_out:
                raise AssertionError("run_system_web left the web app serving after the worker failed to start")
        finally:
            handle.cancel()
            loop.close()

    @classmethod
    def _wire_startup_failure(
        cls,
        monkeypatch: pytest.MonkeyPatch,
        *,
        disconnect_error: Exception | None = None,
    ) -> MagicMock:
        """Wire ``run_system_web`` to a worker whose start raises (fail-closed)."""
        queue = MagicMock()
        queue.connect = AsyncMock()
        queue.disconnect = AsyncMock(side_effect=disconnect_error) if disconnect_error else AsyncMock()

        worker = MagicMock()
        worker.queue = queue
        worker.cron_jobs = []
        # What a fail-closed licence anchor RuntimeError looks like when it
        # escapes ``Worker.start()``'s startup hooks.
        worker.start = AsyncMock(side_effect=RuntimeError("MODULO_LICENSE_PUBLIC_KEY is not configured"))
        worker.stop = AsyncMock()

        monkeypatch.setattr(sw, "_assert_system_auth_configured", MagicMock())
        monkeypatch.setattr(sw, "get_settings", MagicMock(return_value=_settings()))
        monkeypatch.setattr(sw, "_probe_database", MagicMock())
        monkeypatch.setattr(sw, "Worker", MagicMock(return_value=worker))
        monkeypatch.setattr(sw, "system_settings", MagicMock(return_value={"queue": queue}))
        monkeypatch.setattr(sw, "reconcile_cron_registrations", AsyncMock())
        monkeypatch.setattr(sw, "_make_session_factory", MagicMock())
        monkeypatch.setattr(
            "modulo.core.cost_controller.probe.set_duplicate_terminal_cooldown",
            AsyncMock(),
        )
        monkeypatch.setattr("aiohttp.web.run_app", cls._fake_run_app)
        monkeypatch.setattr("saq.web.aiohttp.create_app", MagicMock(return_value=MagicMock()))
        return worker

    def test_startup_failure_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        worker = self._wire_startup_failure(monkeypatch)

        with pytest.raises(SystemExit) as excinfo:
            sw.run_system_web()

        assert excinfo.value.code == 1
        worker.start.assert_awaited_once()
        worker.queue.disconnect.assert_awaited_once()

    def test_startup_failure_exits_nonzero_even_when_disconnect_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A disconnect failure on the half-started worker must not mask the exit.

        This is the FAR-2099 regression: the old ``sys.exit(1)`` lived inside
        the task, so an exception from the ``finally`` disconnect replaced it,
        the done-callback retrieved the replacement, and the web app kept
        serving.
        """
        worker = self._wire_startup_failure(
            monkeypatch,
            disconnect_error=ConnectionError("redis gone during disconnect"),
        )

        with (
            caplog.at_level("ERROR", logger="modulo.core.saq_worker"),
            pytest.raises(SystemExit) as excinfo,
        ):
            sw.run_system_web()

        assert excinfo.value.code == 1
        worker.queue.disconnect.assert_awaited_once()
        assert "saq_worker.system_worker_disconnect_failed" in caplog.text
        assert "saq_worker.system_worker_exiting_nonzero" in caplog.text
