"""Unit tests for polling trigger — evaluate_condition, _fire_polling_trigger, scheduler."""

import datetime
import hashlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
    @pytest.mark.parametrize(
        "expr,records,expected",
        [
            (None, [{"id": 1}], True),
            (None, [], False),
            ("", [{"id": 1}], True),
            ("", [], False),
            ("[?status=='open']", [{"status": "open"}, {"status": "closed"}], True),
            ("[?status=='open']", [{"status": "closed"}], False),
            ("length(@) > `0`", [{"count": 5}], True),
            ("length([?count==`999`])", [{"count": 0}], False),
            ("missing_field", [{"id": 1}], False),
            ("[0].status", [{"status": "open"}], True),
            ("[0].status", [{"status": ""}], False),
            ("[0].nested", [{"nested": {"key": "val"}}], True),
            ("[0].nested", [{"nested": {}}], False),
            ("[0].flag == `true`", [{"flag": True}], True),
            ("[0].count > `0`", [{"count": 42}], True),
        ],
    )
    def test_evaluate_condition(self, expr: str | None, records: list[dict], expected: bool) -> None:
        result = ConnectorResult(records=records, total=len(records))
        assert evaluate_condition(result, expr) is expected

    def test_invalid_jmespath_expression(self) -> None:
        result = ConnectorResult(records=[{"id": 1}], total=1)
        with pytest.raises(ValueError, match="Invalid JMESPath expression"):
            evaluate_condition(result, "[invalid: syntax")


# ---------------------------------------------------------------------------
# _build_polling_connector tests
# ---------------------------------------------------------------------------


class TestBuildPollingConnector:
    @pytest.mark.parametrize(
        "connector_type,config,credentials,expected_type,raises_match",
        [
            ("filesystem", {"base_path": "/tmp"}, {}, "FilesystemConnector", None),
            ("github", {}, {"token": "ghp_xxx"}, "GitHubConnector", None),
            ("jira", {}, {"token": "x"}, None, "requires 'instance'"),
            ("filesystem", {}, {}, None, "requires 'base_path'"),
            ("unknown", {}, {}, None, "Unsupported connector type"),
        ],
    )
    def test_build_polling_connector(
        self,
        connector_type: str,
        config: dict,
        credentials: dict,
        expected_type: str | None,
        raises_match: str | None,
    ) -> None:
        if raises_match:
            with pytest.raises(ValueError, match=raises_match):
                _build_polling_connector(connector_type, config, credentials)
        else:
            connector = _build_polling_connector(connector_type, config, credentials)
            from modulo.connectors.filesystem import FilesystemConnector
            from modulo.connectors.github import GitHubConnector

            cls = FilesystemConnector if expected_type == "FilesystemConnector" else GitHubConnector
            assert isinstance(connector, cls)


# ---------------------------------------------------------------------------
# Helper: build a mocked async session with controlled query behaviour
# ---------------------------------------------------------------------------


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

    # Replace AsyncMock get_bind with sync MagicMock to avoid coroutine issues with Python 3.13+
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind_mock)

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


# (fire_trigger tests moved into TestPollingFireTask parametrize below)


# ---------------------------------------------------------------------------
# DatabasePollingEntry tests
# ---------------------------------------------------------------------------


class TestDatabasePollingEntry:
    @pytest.mark.parametrize(
        "offset_from_now,expected_due",
        [
            (datetime.timedelta(seconds=-10), True),
            (datetime.timedelta(hours=1), False),
        ],
    )
    def test_is_due(self, offset_from_now: datetime.timedelta, expected_due: bool) -> None:
        entry = DatabasePollingEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            connector_instance_id=uuid.uuid4(),
            poll_query="query",
            condition_expression=None,
            next_fire_at=datetime.datetime.now(datetime.UTC) + offset_from_now,
        )
        due, delay = entry.is_due()
        assert due is expected_due
        if expected_due:
            assert delay.total_seconds() == 0

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

    @pytest.mark.parametrize(
        "initial_rows,second_rows,expected_first_len,expected_second_len",
        [
            ([], None, 0, None),
            ([{"trigger_id": "dyn"}], None, 1, None),
            ([{"trigger_id": "dyn"}], [], 1, 0),
        ],
    )
    def test_sync_with_db(
        self,
        initial_rows: list,
        second_rows: list | None,
        expected_first_len: int,
        expected_second_len: int | None,
    ) -> None:
        app = MagicMock()
        now = datetime.datetime.now(datetime.UTC)

        def _row(r):
            tid = uuid.uuid4() if r.get("trigger_id") == "dyn" else r["trigger_id"]
            return {
                "trigger_id": tid,
                "org_id": uuid.uuid4(),
                "pipeline_id": uuid.uuid4(),
                "connector_instance_id": uuid.uuid4(),
                "poll_query": "select 1",
                "condition_expression": None,
                "next_fire_at": now,
            }

        rows = [_row(r) for r in initial_rows]
        with patch(
            "modulo.core.trigger_engine.polling.DatabasePollingScheduler._fetch_due_triggers",
            return_value=rows,
        ):
            scheduler = DatabasePollingScheduler(app)
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == expected_first_len

        if second_rows is not None:
            rows2 = [_row(r) for r in second_rows]
            with patch(
                "modulo.core.trigger_engine.polling.DatabasePollingScheduler._fetch_due_triggers",
                return_value=rows2,
            ):
                scheduler._sync_with_db()
            assert len(scheduler._schedule) == expected_second_len

    def test_tick_calls_sync_and_parent(self) -> None:
        """tick() should sync with DB and then call super().tick()."""
        app = MagicMock()
        scheduler = DatabasePollingScheduler(app)

        with (
            patch.object(scheduler, "_sync_with_db") as mock_sync,
            patch.object(scheduler, "_schedule", {}),
            patch.object(type(scheduler).__bases__[0], "tick", return_value=30.0),
        ):
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
        assert PollingFireTask.autoretry_for == (ConnectionError, TimeoutError, OSError)
        assert PollingFireTask.max_retries == 2
        assert PollingFireTask.default_retry_delay == 30

    @pytest.mark.parametrize(
        (
            "scenario",
            "status",
            "reason",
            "trigger_config",
            "trigger_active",
            "trigger_max_conc",
            "ci_present",
            "active_run_count",
            "condition_expr",
            "extra_patches",
            "extra_check",
        ),
        [
            pytest.param(
                "condition_met",
                "fired",
                None,
                {"snapshot_id": "uuid", "poll_interval_seconds": 60},
                True,
                5,
                True,
                0,
                "[?issue.number > `0`]",
                [],
                "condition_met",
            ),
            pytest.param(
                "no_match",
                "no_match",
                None,
                {"snapshot_id": "uuid", "poll_interval_seconds": 60},
                True,
                5,
                True,
                0,
                "[?issue.number > `999`]",
                ["connector_empty", "no_create_run"],
                "no_match",
            ),
            pytest.param(
                "concurrency_limit",
                "skipped",
                "concurrency_limit",
                {},
                True,
                2,
                False,
                3,
                None,
                ["no_create_run", "log_poll_event"],
                "concurrency",
            ),
            pytest.param(
                "inactive",
                "skipped",
                "trigger_inactive_or_missing",
                {},
                False,
                5,
                False,
                0,
                None,
                [],
                None,
            ),
            pytest.param(
                "connector_not_found",
                "error",
                "connector_not_found",
                {},
                True,
                5,
                None,
                0,
                None,
                ["log_poll_event"],
                None,
            ),
            pytest.param(
                "condition_eval_failure",
                "error",
                "condition_eval_failed",
                {"snapshot_id": "uuid", "poll_interval_seconds": 60},
                True,
                5,
                True,
                0,
                "[invalid syntax",
                ["evaluate_condition_error", "no_create_run"],
                None,
            ),
            pytest.param(
                "connector_init_failed",
                "error",
                "connector_init_failed",
                {"snapshot_id": "uuid", "poll_interval_seconds": 60},
                True,
                5,
                True,
                0,
                None,
                ["build_connector_error"],
                None,
            ),
            pytest.param(
                "query_failed",
                "error",
                "query_failed",
                {"snapshot_id": "uuid", "poll_interval_seconds": 60},
                True,
                5,
                True,
                0,
                None,
                ["connector_error"],
                None,
            ),
            pytest.param(
                "already_fired",
                "skipped",
                "already_fired_this_cycle",
                {},
                True,
                5,
                False,
                0,
                None,
                ["future_next_fire"],
                None,
            ),
        ],
    )
    async def test_fire_trigger(
        self,
        scenario,
        status,
        reason,
        trigger_config,
        trigger_active,
        trigger_max_conc,
        ci_present,
        active_run_count,
        condition_expr,
        extra_patches,
        extra_check,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        mock_create_run,
    ) -> None:
        session = mock_db_components
        _, connector = mock_connector

        if trigger_config.get("snapshot_id") == "uuid":
            trigger_config = dict(trigger_config)
            trigger_config["snapshot_id"] = str(uuid.uuid4())
        trigger = _make_trigger(
            active=trigger_active,
            max_concurrent_runs=trigger_max_conc,
            config=trigger_config or {},
        )

        if "future_next_fire" in extra_patches:
            trigger.next_fire_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)

        _setup_session_for_polling(
            session,
            trigger,
            connector_instance=MagicMock() if ci_present else None,
            active_run_count=active_run_count,
        )

        if "connector_empty" in extra_patches:
            connector.query.return_value = ConnectorResult(records=[], total=0)
        if "connector_error" in extra_patches:
            connector.query.side_effect = RuntimeError("API timeout")

        extra_mocks = {}
        if "no_create_run" in extra_patches:
            extra_mocks["create_run"] = patch("modulo.core.trigger_engine.polling.create_run")
        if "log_poll_event" in extra_patches:
            extra_mocks["log_poll_event"] = patch("modulo.core.trigger_engine.polling._log_poll_event")
        if "build_connector_error" in extra_patches:
            extra_mocks["build"] = patch("modulo.core.trigger_engine.polling._build_polling_connector")
        if "evaluate_condition_error" in extra_patches:
            extra_mocks["eval"] = patch(
                "modulo.core.trigger_engine.polling.evaluate_condition",
                side_effect=ValueError("bad JMESPath"),
            )

        started_patches = {}
        for k, v in extra_mocks.items():
            if hasattr(v, "start"):
                m = v.start()
                if k == "build":
                    m.side_effect = ValueError("missing creds")
                started_patches[k] = m

        poll_query = "select * from issues" if condition_expr else "query"

        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query=poll_query,
            condition_expression=condition_expr,
        )

        assert result["status"] == status
        if reason:
            assert result["reason"] == reason

        if extra_check == "condition_met":
            create_run_fn, _ = mock_create_run
            assert "run_id" in result
            create_run_fn.assert_awaited_once()
            connector.query.assert_awaited_once()
            assert any(
                getattr(c.args[0], "validation_result", None) == "condition_met" for c in session.add.call_args_list
            )
        elif extra_check == "no_match":
            if "no_create_run" in extra_patches:
                started_patches["create_run"].assert_not_called()
        elif extra_check == "concurrency":
            assert result.get("active_runs") == active_run_count
            if "no_create_run" in extra_patches:
                started_patches["create_run"].assert_not_called()
            if "log_poll_event" in extra_patches:
                started_patches["log_poll_event"].assert_called_once()

        for p in extra_mocks.values():
            p.stop()


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

        mock_warning.assert_any_call(
            "Polling trigger %s has no valid snapshot_id in config",
            _TRIGGER_ID,
            exc_info=True,
        )

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
