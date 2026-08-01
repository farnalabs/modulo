"""Unit tests for the minimal in-process cron scheduler (modulo.core.scheduler).

Covers engine creation (per-db connect args + failure gate), the polling loop
(due-trigger fire/dispatch fan-out, background-worker submission, error and
cancellation handling, sleep timeout), and the top-level restart-with-backoff
wrapper. Mock/fake based — no real database or Redis involved.
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.scheduler import _run_scheduler_loop, run_scheduler

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_ZERO_ORG = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _make_trigger(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", uuid.uuid4())
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.pipeline_id = overrides.get("pipeline_id", uuid.uuid4())
    t.cron_expression = overrides.get("cron_expression", "0 * * * *")
    t.config_json = overrides.get("config_json")
    return t


class _FakeSession:
    """Async session double: records executed statements, yields fixed scalars."""

    def __init__(self, triggers: list[MagicMock]) -> None:
        self._triggers = list(triggers)
        self.executed: list[object] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
        self.executed.append(stmt)
        result = MagicMock()
        result.scalars.return_value = list(self._triggers)
        return result


class _RaiseOnExecuteSession(_FakeSession):
    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
        raise RuntimeError("boom")


def _make_sleep(stop_event: asyncio.Event, *, after: int = 1, exc: BaseException | None = None):
    """Patched asyncio.sleep: completes instantly, sets stop_event after N calls."""
    calls = {"n": 0}

    async def _sleep(_delay: float) -> None:
        calls["n"] += 1
        if exc is not None:
            raise exc
        if calls["n"] >= after:
            stop_event.set()

    return _sleep


@pytest.fixture
def env() -> SimpleNamespace:
    """Patched module environment; each test wires the fake session/fire result."""
    settings = SimpleNamespace(modulo_db="postgres", database_url="postgresql+asyncpg://u@h/db")
    engine = MagicMock()
    engine.dispose = AsyncMock()
    sessionmaker = MagicMock()
    with (
        patch("modulo.core.scheduler.get_settings", return_value=settings),
        patch("modulo.core.scheduler.create_async_engine", return_value=engine) as create_engine,
        patch("modulo.core.scheduler.async_sessionmaker", return_value=sessionmaker),
        patch("modulo.core.scheduler.set_rls_org", new=AsyncMock()) as set_rls,
        patch("modulo.core.scheduler.fire_cron_trigger", new=AsyncMock(return_value={})) as fire,
        patch("modulo.core.scheduler.dispatch_run", new=AsyncMock()) as dispatch_run,
    ):
        yield SimpleNamespace(
            settings=settings,
            engine=engine,
            create_engine=create_engine,
            sessionmaker=sessionmaker,
            set_rls=set_rls,
            fire=fire,
            dispatch_run=dispatch_run,
            factory=sessionmaker,
        )


async def _run_loop(env: SimpleNamespace, *, bg_worker: object | None = None) -> asyncio.Event:
    stop_event = asyncio.Event()
    with patch("modulo.core.scheduler.asyncio.sleep", new=_make_sleep(stop_event)):
        await _run_scheduler_loop(stop_event, bg_worker=bg_worker)
    return stop_event


# ---------------------------------------------------------------------------
# Engine creation
# ---------------------------------------------------------------------------


async def test_loop_creates_postgres_engine_with_ssl_connect_args(env: SimpleNamespace) -> None:
    stop_event = asyncio.Event()
    stop_event.set()

    with patch("modulo.core.scheduler.asyncio.sleep", new=AsyncMock()):
        await _run_scheduler_loop(stop_event)

    assert env.create_engine.call_count == 1
    args, kwargs = env.create_engine.call_args
    assert args[0] == env.settings.database_url
    assert kwargs["pool_size"] == 1
    assert kwargs["max_overflow"] == 2
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["connect_args"]["ssl"] is False
    assert kwargs["connect_args"]["statement_cache_size"] == 0
    env.engine.dispose.assert_awaited_once()


async def test_loop_creates_sqlite_engine_without_ssl_connect_args(env: SimpleNamespace) -> None:
    env.settings.modulo_db = "sqlite"
    env.settings.database_url = "sqlite+aiosqlite:///db.sqlite"
    stop_event = asyncio.Event()
    stop_event.set()

    with patch("modulo.core.scheduler.asyncio.sleep", new=AsyncMock()):
        await _run_scheduler_loop(stop_event)

    _, kwargs = env.create_engine.call_args
    assert "ssl" not in kwargs["connect_args"]
    assert "statement_cache_size" not in kwargs["connect_args"]
    assert kwargs["connect_args"]["timeout"] == 10


async def test_loop_returns_when_settings_unavailable(env: SimpleNamespace) -> None:
    env.settings = SimpleNamespace(modulo_db="postgres", database_url="x")
    with patch("modulo.core.scheduler.get_settings", side_effect=RuntimeError("no settings")):
        await _run_scheduler_loop(asyncio.Event())

    env.create_engine.assert_not_called()
    env.engine.dispose.assert_not_awaited()


async def test_loop_returns_when_engine_creation_fails(env: SimpleNamespace) -> None:
    with patch("modulo.core.scheduler.create_async_engine", side_effect=RuntimeError("db down")):
        await _run_scheduler_loop(asyncio.Event())

    env.engine.dispose.assert_not_awaited()


# ---------------------------------------------------------------------------
# Polling loop behaviour
# ---------------------------------------------------------------------------


async def test_loop_breaks_immediately_when_stop_event_set(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    stop_event = asyncio.Event()
    stop_event.set()

    with patch("modulo.core.scheduler.asyncio.sleep", new=AsyncMock()):
        await _run_scheduler_loop(stop_event)

    session = env.factory.return_value
    assert session.executed == []
    env.fire.assert_not_awaited()
    env.engine.dispose.assert_awaited_once()


async def test_loop_fires_due_trigger_and_submits_to_bg_worker(env: SimpleNamespace) -> None:
    trigger = _make_trigger(id=_ORG_ID)
    env.factory.return_value = _FakeSession([trigger])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID), "input_payload": {"k": "v"}}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    await _run_loop(env, bg_worker=bg_worker)

    env.set_rls.assert_awaited_once()
    rls_call = env.set_rls.await_args
    assert rls_call.args[0] is env.factory.return_value
    assert rls_call.args[1] == _ZERO_ORG

    env.fire.assert_awaited_once_with(
        trigger_id=trigger.id,
        org_id=trigger.organisation_id,
        pipeline_id=trigger.pipeline_id,
        cron_expression=trigger.cron_expression,
        snapshot_id=None,
        factory=env.factory,
    )
    bg_worker.submit.assert_called_once_with(
        run_id=_RUN_ID,
        org_id=trigger.organisation_id,
        input_payload={"k": "v"},
    )
    env.dispatch_run.assert_not_awaited()


async def test_loop_dispatches_via_dispatch_run_when_no_bg_worker(env: SimpleNamespace) -> None:
    trigger = _make_trigger()
    env.factory.return_value = _FakeSession([trigger])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID)}

    await _run_loop(env)

    env.dispatch_run.assert_awaited_once_with(
        str(_RUN_ID),
        str(trigger.organisation_id),
        queue="runs",
        celery_queue="runs_automated",
    )


async def test_loop_fires_all_due_triggers(env: SimpleNamespace) -> None:
    t_a = _make_trigger()
    t_b = _make_trigger()
    env.factory.return_value = _FakeSession([t_a, t_b])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID)}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    await _run_loop(env, bg_worker=bg_worker)

    assert env.fire.await_count == 2
    assert bg_worker.submit.call_count == 2


async def test_loop_passes_snapshot_id_from_config_json(env: SimpleNamespace) -> None:
    trigger = _make_trigger(config_json={"snapshot_id": _SNAPSHOT_ID})
    env.factory.return_value = _FakeSession([trigger])
    env.fire.return_value = {"status": "skipped", "reason": "trigger_busy"}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    await _run_loop(env, bg_worker=bg_worker)

    env.fire.assert_awaited_once_with(
        trigger_id=trigger.id,
        org_id=trigger.organisation_id,
        pipeline_id=trigger.pipeline_id,
        cron_expression=trigger.cron_expression,
        snapshot_id=_SNAPSHOT_ID,
        factory=env.factory,
    )
    bg_worker.submit.assert_not_called()


async def test_loop_does_not_submit_when_not_fired(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.return_value = {"status": "skipped", "reason": "trigger_inactive_or_missing"}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    await _run_loop(env, bg_worker=bg_worker)

    bg_worker.submit.assert_not_called()
    env.dispatch_run.assert_not_awaited()


async def test_loop_does_not_submit_when_fired_without_run_id(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.return_value = {"status": "fired"}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    await _run_loop(env, bg_worker=bg_worker)

    bg_worker.submit.assert_not_called()
    env.dispatch_run.assert_not_awaited()


async def test_loop_survives_bg_worker_submit_error(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID)}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock(side_effect=RuntimeError("worker full"))

    await _run_loop(env, bg_worker=bg_worker)

    bg_worker.submit.assert_called_once()
    env.engine.dispose.assert_awaited_once()


async def test_loop_survives_dispatch_run_error(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID)}
    env.dispatch_run.side_effect = RuntimeError("broker down")

    await _run_loop(env)

    env.dispatch_run.assert_awaited_once()
    env.engine.dispose.assert_awaited_once()


async def test_loop_survives_fire_error_and_continues(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.side_effect = RuntimeError("fire failed")
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    await _run_loop(env, bg_worker=bg_worker)

    bg_worker.submit.assert_not_called()
    env.engine.dispose.assert_awaited_once()


async def test_loop_survives_query_error(env: SimpleNamespace) -> None:
    env.factory.return_value = _RaiseOnExecuteSession([])

    await _run_loop(env)

    env.engine.dispose.assert_awaited_once()


async def test_loop_breaks_on_cancellation_in_body(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.side_effect = asyncio.CancelledError()

    stop_event = asyncio.Event()
    with patch("modulo.core.scheduler.asyncio.sleep", new=AsyncMock()):
        await _run_scheduler_loop(stop_event)

    env.engine.dispose.assert_awaited_once()


async def test_loop_breaks_on_cancellation_during_sleep(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([])

    stop_event = asyncio.Event()
    with patch(
        "modulo.core.scheduler.asyncio.sleep",
        new=_make_sleep(stop_event, exc=asyncio.CancelledError()),
    ):
        await _run_scheduler_loop(stop_event)

    env.engine.dispose.assert_awaited_once()


async def test_loop_passes_on_sleep_timeout_and_keeps_running(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID)}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()
    stop_event = asyncio.Event()

    calls = {"n": 0}

    async def _sleep_timeout(_delay: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            stop_event.set()
            raise TimeoutError()

    with patch("modulo.core.scheduler.asyncio.sleep", new=_sleep_timeout):
        await _run_scheduler_loop(stop_event, bg_worker=bg_worker)

    assert calls["n"] == 1
    bg_worker.submit.assert_called_once()
    env.engine.dispose.assert_awaited_once()


async def test_loop_breaks_before_sleep_when_stop_set_mid_iteration(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    stop_event = asyncio.Event()

    def _set_stop(*args: object, **kwargs: object) -> MagicMock:
        stop_event.set()
        return MagicMock(scalars=MagicMock(return_value=[]))

    env.factory.return_value.execute = AsyncMock(side_effect=_set_stop)

    with patch("modulo.core.scheduler.asyncio.sleep", new=AsyncMock()) as sleep:
        await _run_scheduler_loop(stop_event)

    sleep.assert_not_awaited()
    env.engine.dispose.assert_awaited_once()


async def test_loop_logs_unexpected_sleep_error_and_stops(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID)}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    async def _sleep_boom(_delay: float) -> None:
        raise RuntimeError("unexpected")

    with patch("modulo.core.scheduler.asyncio.sleep", new=_sleep_boom):
        await _run_scheduler_loop(asyncio.Event(), bg_worker=bg_worker)

    bg_worker.submit.assert_called_once()
    env.engine.dispose.assert_awaited_once()


async def test_loop_disposes_engine_exactly_once(env: SimpleNamespace) -> None:
    env.factory.return_value = _FakeSession([_make_trigger()])
    env.fire.return_value = {"status": "fired", "run_id": str(_RUN_ID)}
    bg_worker = MagicMock()
    bg_worker.submit = MagicMock()

    await _run_loop(env, bg_worker=bg_worker)

    bg_worker.submit.assert_called_once()
    env.engine.dispose.assert_awaited_once()


# ---------------------------------------------------------------------------
# run_scheduler restart wrapper
# ---------------------------------------------------------------------------


async def test_run_scheduler_returns_on_clean_exit() -> None:
    stop_event = asyncio.Event()
    bg_worker = MagicMock()
    with (
        patch("modulo.core.scheduler._run_scheduler_loop", new=AsyncMock()) as inner,
        patch("modulo.core.scheduler.asyncio.sleep", new=AsyncMock()),
    ):
        await run_scheduler(stop_event, bg_worker)

    inner.assert_awaited_once_with(stop_event, bg_worker)


async def test_run_scheduler_returns_on_cancellation() -> None:
    with (
        patch(
            "modulo.core.scheduler._run_scheduler_loop",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ) as inner,
        patch("modulo.core.scheduler.asyncio.sleep", new=AsyncMock()),
    ):
        await run_scheduler(asyncio.Event())

    inner.assert_awaited_once()


async def test_run_scheduler_restarts_after_unexpected_crash() -> None:
    sleep_calls: list[float] = []
    with patch(
        "modulo.core.scheduler._run_scheduler_loop",
        new=AsyncMock(side_effect=[RuntimeError("crash"), None]),
    ) as inner:

        async def _sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("modulo.core.scheduler.asyncio.sleep", new=_sleep):
            await run_scheduler(asyncio.Event())

    assert inner.await_count == 2
    assert sleep_calls == [1.0]


async def test_run_scheduler_backoff_doubles_before_recovery() -> None:
    sleep_calls: list[float] = []
    with patch(
        "modulo.core.scheduler._run_scheduler_loop",
        new=AsyncMock(side_effect=[RuntimeError("a"), RuntimeError("b"), None]),
    ) as inner:

        async def _sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("modulo.core.scheduler.asyncio.sleep", new=_sleep):
            await run_scheduler(asyncio.Event())

    assert inner.await_count == 3
    assert sleep_calls == [1.0, 2.0]
