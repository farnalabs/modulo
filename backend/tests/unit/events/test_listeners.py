"""Unit tests for SQLAlchemy event listeners that publish to the EventBus.

Covers listener factory behaviour, org/action resolution, version counters,
background-task lifecycle, and idempotent registration — all without a DB.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

import modulo.core.events.listeners as listeners
from modulo.core.events.listeners import _make_listener, _safe_str_attr, register_listeners
from modulo.db.models.agent import Agent
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset module-level globals that tests mutate."""
    listeners._background_tasks.clear()
    listeners._version_counters.clear()
    listeners._listeners_registered = False
    yield
    listeners._background_tasks.clear()
    listeners._version_counters.clear()
    listeners._listeners_registered = False


@pytest.fixture
def fake_bus() -> AsyncMock:
    """An AsyncMock EventBus whose publish() records calls."""
    return AsyncMock()


async def _drain_tasks(wait: float = 0.05) -> None:
    """Let background tasks created by a listener finish."""
    for _ in range(50):
        if not listeners._background_tasks:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(wait / 50)


# ---------------------------------------------------------------------------
# _safe_str_attr
# ---------------------------------------------------------------------------


class _RaisingAttr:
    @property
    def organisation_id(self) -> str:
        raise RuntimeError("attribute exploded")


def test_safe_str_attr_returns_string() -> None:
    run = Run(organisation_id="org-1", id="run-1")
    assert _safe_str_attr(run, "organisation_id", "run", "created") == "org-1"


def test_safe_str_attr_non_string_value_is_stringified(caplog: pytest.LogCaptureFixture) -> None:
    run = Run(organisation_id=12345, id="run-1")
    assert _safe_str_attr(run, "organisation_id", "run", "created") == "12345"


def test_safe_str_attr_none_value_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    run = Run(organisation_id=None, id="run-1")
    assert _safe_str_attr(run, "organisation_id", "run", "created") is None
    assert "event_listener.null_organisation_id" in caplog.text


def test_safe_str_attr_raises_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    target = _RaisingAttr()
    assert _safe_str_attr(target, "organisation_id", "run", "created") is None
    assert "event_listener.attr_error_organisation_id" in caplog.text


def test_safe_str_attr_missing_attribute_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    run = Run(organisation_id="org-1", id="run-1")
    assert _safe_str_attr(run, "nonexistent_attr", "run", "created") is None
    assert "event_listener.null_nonexistent_attr" in caplog.text


# ---------------------------------------------------------------------------
# Listener happy paths
# ---------------------------------------------------------------------------


async def test_listener_publishes_event_for_known_model(fake_bus: AsyncMock) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        listener = _make_listener("after_insert")
        run = Run(organisation_id="org-1", id="run-1")
        listener(None, None, run)

    await _drain_tasks()
    fake_bus.publish.assert_awaited_once()
    kwargs = fake_bus.publish.await_args.kwargs
    assert kwargs["org_id"] == "org-1"
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == "run-1"
    assert kwargs["action"] == "created"
    assert kwargs["version"] == 1


async def test_listener_maps_each_action(fake_bus: AsyncMock) -> None:
    expected = {"after_insert": "created", "after_update": "updated", "after_delete": "deleted"}
    for action, action_name in expected.items():
        fake_bus.publish.reset_mock()
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _make_listener(action)(None, None, Run(organisation_id="org-1", id="run-1"))
        await _drain_tasks()
        assert fake_bus.publish.await_args.kwargs["action"] == action_name


async def test_listener_uses_model_specific_resource_type(fake_bus: AsyncMock) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener("after_insert")(None, None, Pipeline(organisation_id="org-2", id="pipe-1"))
        _make_listener("after_update")(None, None, Agent(organisation_id="org-2", id="agent-1"))

    await _drain_tasks()
    calls = fake_bus.publish.await_args_list
    assert [call.kwargs["resource_type"] for call in calls] == ["pipeline", "agent"]


async def test_listener_increments_version_per_org(fake_bus: AsyncMock) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        listener = _make_listener("after_insert")
        listener(None, None, Run(organisation_id="org-a", id="r1"))
        listener(None, None, Run(organisation_id="org-a", id="r2"))
        listener(None, None, Run(organisation_id="org-b", id="r3"))

    await _drain_tasks()
    versions = [call.kwargs["version"] for call in fake_bus.publish.await_args_list]
    assert versions == [1, 2, 1]


# ---------------------------------------------------------------------------
# Listener skip / warning paths
# ---------------------------------------------------------------------------


async def test_unknown_model_is_skipped(fake_bus: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener("after_insert")(None, None, object())

    await _drain_tasks()
    fake_bus.publish.assert_not_awaited()
    assert "event_listener.unknown_model" in caplog.text


async def test_unknown_action_is_skipped(fake_bus: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener("before_insert")(None, None, Run(organisation_id="org-1", id="run-1"))

    await _drain_tasks()
    fake_bus.publish.assert_not_awaited()
    assert "event_listener.unknown_action" in caplog.text


async def test_missing_organisation_id_is_skipped(fake_bus: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener("after_insert")(None, None, Run(organisation_id=None, id="run-1"))

    await _drain_tasks()
    fake_bus.publish.assert_not_awaited()
    assert "event_listener.null_organisation_id" in caplog.text


async def test_missing_resource_id_is_skipped(fake_bus: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener("after_insert")(None, None, Run(organisation_id="org-1", id=None))

    await _drain_tasks()
    fake_bus.publish.assert_not_awaited()
    assert "event_listener.null_id" in caplog.text


def test_no_running_loop_skips_publish(caplog: pytest.LogCaptureFixture) -> None:
    """Listener invoked outside an event loop must warn and not crash."""
    with patch.object(listeners, "get_event_bus") as mock_get_bus:
        _make_listener("after_insert")(None, None, Run(organisation_id="org-1", id="run-1"))

    mock_get_bus.assert_not_called()
    assert "event_listener.no_running_loop" in caplog.text


async def test_publish_failure_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    async def failing_publish(**_: object) -> None:
        raise RuntimeError("redis unreachable")

    fake_bus = AsyncMock()
    fake_bus.publish = failing_publish
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener("after_insert")(None, None, Run(organisation_id="org-1", id="run-1"))

    await _drain_tasks()
    assert not listeners._background_tasks
    assert "event_listener.publish_failed" in caplog.text
    assert "redis unreachable" in caplog.text


async def test_cancelled_task_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    async def long_publish(**_: object) -> None:
        await asyncio.sleep(60)

    fake_bus = AsyncMock()
    fake_bus.publish = long_publish
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener("after_insert")(None, None, Run(organisation_id="org-1", id="run-1"))

    await asyncio.sleep(0.01)
    assert listeners._background_tasks, "listener should have spawned a background task"
    task = next(iter(listeners._background_tasks))
    task.cancel()
    await asyncio.sleep(0.01)

    assert not listeners._background_tasks
    assert "event_listener.task_cancelled" in caplog.text


# ---------------------------------------------------------------------------
# register_listeners
# ---------------------------------------------------------------------------


def test_register_listeners_registers_all_models_and_actions() -> None:
    model_count = len(listeners._RESOURCE_TYPES)
    with patch("sqlalchemy.event.listen") as mock_listen:
        register_listeners()

    assert mock_listen.call_count == model_count * 3
    models = {call.args[0] for call in mock_listen.call_args_list}
    assert models == set(listeners._RESOURCE_TYPES)
    actions = {call.args[1] for call in mock_listen.call_args_list}
    assert actions == {"after_insert", "after_update", "after_delete"}
    assert listeners._listeners_registered is True


def test_register_listeners_is_idempotent(caplog: pytest.LogCaptureFixture) -> None:
    with patch("sqlalchemy.event.listen") as mock_listen:
        register_listeners()
        first_count = mock_listen.call_count
        register_listeners()

    assert mock_listen.call_count == first_count
    assert "event_listeners.already_registered" in caplog.text
