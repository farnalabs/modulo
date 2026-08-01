"""Unit tests for DatabaseCronEntry — Celery beat entry lifecycle."""

import datetime
import uuid
from contextlib import ExitStack
from decimal import Decimal
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from celery.beat import Scheduler
from sqlalchemy.sql.dml import Update

from modulo.core.cron_scheduler import (
    DatabaseCronEntry,
    DatabaseCronScheduler,
    _get_engine,
    fire_cron_trigger,
)
from modulo.core.pipeline_executor_task import SchedulerDBError
from modulo.db.models.trigger import Trigger
from modulo.settings import Settings


def _make_mock_settings() -> MagicMock:
    """Return a mock Settings instance with valid DB URL and secrets."""
    return MagicMock(
        spec=Settings,
        modulo_db="postgres",
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        redis_url="redis://localhost:6379/0",
        modulo_admin_password="test",
        openai_api_key=None,
        anthropic_api_key=None,
    )


def _make_trigger(**overrides: object) -> MagicMock:
    """Return a Trigger mock with sensible defaults for cron firing."""
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "pipeline_id": uuid.uuid4(),
        "active": True,
        "max_concurrent_runs": 5,
        "daily_spend_limit": None,
        "cron_timezone": "UTC",
        "config_json": {},
    }
    defaults.update(overrides)
    trigger = MagicMock(spec=Trigger)
    for key, value in defaults.items():
        setattr(trigger, key, value)
    return trigger


class _MockBegin:
    """Async context manager for session.begin()."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Replacement for an async DB session in unit tests.

    Supports ``async with session:``, ``async with session.begin():``,
    ``await session.execute(...)``, ``session.add()``, and
    ``await session.flush()``.
    """

    def __init__(self, execute_side_effect: list[MagicMock] | None = None) -> None:
        self._execute_mock = AsyncMock(side_effect=execute_side_effect or [])
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return _MockBegin()

    async def execute(self, *args: object, **kwargs: object) -> MagicMock:
        return await self._execute_mock(*args, **kwargs)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


class _MockSessionFactory:
    """Callable that returns a _MockSession."""

    def __init__(self, session: _MockSession) -> None:
        self._session = session

    def __call__(self) -> _MockSession:
        return self._session


async def _run_fire(
    session: _MockSession,
    *,
    trigger_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID | None = None,
    cron_expression: str = "* * * * *",
    active_count: int = 0,
    create_run_return: MagicMock | None = None,
    log_event_return: MagicMock | None = None,
) -> tuple[dict, dict[str, MagicMock | AsyncMock]]:
    """Invoke ``fire_cron_trigger`` under the standard patch set.

    Returns ``(result, mocks)`` so callers can assert on the patched
    ``_set_rls_org``, ``_count_active_runs``, ``_log_event``, and
    ``create_run`` mocks after the call completes.
    """
    with ExitStack() as stack:
        stack.enter_context(patch("modulo.core.cron_scheduler._get_engine"))
        stack.enter_context(
            patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=_MockSessionFactory(session))
        )
        mock_rls = stack.enter_context(patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock))
        mock_count = stack.enter_context(
            patch("modulo.core.cron_scheduler._count_active_runs", new_callable=AsyncMock, return_value=active_count)
        )
        mock_log_event = stack.enter_context(patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock))
        mock_create_run = stack.enter_context(patch("modulo.core.cron_scheduler.create_run", new_callable=AsyncMock))
        if create_run_return is not None:
            mock_create_run.return_value = create_run_return
        if log_event_return is not None:
            mock_log_event.return_value = log_event_return
        result = await fire_cron_trigger(
            trigger_id=trigger_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            cron_expression=cron_expression,
        )
    return result, {"rls": mock_rls, "count": mock_count, "log_event": mock_log_event, "create_run": mock_create_run}


class TestGetEngine:
    def test_returns_cached_engine(self):
        import modulo.core.cron_scheduler as mcs

        mock_engine = MagicMock()
        with (
            patch.object(mcs, "_ENGINE", None),
            patch.object(mcs, "create_async_engine", return_value=mock_engine) as mock_create,
            patch.object(mcs, "get_settings", return_value=_make_mock_settings()),
        ):
            e1 = _get_engine()
            e2 = _get_engine()
        assert e1 is e2 is mock_engine
        mock_create.assert_called_once()

    def test_engine_uses_postgres_connect_args(self):
        import modulo.core.cron_scheduler as mcs

        with (
            patch.object(mcs, "_ENGINE", None),
            patch.object(mcs, "create_async_engine") as mock_create,
            patch.object(mcs, "get_settings", return_value=_make_mock_settings()),
        ):
            _get_engine()
        assert mock_create.call_args.kwargs["connect_args"] == {"timeout": 10, "ssl": False}


class TestFireCronTrigger:
    async def test_skips_when_spend_limit_exceeded(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        trigger = _make_trigger(
            id=trigger_id, organisation_id=org_id, pipeline_id=pipeline_id, daily_spend_limit=Decimal("100.00")
        )

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = trigger

        cost_result = MagicMock()
        cost_result.scalar_one.return_value = Decimal("150.00")

        session = _MockSession(execute_side_effect=[lock_result, first_result, cost_result])
        result, mocks = await _run_fire(
            session, trigger_id=trigger_id, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=snapshot_id
        )

        assert result == {
            "status": "skipped",
            "reason": "spend_limit",
            "daily_spend_limit": "100.00",
            "today_cost": "150.00",
        }
        mocks["log_event"].assert_awaited_once()
        event_kwargs = mocks["log_event"].call_args.kwargs
        assert event_kwargs["result"] == "spend_limit_reached"
        assert event_kwargs["org_id"] == org_id
        assert event_kwargs["trigger"] is trigger
        assert "Daily spend limit" in event_kwargs["error_detail"]
        mocks["create_run"].assert_not_awaited()

    async def test_skips_when_trigger_busy(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = False

        session = _MockSession(execute_side_effect=[lock_result])
        result, mocks = await _run_fire(
            session, trigger_id=trigger_id, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=uuid.uuid4()
        )

        assert result == {"status": "skipped", "reason": "trigger_busy"}
        mocks["count"].assert_not_awaited()
        mocks["log_event"].assert_not_awaited()
        mocks["create_run"].assert_not_awaited()

    async def test_skips_when_trigger_missing(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = None

        session = _MockSession(execute_side_effect=[lock_result, first_result])
        result, mocks = await _run_fire(
            session, trigger_id=trigger_id, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=uuid.uuid4()
        )

        assert result == {"status": "skipped", "reason": "trigger_inactive_or_missing"}
        mocks["log_event"].assert_not_awaited()
        mocks["create_run"].assert_not_awaited()

    async def test_skips_when_trigger_inactive(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = _make_trigger(id=trigger_id, active=False)

        session = _MockSession(execute_side_effect=[lock_result, first_result])
        result, _ = await _run_fire(
            session, trigger_id=trigger_id, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=uuid.uuid4()
        )

        assert result == {"status": "skipped", "reason": "trigger_inactive_or_missing"}

    async def test_skips_when_concurrency_limit_reached(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        trigger = _make_trigger(id=trigger_id, max_concurrent_runs=5)

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = trigger

        session = _MockSession(execute_side_effect=[lock_result, first_result])
        result, mocks = await _run_fire(
            session,
            trigger_id=trigger_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=uuid.uuid4(),
            active_count=5,
        )

        assert result == {"status": "skipped", "reason": "concurrency_limit", "active_runs": 5}
        mocks["count"].assert_awaited_once()
        assert mocks["count"].await_args.args[0] is session
        assert mocks["count"].await_args.args[1] == trigger_id
        event_kwargs = mocks["log_event"].call_args.kwargs
        assert event_kwargs["result"] == "concurrency_limit_reached"
        assert "Active runs: 5, limit: 5" in event_kwargs["error_detail"]
        mocks["create_run"].assert_not_awaited()

    async def test_skips_when_pipeline_not_found(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        trigger = _make_trigger(id=trigger_id)

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = trigger

        session = _MockSession(execute_side_effect=[lock_result, first_result])
        with patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result, mocks = await _run_fire(
                session, trigger_id=trigger_id, org_id=org_id, pipeline_id=pipeline_id, snapshot_id=None
            )

        assert result == {"status": "skipped", "reason": "pipeline_not_found"}
        event_kwargs = mocks["log_event"].call_args.kwargs
        assert event_kwargs["result"] == "no_pipeline"
        mocks["create_run"].assert_not_awaited()

    async def test_fires_run_successfully(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        run_id = uuid.uuid4()
        event_id = uuid.uuid4()
        trigger = _make_trigger(id=trigger_id, organisation_id=org_id, pipeline_id=pipeline_id)

        lock_result = MagicMock()
        lock_result.scalar_one.return_value = True

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = trigger

        update_result = MagicMock()

        session = _MockSession(execute_side_effect=[lock_result, first_result, update_result])
        result, mocks = await _run_fire(
            session,
            trigger_id=trigger_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            create_run_return=MagicMock(id=run_id),
            log_event_return=MagicMock(id=event_id),
        )

        assert result["status"] == "fired"
        assert result["run_id"] == str(run_id)
        assert result["event_id"] == str(event_id)
        assert result["input_payload"] == {}
        next_fire_at = datetime.datetime.fromisoformat(result["next_fire_at"])
        assert next_fire_at.tzinfo is not None

        mocks["rls"].assert_awaited_once_with(session, org_id)
        mocks["count"].assert_awaited_once_with(session, trigger_id)

        create_kwargs = mocks["create_run"].call_args.kwargs
        assert mocks["create_run"].call_args.args[0] is session
        assert create_kwargs["org_id"] == org_id
        assert create_kwargs["pipeline_id"] == pipeline_id
        assert create_kwargs["snapshot_id"] == snapshot_id
        assert create_kwargs["trigger_type"] == "cron"
        assert create_kwargs["trigger_id"] == trigger_id
        assert create_kwargs["input_payload"] == {}

        event_kwargs = mocks["log_event"].call_args.kwargs
        assert event_kwargs["result"] == "accepted"
        assert event_kwargs["run_id"] == run_id
        assert event_kwargs["trigger"] is trigger
        assert event_kwargs["org_id"] == org_id

        # Implementation-coupled assertion: inspecting ``_values`` reaches into
        # SQLAlchemy's update internals (Column -> BindParameter). This is fine
        # for a unit test pinned to the lockfile, but will need revisiting if
        # the fire path stops using ``session.execute(update(...))``.
        update_stmt = session._execute_mock.await_args.args[0]
        assert isinstance(update_stmt, Update)
        bound = {col.name: val.value for col, val in update_stmt._values.items()}
        assert isinstance(bound["last_fired_at"], datetime.datetime)
        assert isinstance(bound["next_fire_at"], datetime.datetime)


class TestDatabaseCronSchedulerTick:
    def test_tick_syncs_then_defers_to_parent(self):
        scheduler = object.__new__(DatabaseCronScheduler)
        scheduler._schedule = {}
        scheduler.app = MagicMock()
        scheduler.data = {}

        order: list[str] = []

        def _parent_tick() -> float:
            order.append("parent")
            return 30.0

        with (
            patch.object(scheduler, "_sync_with_db", side_effect=lambda: order.append("sync")),
            patch.object(Scheduler, "tick", side_effect=_parent_tick),
        ):
            result = scheduler.tick()

        assert result == 30.0
        assert order == ["sync", "parent"]


class TestDatabaseCronSchedulerSync:
    def _make_row(self, *, trigger_id: uuid.UUID, next_fire_at: datetime.datetime) -> dict:
        return {
            "trigger_id": trigger_id,
            "org_id": uuid.uuid4(),
            "pipeline_id": uuid.uuid4(),
            "snapshot_id": uuid.uuid4(),
            "cron_expression": "*/5 * * * *",
            "next_fire_at": next_fire_at,
        }

    def _make_scheduler(self) -> DatabaseCronScheduler:
        scheduler = object.__new__(DatabaseCronScheduler)
        scheduler._schedule = {}
        return scheduler

    def test_adds_new_entries(self):
        scheduler = self._make_scheduler()
        trigger_id = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)
        rows = [self._make_row(trigger_id=trigger_id, next_fire_at=now)]

        with patch.object(scheduler, "_fetch_due_triggers", return_value=rows):
            scheduler._sync_with_db()

        assert len(scheduler._schedule) == 1
        entry = scheduler._schedule[f"cron-{trigger_id}"]
        assert entry._trigger_id == trigger_id
        assert entry._next_fire_at == now
        assert entry._cron_expression == "*/5 * * * *"

    def test_keeps_unchanged_entry(self):
        scheduler = self._make_scheduler()
        trigger_id = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)
        rows = [self._make_row(trigger_id=trigger_id, next_fire_at=now)]

        with patch.object(scheduler, "_fetch_due_triggers", return_value=rows):
            scheduler._sync_with_db()
        existing = scheduler._schedule[f"cron-{trigger_id}"]

        with patch.object(scheduler, "_fetch_due_triggers", return_value=rows):
            scheduler._sync_with_db()

        assert scheduler._schedule[f"cron-{trigger_id}"] is existing

    def test_updates_changed_next_fire_at(self):
        scheduler = self._make_scheduler()
        trigger_id = uuid.uuid4()
        first_fire = datetime.datetime.now(datetime.UTC)
        second_fire = first_fire + datetime.timedelta(minutes=30)

        with patch.object(
            scheduler,
            "_fetch_due_triggers",
            return_value=[self._make_row(trigger_id=trigger_id, next_fire_at=first_fire)],
        ):
            scheduler._sync_with_db()

        with patch.object(
            scheduler,
            "_fetch_due_triggers",
            return_value=[self._make_row(trigger_id=trigger_id, next_fire_at=second_fire)],
        ):
            scheduler._sync_with_db()

        assert scheduler._schedule[f"cron-{trigger_id}"]._next_fire_at == second_fire

    def test_removes_stale_entries(self):
        scheduler = self._make_scheduler()
        trigger_id = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)
        rows = [self._make_row(trigger_id=trigger_id, next_fire_at=now)]

        with patch.object(scheduler, "_fetch_due_triggers", return_value=rows):
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == 1

        with patch.object(scheduler, "_fetch_due_triggers", return_value=[]):
            scheduler._sync_with_db()

        assert scheduler._schedule == {}

    def test_survives_scheduler_db_error(self):
        scheduler = self._make_scheduler()
        trigger_id = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)
        rows = [self._make_row(trigger_id=trigger_id, next_fire_at=now)]

        with patch.object(scheduler, "_fetch_due_triggers", return_value=rows):
            scheduler._sync_with_db()

        with patch.object(scheduler, "_fetch_due_triggers", side_effect=SchedulerDBError("boom")):
            scheduler._sync_with_db()

        assert scheduler._schedule[f"cron-{trigger_id}"]._next_fire_at == now


class TestDatabaseCronEntry:
    def test_entry_properties(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=trigger_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            cron_expression="*/5 * * * *",
            next_fire_at=now,
        )
        assert entry.name == f"cron-{trigger_id}"
        assert entry.task == "modulo.cron.fire_trigger"
        assert entry.args == [str(trigger_id), str(org_id), str(pipeline_id), "*/5 * * * *", str(snapshot_id)]
        assert isinstance(entry.schedule, DatabaseCronEntry)
        assert entry.kwargs == {}

    def test_args_blank_snapshot_when_none(self):
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=None,
            cron_expression="* * * * *",
            next_fire_at=datetime.datetime.now(datetime.UTC),
        )
        assert entry.args[-1] == ""

    @pytest.mark.parametrize(
        "offset,expected_due",
        [
            (datetime.timedelta(seconds=-10), True),
            (datetime.timedelta(hours=1), False),
            (datetime.timedelta(seconds=0), True),
        ],
    )
    def test_is_due(self, offset, expected_due):
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=datetime.datetime.now(datetime.UTC) + offset,
        )
        due, delay = entry.is_due()
        assert due is expected_due
        if expected_due:
            assert delay == datetime.timedelta(0)
        else:
            assert datetime.timedelta(minutes=59) < delay <= datetime.timedelta(hours=1, minutes=1)

    def test_repr(self):
        trigger_id = uuid.uuid4()
        entry = DatabaseCronEntry(
            trigger_id=trigger_id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="0 9 * * *",
            next_fire_at=datetime.datetime.now(datetime.UTC),
        )
        r = repr(entry)
        assert "DatabaseCronEntry" in r
        assert str(trigger_id) in r
        assert "next=" in r

    def test_options_contains_unique_task_id(self):
        trigger_id = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=trigger_id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=now,
        )
        opts = entry.options
        assert opts["task_id"].startswith(f"cron-{trigger_id}-")
