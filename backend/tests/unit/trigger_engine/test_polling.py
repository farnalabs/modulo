"""Unit tests for polling trigger — evaluate_condition, _fire_polling_trigger, scheduler."""

import datetime
import hashlib
import uuid
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from modulo.connectors.base import ConnectorResult
from modulo.core.trigger_engine.polling import (
    DatabasePollingEntry,
    DatabasePollingScheduler,
    PollingFireTask,
    _build_polling_connector,
    _fire_polling_trigger,
    _log_poll_event,
    evaluate_condition,
)
from modulo.db.models.trigger import Trigger

# ---------------------------------------------------------------------------
# evaluate_condition — pure function tests
# ---------------------------------------------------------------------------


class TestEvaluateCondition:
    def test_none_expression_with_records(self) -> None:
        result = ConnectorResult(records=[{"id": 1}], total=1)
        assert evaluate_condition(result, None) is True

    def test_none_expression_with_empty_records(self) -> None:
        result = ConnectorResult(records=[], total=0)
        assert evaluate_condition(result, None) is False

    def test_empty_expression_with_records(self) -> None:
        result = ConnectorResult(records=[{"id": 1}], total=1)
        assert evaluate_condition(result, "") is True

    def test_empty_expression_with_empty_records(self) -> None:
        result = ConnectorResult(records=[], total=0)
        assert evaluate_condition(result, "") is False

    def test_jmespath_returns_list(self) -> None:
        result = ConnectorResult(records=[{"status": "open"}, {"status": "closed"}], total=2)
        assert evaluate_condition(result, "[?status=='open']") is True

    def test_jmespath_returns_empty_list(self) -> None:
        result = ConnectorResult(records=[{"status": "closed"}], total=1)
        assert evaluate_condition(result, "[?status=='open']") is False

    def test_jmespath_returns_true(self) -> None:
        result = ConnectorResult(records=[{"count": 5}], total=1)
        assert evaluate_condition(result, "length(@) > `0`") is True

    def test_jmespath_returns_number_zero(self) -> None:
        result = ConnectorResult(records=[{"count": 0}], total=1)
        assert evaluate_condition(result, "length([?count==`999`])") is False

    def test_jmespath_returns_none(self) -> None:
        result = ConnectorResult(records=[{"id": 1}], total=1)
        assert evaluate_condition(result, "missing_field") is False

    def test_jmespath_returns_string(self) -> None:
        result = ConnectorResult(records=[{"status": "open"}], total=1)
        assert evaluate_condition(result, "[0].status") is True

    def test_jmespath_returns_empty_string(self) -> None:
        result = ConnectorResult(records=[{"status": ""}], total=1)
        assert evaluate_condition(result, "[0].status") is False

    def test_jmespath_returns_dict(self) -> None:
        result = ConnectorResult(records=[{"nested": {"key": "val"}}], total=1)
        assert evaluate_condition(result, "[0].nested") is True

    def test_jmespath_returns_empty_dict(self) -> None:
        result = ConnectorResult(records=[{"nested": {}}], total=1)
        assert evaluate_condition(result, "[0].nested") is False

    def test_jmespath_returns_bool_true(self) -> None:
        result = ConnectorResult(records=[{"flag": True}], total=1)
        assert evaluate_condition(result, "[0].flag == `true`") is True

    def test_invalid_jmespath_expression(self) -> None:
        result = ConnectorResult(records=[{"id": 1}], total=1)
        with pytest.raises(ValueError, match="Invalid JMESPath expression"):
            evaluate_condition(result, "[invalid: syntax")

    def test_jmespath_returns_nonzero_number(self) -> None:
        result = ConnectorResult(records=[{"count": 42}], total=1)
        assert evaluate_condition(result, "[0].count > `0`") is True


# ---------------------------------------------------------------------------
# _build_polling_connector tests
# ---------------------------------------------------------------------------


class TestBuildPollingConnector:
    def test_filesystem_missing_base_path(self) -> None:
        with pytest.raises(ValueError, match="requires 'base_path'"):
            _build_polling_connector("filesystem", {}, {})

    def test_filesystem_with_base_path(self) -> None:
        connector = _build_polling_connector("filesystem", {"base_path": "/tmp"}, {})
        from modulo.connectors.filesystem import FilesystemConnector

        assert isinstance(connector, FilesystemConnector)

    def test_github(self) -> None:
        connector = _build_polling_connector("github", {}, {"token": "ghp_xxx"})
        from modulo.connectors.github import GitHubConnector

        assert isinstance(connector, GitHubConnector)

    def test_unsupported_type(self) -> None:
        with pytest.raises(ValueError, match="Unsupported connector type"):
            _build_polling_connector("unknown", {}, {})


# ---------------------------------------------------------------------------
# Helper: build a mocked async session with controlled query behaviour
# ---------------------------------------------------------------------------


def _make_mock_session(
    trigger: MagicMock | None = None,
    active_run_count: int = 0,
    connector_instance: MagicMock | None = None,
) -> AsyncMock:
    """Build a mocked session that returns controlled values on execute()."""
    session = AsyncMock()

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger
    trigger_result.scalar_one.return_value = active_run_count

    ci_result = MagicMock()
    ci_result.scalar_one_or_none.return_value = connector_instance

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    call_index: int = 0

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_index
        call_index += 1
        stmt_str = str(stmt)
        # Determine response based on query type (order may vary)
        if "SELECT count" in stmt_str:
            return count_result
        return trigger_result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()

    # Support async session.begin() context manager
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)

    # Support begin_nested for savepoints
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)

    return session


def _make_trigger(
    active: bool = True,
    max_concurrent_runs: int = 5,
    config: dict[str, Any] | None = None,
) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.organisation_id = uuid.uuid4()
    t.active = active
    t.max_concurrent_runs = max_concurrent_runs
    t.config_json = config or {}
    t.next_fire_at = datetime.datetime.now(datetime.UTC)
    return t


# ---------------------------------------------------------------------------
# _fire_polling_trigger tests
# ---------------------------------------------------------------------------


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_CI_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_VALID_32 = "a" * 32


@pytest.fixture
def mock_settings():
    with patch("modulo.core.trigger_engine.polling.get_settings") as mock:
        settings = MagicMock()
        settings.database_url = "postgresql+asyncpg://localhost/test"
        settings.fernet_key = _VALID_32
        settings.modulo_secrets_backend = "fernet"
        mock.return_value = settings
        yield mock


@pytest.fixture
def mock_db_components(mock_settings):
    """Mock create_async_engine and async_sessionmaker so _fire_polling_trigger
    uses a controlled session instead of a real DB."""
    session = AsyncMock()

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session.execute = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()

    with (
        patch("modulo.core.trigger_engine.polling.create_async_engine", return_value=engine),
        patch("modulo.core.trigger_engine.polling.async_sessionmaker", return_value=factory),
    ):
        yield session


@pytest.fixture
def mock_secrets_backend():
    with patch("modulo.core.trigger_engine.polling.create_secrets_backend") as mock:
        backend = AsyncMock()
        backend.get_secret.return_value = '{"token": "test-token"}'
        mock.return_value = backend
        yield mock


@pytest.fixture
def mock_connector():
    with patch("modulo.core.trigger_engine.polling._build_polling_connector") as mock:
        connector = AsyncMock()
        connector.query.return_value = ConnectorResult(
            records=[{"issue": {"number": 1, "title": "Bug"}}],
            total=1,
        )
        mock.return_value = connector
        yield mock, connector


@pytest.fixture
def mock_create_run():
    with patch("modulo.core.trigger_engine.polling.create_run") as mock:
        run_mock = MagicMock()
        run_mock.id = uuid.uuid4()
        mock.return_value = run_mock
        yield mock, run_mock


def _setup_session_for_polling(
    session: AsyncMock,
    trigger: MagicMock,
    connector_instance: MagicMock | None = None,
    active_run_count: int = 0,
) -> None:
    """Configure session.execute to handle all DB queries from _fire_polling_trigger.

    The function makes calls in this order:
      1. _set_rls_org → text(...)
      2. select(Trigger).with_for_update()
      3. _count_active_runs → select(func.count())
      4. select(ConnectorInstance)
      5. update(Trigger)  (in _update_next_fire)
    """
    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    ci_result = MagicMock()
    ci_result.scalar_one_or_none.return_value = connector_instance

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    rls_result = MagicMock()

    # Route to the right result based on query type
    async def _execute(stmt, *args, **kwargs):
        stmt_str = str(stmt).lower()
        if "set_config" in stmt_str:
            return rls_result
        if "for update" in stmt_str or "from triggers" in stmt_str:
            return trigger_result
        if "connector_instance" in stmt_str:
            return ci_result
        if "count(*)" in stmt_str:
            return count_result
        if "update" in stmt_str:
            return count_result
        return rls_result

    session.execute = _execute


async def test_fire_trigger_condition_met(
    mock_db_components,
    mock_secrets_backend,
    mock_connector,
    mock_create_run,
) -> None:
    """Happy path: condition matches → run created, event logged."""
    session = mock_db_components
    _, connector = mock_connector
    mock_create_run_fn, _run_mock = mock_create_run

    trigger = _make_trigger(config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60})
    _setup_session_for_polling(session, trigger, connector_instance=MagicMock(), active_run_count=0)

    result = await _fire_polling_trigger(
        trigger_id=_TRIGGER_ID,
        org_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        connector_instance_id=_CI_ID,
        poll_query="select * from issues",
        condition_expression="[?issue.number > `0`]",
    )

    assert result["status"] == "fired"
    assert "run_id" in result
    mock_create_run_fn.assert_awaited_once()
    connector.query.assert_awaited_once_with(ANY)

    session.add.assert_called()
    assert any(getattr(c.args[0], "validation_result", None) == "condition_met" for c in session.add.call_args_list)


async def test_fire_trigger_no_match(
    mock_db_components,
    mock_secrets_backend,
    mock_connector,
) -> None:
    """Condition not met → no run created, no_match event logged."""
    session = mock_db_components
    _, connector = mock_connector
    connector.query.return_value = ConnectorResult(records=[], total=0)

    trigger = _make_trigger(config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60})
    _setup_session_for_polling(session, trigger, connector_instance=MagicMock(), active_run_count=0)

    with patch("modulo.core.trigger_engine.polling.create_run") as mock_cr:
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression="[?issue.number > `999`]",
        )

    assert result["status"] == "no_match"
    mock_cr.assert_not_called()


async def test_fire_trigger_concurrency_limit(
    mock_db_components,
) -> None:
    """Active runs >= max_concurrent_runs → skipped, concurrency event logged."""
    session = mock_db_components
    trigger = _make_trigger(max_concurrent_runs=2)
    _setup_session_for_polling(session, trigger, active_run_count=3)

    with (
        patch("modulo.core.trigger_engine.polling.create_run") as mock_cr,
        patch("modulo.core.trigger_engine.polling._log_poll_event") as mock_log,
    ):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="query",
            condition_expression=None,
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "concurrency_limit"
    assert result["active_runs"] == 3
    mock_cr.assert_not_called()
    mock_log.assert_called_once()


async def test_fire_trigger_inactive(mock_db_components) -> None:
    """Inactive trigger → skipped."""
    session = mock_db_components
    trigger = _make_trigger(active=False)
    _setup_session_for_polling(session, trigger)

    result = await _fire_polling_trigger(
        trigger_id=_TRIGGER_ID,
        org_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        connector_instance_id=_CI_ID,
        poll_query="query",
        condition_expression=None,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "trigger_inactive_or_missing"


async def test_fire_trigger_connector_not_found(
    mock_db_components,
    mock_secrets_backend,
) -> None:
    """Connector instance missing → poll_error event."""
    session = mock_db_components
    trigger = _make_trigger()
    # Passing connector_instance=None so the CI query returns None
    _setup_session_for_polling(session, trigger, connector_instance=None, active_run_count=0)

    with patch("modulo.core.trigger_engine.polling._log_poll_event") as mock_log:
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="query",
            condition_expression=None,
        )

    assert result["status"] == "error"
    assert result["reason"] == "connector_not_found"
    mock_log.assert_called_once()


async def test_fire_trigger_condition_eval_failure(
    mock_db_components,
    mock_secrets_backend,
    mock_connector,
) -> None:
    """Invalid JMESPath in condition → poll_error logged."""
    session = mock_db_components

    trigger = _make_trigger(config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60})
    _setup_session_for_polling(session, trigger, connector_instance=MagicMock(), active_run_count=0)

    with (
        patch("modulo.core.trigger_engine.polling.create_run") as mock_cr,
        patch(
            "modulo.core.trigger_engine.polling.evaluate_condition",
            side_effect=ValueError("bad JMESPath"),
        ),
    ):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="query",
            condition_expression="[invalid syntax",
        )

    assert result["status"] == "error"
    assert result["reason"] == "condition_eval_failed"
    mock_cr.assert_not_called()


# ---------------------------------------------------------------------------
# DatabasePollingEntry tests
# ---------------------------------------------------------------------------


class TestDatabasePollingEntry:
    def test_is_due_true(self) -> None:
        entry = DatabasePollingEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            connector_instance_id=uuid.uuid4(),
            poll_query="query",
            condition_expression=None,
            next_fire_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=10),
        )
        due, delay = entry.is_due()
        assert due is True
        assert delay.total_seconds() == 0

    def test_is_due_false(self) -> None:
        entry = DatabasePollingEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            connector_instance_id=uuid.uuid4(),
            poll_query="query",
            condition_expression=None,
            next_fire_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )
        due, _ = entry.is_due()
        assert due is False

    def test_task_name(self) -> None:
        entry = DatabasePollingEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            connector_instance_id=uuid.uuid4(),
            poll_query="query",
            condition_expression=None,
            next_fire_at=datetime.datetime.now(datetime.UTC),
        )
        assert entry.task == "modulo.polling.fire_trigger"

    def test_args(self) -> None:
        tid = uuid.uuid4()
        oid = uuid.uuid4()
        pid = uuid.uuid4()
        ci = uuid.uuid4()
        entry = DatabasePollingEntry(
            trigger_id=tid,
            org_id=oid,
            pipeline_id=pid,
            connector_instance_id=ci,
            poll_query="select *",
            condition_expression="[?id]",
            next_fire_at=datetime.datetime.now(datetime.UTC),
        )
        assert entry.args == [str(tid), str(oid), str(pid), str(ci), "select *", "[?id]"]

    def test_args_condition_none(self) -> None:
        entry = DatabasePollingEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            connector_instance_id=uuid.uuid4(),
            poll_query="q",
            condition_expression=None,
            next_fire_at=datetime.datetime.now(datetime.UTC),
        )
        assert entry.args[-1] == ""


# ---------------------------------------------------------------------------
# DatabasePollingScheduler tests
# ---------------------------------------------------------------------------


class TestDatabasePollingScheduler:
    def test_max_interval(self) -> None:
        app = MagicMock()
        scheduler = DatabasePollingScheduler(app)
        assert scheduler.max_interval == 30

    def test_sync_with_db_empty(self) -> None:
        """When DB returns no triggers, schedule should be empty."""
        app = MagicMock()

        with patch(
            "modulo.core.trigger_engine.polling.DatabasePollingScheduler._fetch_due_triggers",
            return_value=[],
        ):
            scheduler = DatabasePollingScheduler(app)
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == 0

    def test_sync_with_db_populates(self) -> None:
        """When DB returns a trigger row, schedule should have one entry."""
        app = MagicMock()
        tid = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)

        with patch(
            "modulo.core.trigger_engine.polling.DatabasePollingScheduler._fetch_due_triggers",
            return_value=[
                {
                    "trigger_id": tid,
                    "org_id": uuid.uuid4(),
                    "pipeline_id": uuid.uuid4(),
                    "connector_instance_id": uuid.uuid4(),
                    "poll_query": "select 1",
                    "condition_expression": None,
                    "next_fire_at": now,
                }
            ],
        ):
            scheduler = DatabasePollingScheduler(app)
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == 1
        key = f"polling-{tid}"
        assert key in scheduler._schedule

    def test_sync_with_db_removes_stale(self) -> None:
        """When a trigger is removed from DB, its entry should be removed."""
        app = MagicMock()
        tid = uuid.uuid4()
        now = datetime.datetime.now(datetime.UTC)

        with patch(
            "modulo.core.trigger_engine.polling.DatabasePollingScheduler._fetch_due_triggers",
            return_value=[
                {
                    "trigger_id": tid,
                    "org_id": uuid.uuid4(),
                    "pipeline_id": uuid.uuid4(),
                    "connector_instance_id": uuid.uuid4(),
                    "poll_query": "select 1",
                    "condition_expression": None,
                    "next_fire_at": now,
                }
            ],
        ):
            scheduler = DatabasePollingScheduler(app)
            scheduler._sync_with_db()

        assert len(scheduler._schedule) == 1

        # Second sync with empty list removes it
        with patch(
            "modulo.core.trigger_engine.polling.DatabasePollingScheduler._fetch_due_triggers",
            return_value=[],
        ):
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == 0

    def test_tick_calls_sync_and_parent(self) -> None:
        """tick() should sync with DB and then call super().tick()."""
        app = MagicMock()
        scheduler = DatabasePollingScheduler(app)

        with (
            patch.object(scheduler, "_sync_with_db") as mock_sync,
            patch.object(scheduler, "_schedule", {}),
        ):
            with patch.object(type(scheduler).__bases__[0], "tick", return_value=30.0):
                result = scheduler.tick()
                mock_sync.assert_called_once()
                assert result == 30.0


# ---------------------------------------------------------------------------
# PollingFireTask tests
# ---------------------------------------------------------------------------


class TestPollingFireTask:
    def test_task_name(self) -> None:
        assert PollingFireTask.name == "modulo.polling.fire_trigger"

    def test_task_attributes(self) -> None:
        assert PollingFireTask.autoretry_for == (Exception,)
        assert PollingFireTask.max_retries == 2
        assert PollingFireTask.default_retry_delay == 30


    async def test_fire_trigger_connector_init_failed(
        self,
        mock_db_components,
        mock_secrets_backend,
    ) -> None:
        """Connector init failure → status=error reason=connector_init_failed."""
        session = mock_db_components
        trigger = _make_trigger(config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock(), active_run_count=0)

        with patch("modulo.core.trigger_engine.polling._build_polling_connector") as mock_build:
            mock_build.side_effect = ValueError("missing creds")
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="select * from issues",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "connector_init_failed"

    async def test_fire_trigger_query_failed(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """Poll query failure → status=error reason=query_failed."""
        session = mock_db_components
        _, connector = mock_connector
        connector.query.side_effect = RuntimeError("API timeout")

        trigger = _make_trigger(config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock(), active_run_count=0)

        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression=None,
        )

        assert result["status"] == "error"
        assert result["reason"] == "query_failed"

    async def test_fire_trigger_already_fired(
        self,
        mock_db_components,
    ) -> None:
        """next_fire_at in future → skipped, already_fired_this_cycle."""
        session = mock_db_components
        trigger = _make_trigger()
        trigger.next_fire_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        _setup_session_for_polling(session, trigger)

        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="query",
            condition_expression=None,
        )

        assert result["status"] == "skipped"
        assert result["reason"] == "already_fired_this_cycle"


class TestBuildPollingConnectorExtended:
    """Additional _build_polling_connector edge cases."""

    def test_jira_missing_instance(self) -> None:
        with pytest.raises(ValueError, match="requires 'instance'"):
            _build_polling_connector("jira", {}, {"token": "x"})


# ---------------------------------------------------------------------------
# Logging behaviour tests
# ---------------------------------------------------------------------------


class TestPollingLogging:
    """Tests for _log.warning() calls in polling trigger error paths."""

    async def test_connector_not_found_logs_warning(
        self,
        mock_db_components,
    ) -> None:
        """Connector instance missing should log a warning."""
        session = mock_db_components
        trigger = _make_trigger()
        _setup_session_for_polling(session, trigger, connector_instance=None, active_run_count=0)

        with patch("modulo.core.trigger_engine.polling._log.warning") as mock_warning:
            await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )

        mock_warning.assert_called_once()
        args, _ = mock_warning.call_args
        assert "Connector instance" in args[0]

    async def test_invalid_snapshot_id_fallback_logs_warning(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """Invalid snapshot_id in config should log a warning."""
        session = mock_db_components
        _, connector = mock_connector
        connector.query.return_value = ConnectorResult(
            records=[{"issue": {"number": 1, "title": "Bug"}}],
            total=1,
        )

        trigger = _make_trigger(config={"snapshot_id": "not-a-uuid", "poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock(), active_run_count=0)

        with (
            patch("modulo.core.trigger_engine.polling.create_run") as mock_cr,
            patch("modulo.core.trigger_engine.polling._log.warning") as mock_warning,
        ):
            mock_run = MagicMock()
            mock_run.id = uuid.uuid4()
            mock_cr.return_value = mock_run

            await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="select * from issues",
                condition_expression="[?issue.number > `0`]",
            )

        mock_warning.assert_any_call("Polling trigger %s has no valid snapshot_id in config", _TRIGGER_ID)

    async def test_poll_event_has_meaningful_hash(self) -> None:
        """_log_poll_event should compute a hash based on trigger id + result."""
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()

        trigger = MagicMock()
        trigger.id = uuid.uuid4()
        org_id = uuid.uuid4()

        event = await _log_poll_event(
            session,
            trigger=trigger,
            org_id=org_id,
            result="condition_met",
        )

        expected_hash = hashlib.sha256(f"polling:{trigger.id}:condition_met".encode()).hexdigest()
        assert event.raw_payload_hash == expected_hash
        assert event.raw_payload_hash != hashlib.sha256(b"polling").hexdigest()
