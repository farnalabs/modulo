"""Unit tests for SQLAlchemy event listeners that publish to the EventBus.

Covers listener factory behaviour, org/action resolution, version counters,
background-task lifecycle, and idempotent registration — all without a DB —
plus the FAR-250 commit-deferred semantics: exactly-one-event per commit,
rollback-no-phantom, notifier-session Redis suppression, and the
notification-only payload fields (never content).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from modulo.core.events import listeners
from modulo.core.events.listeners import (
    _make_listener,
    _PendingEvent,
    _queue_for_commit,
    _safe_str_attr,
    register_listeners,
)
from modulo.core.events.notification_events import NOTIFIER_SESSION_KEY
from modulo.db.models.agent import Agent
from modulo.db.models.notification import Notification
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from tests.unit.events._helpers import drain_background_tasks


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


async def _drain_tasks() -> None:
    """Let background tasks created by a listener finish."""
    await drain_background_tasks(listeners._background_tasks)


# ---------------------------------------------------------------------------
# _safe_str_attr
# ---------------------------------------------------------------------------


class _RaisingAttr:
    @property
    def organisation_id(self) -> str:
        raise RuntimeError("attribute exploded")


@pytest.mark.parametrize(
    ("target", "attr", "expected", "expected_log"),
    [
        pytest.param(
            Run(organisation_id="org-1", id="run-1"),
            "organisation_id",
            "org-1",
            None,
            id="string-value-passes-through",
        ),
        pytest.param(
            Run(organisation_id=12345, id="run-1"),
            "organisation_id",
            "12345",
            None,
            id="non-string-value-stringified",
        ),
        pytest.param(
            Run(organisation_id=None, id="run-1"),
            "organisation_id",
            None,
            "event_listener.null_organisation_id",
            id="none-value-warns",
        ),
        pytest.param(
            _RaisingAttr(),
            "organisation_id",
            None,
            "event_listener.attr_error_organisation_id",
            id="raising-attr-warns",
        ),
        pytest.param(
            Run(organisation_id="org-1", id="run-1"),
            "nonexistent_attr",
            None,
            "event_listener.null_nonexistent_attr",
            id="missing-attr-warns",
        ),
    ],
)
def test_safe_str_attr(
    target: object,
    attr: str,
    expected: str | None,
    expected_log: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reads the attribute, stringifies non-strings, warns + returns None on failure."""
    with caplog.at_level("WARNING", logger="modulo.core.events.listeners"):
        assert _safe_str_attr(target, attr, "run", "created") == expected

    if expected_log is None:
        assert not caplog.text
    else:
        assert expected_log in caplog.text


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


@pytest.mark.parametrize(
    ("sqlalchemy_action", "expected_action_name"),
    [
        pytest.param("after_insert", "created", id="after_insert->created"),
        pytest.param("after_update", "updated", id="after_update->updated"),
        pytest.param("after_delete", "deleted", id="after_delete->deleted"),
    ],
)
async def test_listener_maps_each_action(
    sqlalchemy_action: str,
    expected_action_name: str,
    fake_bus: AsyncMock,
) -> None:
    with patch.object(listeners, "get_event_bus", return_value=fake_bus):
        _make_listener(sqlalchemy_action)(None, None, Run(organisation_id="org-1", id="run-1"))

    await _drain_tasks()
    fake_bus.publish.assert_awaited_once()
    assert fake_bus.publish.await_args.kwargs["action"] == expected_action_name


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


def test_eval_audit_registration_covers_the_live_eval_model() -> None:
    """FAR-1961: eval audit events stay registered against the LIVE ``Eval`` model.

    The legacy ``EvalDefinition`` ORM entity was deleted. Removing its map entry
    without substituting ``Eval`` would silently de-register every eval audit
    event — no error, no other test failure. This pins the substitution: the map
    must resolve ``Eval`` to the ``"eval"`` resource type, and registration must
    attach all three actions to it.
    """
    from modulo.db.models.eval import Eval

    assert listeners._RESOURCE_TYPES[Eval] == "eval"

    with patch("sqlalchemy.event.listen") as mock_listen:
        register_listeners()

    eval_calls = [call for call in mock_listen.call_args_list if call.args[0] is Eval]
    actions = {call.args[1] for call in eval_calls}
    assert actions == {"after_insert", "after_update", "after_delete"}


# ---------------------------------------------------------------------------
# FAR-250: commit-deferred delivery (exactly-one-event + rollback-no-phantom)
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Iterator[Session]:
    """A real sync Session on in-memory sqlite (no tables needed).

    Queue/commit/rollback mechanics only — no INSERT is ever executed, so
    no table creation is required. Teardown always rolls back, closes and
    disposes, so a failing assertion can never leak the connection/engine.
    """
    engine = create_engine("sqlite://")
    sess = Session(bind=engine)
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()
        engine.dispose()


def _notification() -> Notification:
    # id is passed explicitly: the ORM default fires at flush, but these tests
    # invoke the listener pre-flush, exactly where mapper events would not yet
    # have a generated id if the default were left to the flush.
    return Notification(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        scope="org",
        level="info",
        category="run_failed",
        title="Run failed",
        body="secret body content must never appear in the SSE payload",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


def _pending(**overrides: object) -> _PendingEvent:
    base: dict[str, object] = {
        "org_id": "org-1",
        "resource_type": "run",
        "resource_id": "run-1",
        "action_name": "created",
        "version": 1,
        "broadcast_redis": True,
        "extra": {},
    }
    base.update(overrides)
    return _PendingEvent(**base)  # type: ignore[arg-type]


class TestCommitDeferredDelivery:
    async def test_listener_with_session_queues_instead_of_publishing(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        """A session-owned target must NOT publish at flush time (deferred to commit)."""
        run = Run(organisation_id="org-1", id="run-1")
        session.add(run)
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _make_listener("after_insert")(None, None, run)

        await _drain_tasks()
        fake_bus.publish.assert_not_awaited()
        queued = session.info.get(listeners._PENDING_KEY)
        assert queued is not None
        assert len(queued) == 1
        assert queued[0].resource_id == "run-1"

    async def test_commit_delivers_exactly_one_event(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        """exactly-one-event: one queued snapshot -> one publish on commit."""
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _queue_for_commit(session, _pending())
            await _drain_tasks()
            fake_bus.publish.assert_not_awaited()  # nothing before commit

            session.commit()
            await _drain_tasks()

        fake_bus.publish.assert_awaited_once()
        kwargs = fake_bus.publish.await_args.kwargs
        assert kwargs["org_id"] == "org-1"
        assert kwargs["resource_id"] == "run-1"
        assert kwargs["version"] == 1

    async def test_commit_delivers_every_queued_snapshot(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _queue_for_commit(session, _pending(resource_id="run-1"))
            _queue_for_commit(session, _pending(resource_id="run-2", version=2))
            session.commit()
            await _drain_tasks()

        ids = [call.kwargs["resource_id"] for call in fake_bus.publish.await_args_list]
        assert ids == ["run-1", "run-2"]

    async def test_rollback_drops_pending_no_phantom(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        """rollback-no-phantom: an outermost rollback emits nothing.

        Under the pre-FAR-250 listener this scenario published at flush
        time, before the rollback — the phantom this test locks out.
        """
        session.begin()  # rollback is a no-op without a begun transaction
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _queue_for_commit(session, _pending())
            session.rollback()
            await _drain_tasks()

        fake_bus.publish.assert_not_awaited()
        assert session.info.get(listeners._PENDING_KEY) is None

    async def test_listener_rollback_after_queue_emits_nothing(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        """Full path: listener queues for a session-owned target, then rollback."""
        run = Run(organisation_id="org-1", id="run-1")
        session.add(run)
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _make_listener("after_insert")(None, None, run)
            session.rollback()
            await _drain_tasks()

        fake_bus.publish.assert_not_awaited()

    async def test_inner_savepoint_rollback_keeps_pending(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        """An INNER ``begin_nested()`` savepoint rollback must keep the queue.

        Drives a REAL savepoint rollback through the registered
        ``after_soft_rollback`` hook — SQLAlchemy invokes it as
        ``(session, previous_transaction)`` where the second argument is a
        truthy ``SessionTransaction``, never ``outter=False``. Pending events
        queued by an earlier flush survive the inner rollback and are still
        delivered by the enclosing transaction's commit (the dominant
        ``begin_nested()`` bounded-retry shape in this codebase).
        """
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _queue_for_commit(session, _pending())  # registers the real event hooks
            session.begin()
            session.begin_nested().rollback()  # inner savepoint rollback -> fires the hook
            queued = session.info.get(listeners._PENDING_KEY)
            assert queued is not None, "inner savepoint rollback must not drop pending events"
            assert len(queued) == 1

            session.commit()  # enclosing transaction still owns the queue
            await _drain_tasks()

        fake_bus.publish.assert_awaited_once()
        assert fake_bus.publish.await_args.kwargs["resource_id"] == "run-1"

    def test_outermost_rollback_drops_pending_via_event_hook(self, session: Session) -> None:
        """The OUTERMOST rollback clears the queue through the same real hook.

        Sequence: queue -> inner savepoint rollback (keeps) -> root rollback
        (drops). Proves the hook distinguishes the two rollback kinds by real
        session state, not by the second argument's truthiness.
        """
        _queue_for_commit(session, _pending())  # registers the real event hooks
        session.begin()
        session.begin_nested().rollback()  # inner savepoint rollback
        assert session.info.get(listeners._PENDING_KEY) is not None, "inner savepoint rollback must keep pending events"
        session.rollback()  # outermost rollback -> fires the hook again
        assert session.info.get(listeners._PENDING_KEY) is None, (
            "outermost rollback must drop pending events (rollback-no-phantom)"
        )


class TestNotificationListenerPayload:
    """FAR-250: notification listener behaviour — suppression + payload."""

    def _queued_pending(self, session: Session, *, notifier_owned: bool) -> _PendingEvent:
        notification = _notification()
        session.add(notification)
        if notifier_owned:
            session.info[NOTIFIER_SESSION_KEY] = True
        _make_listener("after_insert")(None, None, notification)
        queued = session.info.get(listeners._PENDING_KEY)
        assert queued is not None
        return queued[-1]

    def test_notifier_owned_session_suppresses_listener_redis_leg(self, session: Session) -> None:
        pending = self._queued_pending(session, notifier_owned=True)
        assert pending.broadcast_redis is False

    def test_non_notifier_session_keeps_listener_redis_leg(self, session: Session) -> None:
        """Direct creates (health probe, CRUD) keep the listener's Redis leg."""
        pending = self._queued_pending(session, notifier_owned=False)
        assert pending.broadcast_redis is True

    def test_payload_carries_only_allowed_notification_fields(self, session: Session) -> None:
        """Exactly {notification_id, category, created_at} + event_id — never content."""
        pending = self._queued_pending(session, notifier_owned=True)
        assert pending.extra is not None
        assert set(pending.extra) == {"notification_id", "category", "created_at", "event_id"}
        assert pending.extra["category"] == "run_failed"
        assert pending.extra["created_at"] is not None
        assert pending.extra["event_id"].endswith(":created")
        # Content must never be captured for the org-wide SSE fan-out.
        assert "title" not in pending.extra
        assert "body" not in pending.extra
        assert "action_url" not in pending.extra

    async def test_notification_commit_publishes_once_with_payload(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        """End-to-end through commit: one local publish carrying the payload.

        The instance is expunged before commit so no INSERT executes — that
        keeps the test independent of whether this process has globally
        registered the real mapper listeners (other test files do), which
        would otherwise fire a second, duplicate listener at flush time.
        """
        notification = _notification()
        session.add(notification)
        session.info[NOTIFIER_SESSION_KEY] = True
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _make_listener("after_insert")(None, None, notification)
            session.expunge(notification)
            session.commit()
            await _drain_tasks()

        fake_bus.publish.assert_awaited_once()
        kwargs = fake_bus.publish.await_args.kwargs
        assert kwargs["broadcast_redis"] is False
        extra = kwargs["extra"]
        assert extra["notification_id"] == str(notification.id)
        assert extra["category"] == "run_failed"
        assert "body" not in extra

    async def test_notification_rollback_emits_nothing(
        self,
        fake_bus: AsyncMock,
        session: Session,
    ) -> None:
        """A rolled-back notification create emits neither local nor Redis."""
        notification = _notification()
        session.add(notification)
        session.info[NOTIFIER_SESSION_KEY] = True
        with patch.object(listeners, "get_event_bus", return_value=fake_bus):
            _make_listener("after_insert")(None, None, notification)
            session.rollback()
            await _drain_tasks()

        fake_bus.publish.assert_not_awaited()


# ---------------------------------------------------------------------------
# _deliver_pending / _resolve_session edge paths (FAR-250 coverage)
# ---------------------------------------------------------------------------


def test_deliver_pending_without_running_loop_warns(caplog: pytest.LogCaptureFixture, session: Session) -> None:
    """A queued event delivered with no running loop warns and is not lost."""
    _queue_for_commit(session, _pending())
    with caplog.at_level("WARNING", logger="modulo.core.events.listeners"):
        listeners._deliver_pending(session)
    assert "event_listener.no_running_loop" in caplog.text


def test_resolve_session_object_session_raises_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """object_session raising (detached/mock target) falls through to no session."""
    monkeypatch.setattr(listeners, "object_session", MagicMock(side_effect=RuntimeError("boom")))
    assert listeners._resolve_session(object()) is None


def test_resolve_session_non_session_value_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Session object_session result (mock target) returns None."""
    monkeypatch.setattr(listeners, "object_session", MagicMock(return_value=MagicMock()))
    assert listeners._resolve_session(object()) is None


def test_deliver_pending_with_no_pending_is_noop(session: Session) -> None:
    """after_commit with an empty queue returns immediately (no loop needed)."""
    assert listeners._deliver_pending(session) is None  # no pending queued
    assert session.info.get(listeners._PENDING_KEY) is None
    assert not listeners._background_tasks


def test_on_soft_rollback_without_pending_is_noop(
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An outermost rollback with no queued events logs nothing and returns."""
    assert session.in_transaction() is False
    with caplog.at_level("INFO", logger="modulo.core.events.listeners"):
        listeners._on_soft_rollback(session, MagicMock())

    # Nothing was queued, so the hook must not invent a queue and must not
    # report a drop it never performed.
    assert listeners._PENDING_KEY not in session.info
    assert "event_listener.pending_dropped_on_rollback" not in caplog.text
