"""Unit tests for polling trigger — evaluate_condition, _fire_polling_trigger, scheduler."""

import asyncio
import datetime
import hashlib
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.connectors.base import ConnectorResult
from modulo.core.trigger_engine.polling import (
    _build_polling_connector,
    _count_active_runs,
    _daily_spend_limit_reached,
    _fire_polling_trigger,
    _log_poll_event,
    _set_rls_org,
    _update_next_fire,
    _update_next_fire_no_last,
    evaluate_condition,
)
from modulo.db.models.trigger import Trigger

# ---------------------------------------------------------------------------
# evaluate_condition — pure function tests
# ---------------------------------------------------------------------------


class TestEvaluateCondition:
    @pytest.mark.parametrize(
        ("expr", "records", "expected"),
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
            ("[0].x", [{"x": Decimal("1.5")}], True),
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
        ("connector_type", "config", "credentials", "expected_type", "raises_match"),
        [
            ("filesystem", {"base_path": "/tmp"}, {}, "FilesystemConnector", None),
            ("github", {}, {"token": "ghp_xxx"}, "GitHubConnector", None),
            ("gitlab", {}, {"token": "glpat_xxx"}, "GitLabConnector", None),
            (
                "gitlab",
                {"base_url": "https://gitlab.example.com/api/v4"},
                {"token": "glpat_xxx"},
                "GitLabConnector",
                None,
            ),
            ("slack", {}, {"bot_token": "xoxb-xxx"}, "SlackConnector", None),
            ("jira", {"instance": "https://acme.atlassian.net"}, {"token": "x"}, "JiraConnector", None),
            ("jira", {}, {"token": "x"}, None, "requires 'instance' or 'base_url'"),
            (
                "jira",
                {"base_url": "https://jira.example.com/rest/api/2", "api_version": 2},
                {"token": "x"},
                "JiraConnector",
                None,
            ),
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
            from modulo.connectors.gitlab import GitLabConnector
            from modulo.connectors.jira import JiraConnector
            from modulo.connectors.slack import SlackConnector

            cls = {
                "FilesystemConnector": FilesystemConnector,
                "GitHubConnector": GitHubConnector,
                "GitLabConnector": GitLabConnector,
                "SlackConnector": SlackConnector,
                "JiraConnector": JiraConnector,
            }[expected_type]
            assert isinstance(connector, cls)


# ---------------------------------------------------------------------------
# Helper: build a mocked async session with controlled query behaviour
# ---------------------------------------------------------------------------


def _make_trigger(
    active: bool = True,
    max_concurrent_runs: int = 5,
    config: dict[str, Any] | None = None,
    daily_spend_limit: Any = None,
) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = uuid.uuid4()
    t.pipeline_id = uuid.uuid4()
    t.organisation_id = uuid.uuid4()
    t.active = active
    t.max_concurrent_runs = max_concurrent_runs
    t.daily_spend_limit = daily_spend_limit
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
    with patch("modulo.core.cron_helpers.get_settings") as mock:
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
    session.begin_nested = MagicMock(return_value=begin_cm)

    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    engine = MagicMock()

    with (
        patch("modulo.db.session.get_shared_engine", return_value=engine),
        patch("modulo.core.cron_helpers.async_sessionmaker", return_value=factory),
    ):
        yield session


@pytest.fixture
def mock_secrets_backend():
    with patch("modulo.core.secrets_backend.create_secrets_backend") as mock:
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
    with patch("modulo.db.crud.run.create_run") as mock:
        run_mock = MagicMock()
        run_mock.id = uuid.uuid4()
        mock.return_value = run_mock
        yield mock, run_mock


def _setup_session_for_polling(
    session: AsyncMock,
    trigger: MagicMock,
    connector_instance: MagicMock | None = None,
    active_run_count: int = 0,
    today_cost: Any = 0,
    *,
    lock_acquired: bool = True,
    trigger_missing: bool = False,
) -> None:
    """Configure session.execute to handle all DB queries from _fire_polling_trigger.

    The function makes calls in this order:
      1. _set_rls_org → text(...)
      2. pg_try_advisory_xact_lock (skip gate)
      3. select(Trigger).with_for_update()
      4. _count_active_runs → select(func.count())
      5. _daily_spend_limit_reached → select(coalesce(sum(Run.total_cost_usd), 0))
      6. select(ConnectorInstance)
      7. update(Trigger)  (in _update_next_fire)
    """
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = lock_acquired

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = None if trigger_missing else trigger

    ci_result = MagicMock()
    ci_result.scalar_one_or_none.return_value = connector_instance

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    cost_result = MagicMock()
    cost_result.scalar_one.return_value = today_cost

    org_result = MagicMock()
    org_result.one_or_none.return_value = (False, "active")

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
        if "pg_try_advisory_xact_lock" in stmt_str:
            return lock_result
        if "for update" in stmt_str or "from triggers" in stmt_str:
            return trigger_result
        if "organisations" in stmt_str:
            return org_result
        if "connector_instance" in stmt_str:
            return ci_result
        if "count(*)" in stmt_str:
            return count_result
        if "total_cost_usd" in stmt_str:
            return cost_result
        if "update" in stmt_str:
            return count_result
        return rls_result

    session.execute = _execute


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _update_stmt_sql(session: MagicMock) -> str:
    args, _kwargs = session.execute.call_args
    return str(args[0])


class TestUpdateNextFire:
    async def test_update_next_fire_sets_last_and_next(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        trigger = _make_trigger(config={"poll_interval_seconds": 120})

        await _update_next_fire(session, trigger)

        sql = _update_stmt_sql(session)
        assert "last_fired_at" in sql
        assert "next_fire_at" in sql

    async def test_update_next_fire_default_interval(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        trigger = _make_trigger(config={})

        await _update_next_fire(session, trigger)

        assert "next_fire_at" in _update_stmt_sql(session)

    async def test_update_next_fire_no_last_omits_last_fired_at(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        trigger = _make_trigger(config={"poll_interval_seconds": 60})

        await _update_next_fire_no_last(session, trigger)

        sql = _update_stmt_sql(session)
        assert "next_fire_at" in sql
        assert "last_fired_at" not in sql


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestDailySpendLimit:
    """Daily spend limit (trigger.daily_spend_limit) must prevent run creation."""

    async def test_spend_limit_reached_skips_with_event(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        session = mock_db_components
        trigger = _make_trigger(
            daily_spend_limit=Decimal("50.00"),
            config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60},
        )
        _setup_session_for_polling(
            session,
            trigger,
            connector_instance=MagicMock(),
            active_run_count=0,
            today_cost=Decimal("55.00"),
        )

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.cron_helpers._log_poll_event") as mock_event,
        ):
            mock_event.return_value = MagicMock(id=uuid.uuid4())
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="select * from issues",
                condition_expression=None,
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "spend_limit"
        assert result["daily_spend_limit"] == "50.00"
        assert result["today_cost"] == "55.00"
        mock_cr.assert_not_called()
        mock_event.assert_called_once()
        assert mock_event.call_args.kwargs["result"] == "spend_limit_reached"
        assert mock_event.call_args.kwargs["error_detail"] == ("Daily spend limit 50.00 reached (today: 55.00)")

    async def test_spend_limit_equal_skips(self, mock_db_components) -> None:
        """today_cost == limit is still over budget (>= comparison)."""
        session = mock_db_components
        trigger = _make_trigger(
            daily_spend_limit=Decimal("50.00"),
            config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60},
        )
        _setup_session_for_polling(
            session,
            trigger,
            connector_instance=MagicMock(),
            active_run_count=0,
            today_cost=Decimal("50.00"),
        )

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.trigger_engine.polling._log_poll_event", new_callable=AsyncMock),
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
        assert result["reason"] == "spend_limit"
        mock_cr.assert_not_called()

    async def test_spend_limit_not_reached_fires(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        mock_create_run,
    ) -> None:
        """today_cost below the limit must not block run creation."""
        session = mock_db_components
        trigger = _make_trigger(
            daily_spend_limit=Decimal("100.00"),
            config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60},
        )
        _setup_session_for_polling(
            session,
            trigger,
            connector_instance=MagicMock(),
            active_run_count=0,
            today_cost=Decimal("55.00"),
        )

        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression=None,
        )

        assert result["status"] == "fired"
        create_run_fn, _ = mock_create_run
        create_run_fn.assert_awaited_once()

    async def test_no_limit_configured_fires(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        mock_create_run,
    ) -> None:
        """A trigger with no daily_spend_limit is never blocked."""
        session = mock_db_components
        trigger = _make_trigger(config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60})
        _setup_session_for_polling(
            session,
            trigger,
            connector_instance=MagicMock(),
            active_run_count=0,
        )

        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression=None,
        )

        assert result["status"] == "fired"
        create_run_fn, _ = mock_create_run
        create_run_fn.assert_awaited_once()

    async def test_spend_limit_query_scoped_to_trigger_and_org(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        mock_create_run,
    ) -> None:
        """The spend query must filter by trigger_id and organisation_id and today's runs."""
        session = mock_db_components
        captured: list[str] = []

        trigger = _make_trigger(
            daily_spend_limit=Decimal("100.00"),
            config={"snapshot_id": str(uuid.uuid4()), "poll_interval_seconds": 60},
        )
        _setup_session_for_polling(
            session,
            trigger,
            connector_instance=MagicMock(),
            active_run_count=0,
            today_cost=Decimal("10.00"),
        )

        orig_execute = session.execute

        async def _capture_execute(stmt, *args, **kwargs):
            captured.append(str(stmt).lower())
            return await orig_execute(stmt, *args, **kwargs)

        session.execute = _capture_execute

        await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="query",
            condition_expression=None,
        )

        spend_sql = next(s for s in captured if "total_cost_usd" in s)
        assert "runs.trigger_id" in spend_sql
        assert "runs.organisation_id" in spend_sql
        assert "runs.created_at" in spend_sql


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

        with patch("modulo.core.cron_helpers._log.warning") as mock_warning:
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

    async def test_invalid_snapshot_id_falls_back_to_zero_uuid(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        mock_create_run,
    ) -> None:
        """An invalid snapshot_id in config falls back to the zero UUID."""
        session = mock_db_components
        _, connector = mock_connector
        connector.query.return_value = ConnectorResult(
            records=[{"issue": {"number": 1, "title": "Bug"}}],
            total=1,
        )

        trigger = _make_trigger(config={"snapshot_id": "not-a-uuid", "poll_interval_seconds": 60})
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
        create_run_fn, _ = mock_create_run
        create_run_fn.assert_awaited_once()
        assert create_run_fn.call_args.kwargs["snapshot_id"] == uuid.UUID(int=0)

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


# ---------------------------------------------------------------------------
# _fire_polling_trigger — skip paths
# ---------------------------------------------------------------------------


class TestFirePollingTriggerSkips:
    async def test_lock_not_acquired_returns_trigger_busy(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """When the advisory xact lock is not acquired, skip with trigger_busy."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, lock_acquired=False)

        with patch("modulo.db.crud.run.create_run") as mock_cr:
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="select * from issues",
                condition_expression=None,
            )

        assert result == {"status": "skipped", "reason": "trigger_busy"}
        mock_cr.assert_not_called()

    async def test_trigger_missing_returns_trigger_inactive_or_missing(
        self,
        mock_db_components,
    ) -> None:
        """A missing trigger row must be skipped, not crash."""
        session = mock_db_components
        _setup_session_for_polling(
            session,
            MagicMock(),
            trigger_missing=True,
        )

        with patch("modulo.db.crud.run.create_run") as mock_cr:
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )

        assert result == {"status": "skipped", "reason": "trigger_inactive_or_missing"}
        mock_cr.assert_not_called()

    async def test_inactive_trigger_returns_trigger_inactive_or_missing(
        self,
        mock_db_components,
    ) -> None:
        """An inactive trigger must be skipped before running any query."""
        session = mock_db_components
        trigger = _make_trigger(active=False, config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger)

        with patch("modulo.db.crud.run.create_run") as mock_cr:
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )

        assert result == {"status": "skipped", "reason": "trigger_inactive_or_missing"}
        mock_cr.assert_not_called()

    async def test_future_next_fire_still_fires(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        mock_create_run,
    ) -> None:
        """The per-item fire job does NOT re-check next_fire_at — the atomic
        advance happens at enqueue time in fire_due_triggers, so a future
        next_fire_at still fires when this job runs."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        trigger.next_fire_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="query",
            condition_expression=None,
        )

        assert result["status"] == "fired"
        create_run_fn, _ = mock_create_run
        create_run_fn.assert_awaited_once()

    async def test_concurrency_limit_reached_skips_with_event(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """Active runs >= max_concurrent_runs must skip and log a poll event."""
        session = mock_db_components
        trigger = _make_trigger(
            max_concurrent_runs=2,
            config={"poll_interval_seconds": 60},
        )
        _setup_session_for_polling(session, trigger, active_run_count=2)

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.cron_helpers._log_poll_event") as mock_event,
        ):
            mock_event.return_value = MagicMock(id=uuid.uuid4())
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )

        assert result == {
            "status": "skipped",
            "reason": "concurrency_limit",
            "active_runs": 2,
        }
        mock_cr.assert_not_called()
        mock_event.assert_called_once()
        assert mock_event.call_args.kwargs["result"] == "concurrency_limit_reached"
        assert mock_event.call_args.kwargs["error_detail"] == ("Active runs: 2, limit: 2")


# ---------------------------------------------------------------------------
# _fire_polling_trigger — error paths
# ---------------------------------------------------------------------------


class TestFirePollingTriggerErrorPaths:
    async def test_connector_init_failed_returns_error(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """A failure building the connector must return connector_init_failed."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        build_mock, _ = mock_connector
        build_mock.side_effect = ValueError("invalid credentials")

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.cron_helpers._log_poll_event") as mock_event,
        ):
            mock_event.return_value = MagicMock(id=uuid.uuid4())
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "connector_init_failed"
        mock_cr.assert_not_called()
        mock_event.assert_called_once()
        assert mock_event.call_args.kwargs["result"] == "poll_error"

    async def test_query_timeout_returns_error(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """A query that exceeds the 60s timeout must be reported as query_timeout."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        _, connector = mock_connector
        connector.query.side_effect = TimeoutError()

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.cron_helpers._log_poll_event") as mock_event,
        ):
            mock_event.return_value = MagicMock(id=uuid.uuid4())
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "query_timeout"
        mock_cr.assert_not_called()
        mock_event.assert_called_once()
        assert mock_event.call_args.kwargs["error_detail"] == "Poll query timed out after 60s"

    async def test_query_failed_returns_error(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """A generic query failure must be reported as query_failed."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        _, connector = mock_connector
        connector.query.side_effect = RuntimeError("upstream 502")

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.cron_helpers._log_poll_event") as mock_event,
        ):
            mock_event.return_value = MagicMock(id=uuid.uuid4())
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )

        assert result["status"] == "error"
        assert result["reason"] == "query_failed"
        assert "upstream 502" in result["error"]
        mock_cr.assert_not_called()
        mock_event.assert_called_once()

    async def test_condition_eval_failed_returns_error(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """An invalid condition expression must be reported as condition_eval_failed."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.cron_helpers._log_poll_event") as mock_event,
            patch(
                "modulo.core.trigger_engine.polling.evaluate_condition",
                side_effect=ValueError("Invalid JMESPath expression"),
            ),
        ):
            mock_event.return_value = MagicMock(id=uuid.uuid4())
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression="[bad",
            )

        assert result["status"] == "error"
        assert result["reason"] == "condition_eval_failed"
        assert "Invalid JMESPath expression" in result["error"]
        mock_cr.assert_not_called()
        mock_event.assert_called_once()

    @pytest.mark.parametrize(
        "stage",
        [
            "connector_init",
            "query",
            "condition_eval",
        ],
    )
    async def test_cancelled_error_propagates(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        stage: str,
    ) -> None:
        """asyncio.CancelledError must never be swallowed by error handling."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        if stage == "connector_init":
            build_mock, _ = mock_connector
            build_mock.side_effect = asyncio.CancelledError()
        elif stage == "query":
            _, connector = mock_connector
            connector.query.side_effect = asyncio.CancelledError()
        else:
            with (
                patch(
                    "modulo.core.trigger_engine.polling.evaluate_condition",
                    side_effect=asyncio.CancelledError(),
                ),
                pytest.raises(asyncio.CancelledError),
            ):
                await _fire_polling_trigger(
                    trigger_id=_TRIGGER_ID,
                    org_id=_ORG_ID,
                    pipeline_id=_PIPELINE_ID,
                    connector_instance_id=_CI_ID,
                    poll_query="query",
                    condition_expression=None,
                )
            return

        with pytest.raises(asyncio.CancelledError):
            await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression=None,
            )


# ---------------------------------------------------------------------------
# _fire_polling_trigger — no_match and fire paths
# ---------------------------------------------------------------------------


class TestFirePollingTriggerNoMatch:
    async def test_condition_not_met_returns_no_match(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
    ) -> None:
        """A false condition must log no_match and advance next_fire without firing."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        with (
            patch("modulo.db.crud.run.create_run") as mock_cr,
            patch("modulo.core.cron_helpers._log_poll_event") as mock_event,
        ):
            mock_event.return_value = MagicMock(id=uuid.uuid4())
            result = await _fire_polling_trigger(
                trigger_id=_TRIGGER_ID,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                connector_instance_id=_CI_ID,
                poll_query="query",
                condition_expression="[?status == 'open']",
            )

        assert result == {"status": "no_match"}
        mock_cr.assert_not_called()
        mock_event.assert_called_once()
        assert mock_event.call_args.kwargs["result"] == "no_match"

    async def test_missing_snapshot_id_falls_back_to_zero_uuid(
        self,
        mock_db_components,
        mock_secrets_backend,
        mock_connector,
        mock_create_run,
    ) -> None:
        """A trigger with no snapshot_id in config must fire with the zero UUID."""
        session = mock_db_components
        trigger = _make_trigger(config={"poll_interval_seconds": 60})
        _setup_session_for_polling(session, trigger, connector_instance=MagicMock())

        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="query",
            condition_expression=None,
        )

        assert result["status"] == "fired"
        create_run_fn, _ = mock_create_run
        create_run_fn.assert_awaited_once()
        assert create_run_fn.call_args.kwargs["snapshot_id"] == uuid.UUID(int=0)


# ---------------------------------------------------------------------------
# _set_rls_org — non-Postgres dialect
# ---------------------------------------------------------------------------


class TestSetRlsOrg:
    async def test_sqlite_dialect_sets_session_info(self, mock_db_components) -> None:
        """On non-Postgres backends RLS context is stored in session.info."""
        session = mock_db_components
        session.info = {}
        bind_mock = MagicMock()
        bind_mock.dialect.name = "sqlite"
        session.get_bind = MagicMock(return_value=bind_mock)

        await _set_rls_org(session, _ORG_ID)

        assert session.info["organisation_id"] == _ORG_ID

    async def test_postgres_dialect_sets_config(self, mock_db_components) -> None:
        """On Postgres, RLS context is applied via SET LOCAL set_config."""
        session = mock_db_components
        session.info = {}
        bind_mock = MagicMock()
        bind_mock.dialect.name = "postgresql"
        session.get_bind = MagicMock(return_value=bind_mock)
        session.execute = AsyncMock()

        await _set_rls_org(session, _ORG_ID)

        # Org scope + the internal-execution hatch (app.execution_context) so
        # polling reads see team-private rows.
        assert session.execute.await_count == 2
        org_stmt, org_params = session.execute.await_args_list[0].args
        assert "set_config" in str(org_stmt).lower()
        assert org_params == {"val": str(_ORG_ID)}
        exec_stmt = session.execute.await_args_list[1].args[0]
        assert "app.execution_context" in str(exec_stmt).lower()


# ---------------------------------------------------------------------------
# _count_active_runs / _daily_spend_limit_reached — direct unit tests
# ---------------------------------------------------------------------------


class TestCountActiveRuns:
    async def test_counts_active_runs(self) -> None:
        session = MagicMock()
        result = MagicMock()
        result.scalar_one.return_value = 3
        session.execute = AsyncMock(return_value=result)
        trigger_id = uuid.uuid4()

        count = await _count_active_runs(session, trigger_id)

        assert count == 3
        stmt = str(session.execute.await_args.args[0]).lower()
        assert "trigger_id" in stmt
        assert "status in" in stmt

    async def test_zero_runs_when_none(self) -> None:
        session = MagicMock()
        result = MagicMock()
        result.scalar_one.return_value = None
        session.execute = AsyncMock(return_value=result)

        count = await _count_active_runs(session, uuid.uuid4())

        assert count == 0


class TestDailySpendLimitReached:
    def _session_with_cost(self, today_cost: Any) -> MagicMock:
        session = MagicMock()
        result = MagicMock()
        result.scalar_one.return_value = today_cost
        session.execute = AsyncMock(return_value=result)
        return session

    async def test_no_limit_configured_returns_none(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        trigger = _make_trigger(daily_spend_limit=None)

        assert await _daily_spend_limit_reached(session, trigger, _ORG_ID) is None
        session.execute.assert_not_awaited()

    async def test_limit_not_reached_returns_none(self) -> None:
        session = self._session_with_cost(Decimal("10.00"))
        trigger = _make_trigger(daily_spend_limit=Decimal("50.00"))

        assert await _daily_spend_limit_reached(session, trigger, _ORG_ID) is None

    async def test_limit_reached_returns_today_cost(self) -> None:
        session = self._session_with_cost(Decimal("55.00"))
        trigger = _make_trigger(daily_spend_limit=Decimal("50.00"))

        result = await _daily_spend_limit_reached(session, trigger, _ORG_ID)

        assert result == Decimal("55.00")
        stmt = str(session.execute.await_args.args[0]).lower()
        assert "runs.trigger_id" in stmt
        assert "runs.organisation_id" in stmt
