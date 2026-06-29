"""Unit tests for DatabaseCronEntry — Celery beat entry lifecycle."""

import datetime
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from celery.beat import Scheduler

from modulo.core.cron_scheduler import (
    DatabaseCronEntry,
    DatabaseCronScheduler,
    _get_engine,
    fire_cron_trigger,
)
from modulo.db.models.trigger import Trigger
from modulo.settings import Settings


def _make_mock_settings() -> MagicMock:
    """Return a mock Settings instance with valid DB URL and secrets."""
    return MagicMock(
        spec=Settings,
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        redis_url="redis://localhost:6379/0",
        modulo_admin_password="test",
        openai_api_key=None,
        anthropic_api_key=None,
    )


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

    async def __aenter__(self) -> "_MockSession":
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


class TestGetEngine:
    def test_returns_cached_engine(self):
        import modulo.core.cron_scheduler as mcs

        saved = mcs._ENGINE
        try:
            mcs._ENGINE = None
            mock_engine = MagicMock()
            with (
                patch.object(mcs, "_ENGINE", None),
                patch.object(mcs, "create_async_engine", return_value=mock_engine) as mock_create,
                patch.object(mcs, "get_settings", return_value=_make_mock_settings()),
            ):
                e1 = _get_engine()
                e2 = _get_engine()
                assert e1 is e2
                mock_create.assert_called_once()
        finally:
            mcs._ENGINE = saved


class TestFireCronTrigger:
    async def test_skips_when_spend_limit_exceeded(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()

        trigger_mock = MagicMock(spec=Trigger)
        trigger_mock.id = trigger_id
        trigger_mock.organisation_id = org_id
        trigger_mock.pipeline_id = pipeline_id
        trigger_mock.active = True
        trigger_mock.max_concurrent_runs = 5
        trigger_mock.daily_spend_limit = Decimal("100.00")
        trigger_mock.config_json = {}

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = trigger_mock

        second_result = MagicMock()
        second_result.scalar_one.return_value = Decimal("150.00")

        session = _MockSession(execute_side_effect=[first_result, second_result])

        with (
            patch("modulo.core.cron_scheduler._get_engine"),
            patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=_MockSessionFactory(session)),
            patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.cron_scheduler._count_active_runs", new_callable=AsyncMock, return_value=0),
            patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock),
        ):
            result = await fire_cron_trigger(
                trigger_id=trigger_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                cron_expression="* * * * *",
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "spend_limit"
        assert result["daily_spend_limit"] == "100.00"
        assert result["today_cost"] == "150.00"

    async def test_logs_spend_limit_reached_event(self):
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()

        trigger_mock = MagicMock(spec=Trigger)
        trigger_mock.id = trigger_id
        trigger_mock.organisation_id = org_id
        trigger_mock.pipeline_id = pipeline_id
        trigger_mock.active = True
        trigger_mock.max_concurrent_runs = 5
        trigger_mock.daily_spend_limit = Decimal("100.00")
        trigger_mock.config_json = {}

        first_result = MagicMock()
        first_result.scalar_one_or_none.return_value = trigger_mock

        second_result = MagicMock()
        second_result.scalar_one.return_value = Decimal("150.00")

        session = _MockSession(execute_side_effect=[first_result, second_result])

        with (
            patch("modulo.core.cron_scheduler._get_engine"),
            patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=_MockSessionFactory(session)),
            patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.cron_scheduler._count_active_runs", new_callable=AsyncMock, return_value=0),
            patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock) as mock_log_event,
        ):
            mock_log_event.return_value = MagicMock(id=uuid.uuid4())
            await fire_cron_trigger(
                trigger_id=trigger_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                cron_expression="* * * * *",
            )

        mock_log_event.assert_awaited_once()
        call_kwargs = mock_log_event.call_args.kwargs
        assert call_kwargs["result"] == "spend_limit_reached"
        assert call_kwargs["org_id"] == org_id


class TestDatabaseCronSchedulerTick:
    def test_tick_calls_sync_with_db(self):
        scheduler = object.__new__(DatabaseCronScheduler)
        scheduler._schedule = {}
        scheduler.app = MagicMock()
        scheduler.data = {}

        with (
            patch.object(scheduler, "_sync_with_db") as mock_sync,
            patch.object(Scheduler, "tick", return_value=30.0),
        ):
            scheduler.tick()
        mock_sync.assert_called_once()


class TestDatabaseCronEntry:
    def test_entry_properties(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="*/5 * * * *",
            next_fire_at=now,
        )
        assert entry.name.startswith("cron-")
        assert entry.task == "modulo.cron.fire_trigger"
        assert len(entry.args) == 5
        assert entry.args[4] == "*/5 * * * *"
        assert isinstance(entry.schedule, DatabaseCronEntry)

    def test_is_due_when_past(self):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=past,
        )
        due, delay = entry.is_due()
        assert due is True
        assert delay.total_seconds() == 0

    def test_is_not_due_when_future(self):
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="0 * * * *",
            next_fire_at=future,
        )
        due, delay = entry.is_due()
        assert due is False
        assert delay.total_seconds() > 0

    def test_is_due_when_exactly_now(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=now,
        )
        due, _delay = entry.is_due()
        assert due is True

    def test_repr(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="0 9 * * *",
            next_fire_at=now,
        )
        r = repr(entry)
        assert "DatabaseCronEntry" in r
        assert "next=" in r

    def test_options_contains_unique_task_id(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=now,
        )
        opts = entry.options
        assert "task_id" in opts
        assert opts["task_id"].startswith("cron-")
