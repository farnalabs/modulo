"""Unit tests for polling trigger — evaluate_condition, _fire_polling_trigger, scheduler."""

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
    _fire_polling_trigger,
    _log_poll_event,
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
    today_cost: Any = 0,
) -> None:
    """Configure session.execute to handle all DB queries from _fire_polling_trigger.

    The function makes calls in this order:
      1. _set_rls_org → text(...)
      2. select(Trigger).with_for_update()
      3. _count_active_runs → select(func.count())
      4. _daily_spend_limit_reached → select(coalesce(sum(Run.total_cost_usd), 0))
      5. select(ConnectorInstance)
      6. update(Trigger)  (in _update_next_fire)
    """
    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    ci_result = MagicMock()
    ci_result.scalar_one_or_none.return_value = connector_instance

    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count

    cost_result = MagicMock()
    cost_result.scalar_one.return_value = today_cost

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
            patch("modulo.core.trigger_engine.polling.create_run") as mock_cr,
            patch("modulo.core.trigger_engine.polling._log_poll_event") as mock_event,
            patch("modulo.core.trigger_engine.polling._update_next_fire_no_last") as mock_advance,
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
        mock_advance.assert_awaited_once()

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
            patch("modulo.core.trigger_engine.polling.create_run") as mock_cr,
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
