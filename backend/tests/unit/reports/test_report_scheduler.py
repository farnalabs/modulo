"""Unit tests for scheduled report framework — registry, entry, fire, delivery."""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from celery.beat import Scheduler

from modulo.core.reports.scheduler import (
    DatabaseReportEntry,
    DatabaseReportScheduler,
    ReportFireTask,
    _deliver_slack_webhook,
    _deliver_via_config,
    _deliver_webhook,
    _fire_scheduled_report,
    _get_engine,
    compute_next_send,
    get_deliverer,
    get_formatter,
    get_generator,
    register_report_type,
)
from modulo.db.models.scheduled_report import ScheduledReport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockBegin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    def __init__(self, execute_side_effect: list[MagicMock] | None = None) -> None:
        self._execute_mock = AsyncMock(side_effect=execute_side_effect or [])
        self.added: list[object] = []

    async def __aenter__(self) -> _MockSession:
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
    def __init__(self, session: _MockSession) -> None:
        self._session = session

    def __call__(self) -> _MockSession:
        return self._session


def _make_report_mock(
    *,
    active: bool = True,
    report_type: str = "quality",
    cron_expression: str = "0 9 * * 1",
    config_json: dict | None = None,
    recipient_config: dict | None = None,
) -> MagicMock:
    report = MagicMock(spec=ScheduledReport)
    report.id = uuid.uuid4()
    report.organisation_id = uuid.uuid4()
    report.active = active
    report.report_type = report_type
    report.cron_expression = cron_expression
    report.config_json = config_json or {}
    report.recipient_config = recipient_config or {}
    return report


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    def setup_method(self) -> None:
        # Clear registry before each test
        from modulo.core.reports import scheduler as sched_mod

        sched_mod._generators.clear()
        sched_mod._formatters.clear()
        sched_mod._deliverers.clear()

    async def _dummy_generator(self, *args: object) -> dict[str, object]:
        return {"data": "ok"}

    def test_register_and_get_generator(self) -> None:
        gen = self._dummy_generator
        register_report_type("test_type", gen)
        assert get_generator("test_type") is gen
        assert get_generator("unknown") is None

    def test_register_with_formatter_and_deliverer(self) -> None:
        def _fmt(data: object) -> str:
            return "formatted"

        async def _del(payload: object, config: object) -> list[dict[str, object]]:
            return [{"status": "ok"}]

        register_report_type("full_type", self._dummy_generator, formatter=_fmt, deliverer=_del)
        assert get_formatter("full_type") is _fmt
        assert get_deliverer("full_type") is _del

    def test_register_overwrites_existing(self) -> None:
        async def gen_a(*args: object) -> dict[str, object]:
            return {"a": 1}

        async def gen_b(*args: object) -> dict[str, object]:
            return {"b": 2}

        register_report_type("overwrite", gen_a)
        register_report_type("overwrite", gen_b)
        assert get_generator("overwrite") is gen_b


# ---------------------------------------------------------------------------
# compute_next_send tests
# ---------------------------------------------------------------------------


class TestComputeNextSend:
    def test_computes_next_minute(self) -> None:
        result = compute_next_send("* * * * *")
        assert isinstance(result, datetime.datetime)
        assert result.tzinfo is not None

    def test_daily_at_midnight(self) -> None:
        result = compute_next_send("0 0 * * *")
        assert result.hour == 0
        assert result.minute == 0

    def test_weekly_on_monday(self) -> None:
        result = compute_next_send("0 9 * * 1")
        assert result.hour == 9
        assert result.minute == 0

    def test_raises_on_invalid_expression(self) -> None:
        with pytest.raises((ValueError, KeyError)):
            compute_next_send("not-a-cron")


# ---------------------------------------------------------------------------
# DatabaseReportEntry tests
# ---------------------------------------------------------------------------


class TestDatabaseReportEntry:
    def test_entry_properties(self) -> None:
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseReportEntry(
            report_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            cron_expression="0 9 * * *",
            next_send_at=now,
        )
        assert entry.name.startswith("report-")
        assert entry.task == ReportFireTask.name
        assert len(entry.args) == 2
        assert isinstance(entry.schedule, DatabaseReportEntry)

    def test_is_due_when_past(self) -> None:
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        entry = DatabaseReportEntry(
            report_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_send_at=past,
        )
        due, delay = entry.is_due()
        assert due is True
        assert delay.total_seconds() == 0

    def test_is_not_due_when_future(self) -> None:
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        entry = DatabaseReportEntry(
            report_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            cron_expression="0 * * * *",
            next_send_at=future,
        )
        due, delay = entry.is_due()
        assert due is False
        assert delay.total_seconds() > 0

    def test_is_due_when_exactly_now(self) -> None:
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseReportEntry(
            report_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_send_at=now,
        )
        due, _delay = entry.is_due()
        assert due is True

    def test_repr(self) -> None:
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseReportEntry(
            report_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            cron_expression="0 9 * * *",
            next_send_at=now,
        )
        r = repr(entry)
        assert "DatabaseReportEntry" in r
        assert "next=" in r

    def test_options_contains_unique_task_id(self) -> None:
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseReportEntry(
            report_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_send_at=now,
        )
        opts = entry.options
        assert "task_id" in opts
        assert opts["task_id"].startswith("report-")


# ---------------------------------------------------------------------------
# DatabaseReportScheduler tests
# ---------------------------------------------------------------------------


class TestDatabaseReportSchedulerTick:
    def test_tick_calls_sync_with_db(self) -> None:
        scheduler = object.__new__(DatabaseReportScheduler)
        scheduler._schedule = {}
        scheduler.app = MagicMock()
        scheduler.data = {}

        with (
            patch.object(scheduler, "_sync_with_db") as mock_sync,
            patch.object(Scheduler, "tick", return_value=60.0),
        ):
            scheduler.tick()
        mock_sync.assert_called_once()

    def test_max_interval_is_60(self) -> None:
        scheduler = object.__new__(DatabaseReportScheduler)
        assert scheduler.max_interval == 60


# ---------------------------------------------------------------------------
# _fire_scheduled_report tests
# ---------------------------------------------------------------------------


class TestFireScheduledReport:
    def setup_method(self) -> None:
        from modulo.core.reports import scheduler as sched_mod

        sched_mod._generators.clear()
        sched_mod._formatters.clear()
        sched_mod._deliverers.clear()

    async def test_skips_when_report_missing(self) -> None:
        report_id = uuid.uuid4()
        org_id = uuid.uuid4()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None

        session = _MockSession(execute_side_effect=[result_mock])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=_MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
        ):
            result = await _fire_scheduled_report(report_id=report_id, org_id=org_id)

        assert result["status"] == "skipped"
        assert result["reason"] == "report_inactive_or_missing"

    async def test_skips_when_report_inactive(self) -> None:
        report_id = uuid.uuid4()
        org_id = uuid.uuid4()
        report_mock = _make_report_mock(active=False)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = report_mock

        session = _MockSession(execute_side_effect=[result_mock])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=_MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
        ):
            result = await _fire_scheduled_report(report_id=report_id, org_id=org_id)

        assert result["status"] == "skipped"
        assert result["reason"] == "report_inactive_or_missing"

    async def test_fails_when_no_generator_registered(self) -> None:
        report_id = uuid.uuid4()
        org_id = uuid.uuid4()
        report_mock = _make_report_mock(report_type="unknown_type")
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = report_mock

        session = _MockSession(execute_side_effect=[result_mock])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=_MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
        ):
            result = await _fire_scheduled_report(report_id=report_id, org_id=org_id)

        assert result["status"] == "failed"
        assert "no_generator" in result["reason"]

    async def test_generates_and_delivers_with_registered_components(self) -> None:
        async def dummy_generator(
            session: object, org_id: uuid.UUID, config: dict[str, object]
        ) -> dict[str, object]:
            return {"runs": 42, "pass_rate": 95.0}

        def dummy_formatter(data: dict[str, object]) -> str:
            return f"Report: {data['runs']} runs, {data['pass_rate']}% pass"

        async def dummy_deliverer(
            payload: str, config: dict[str, object]
        ) -> list[dict[str, object]]:
            return [{"url": "https://hooks.example.com", "status": "delivered"}]

        register_report_type("test_report", dummy_generator, formatter=dummy_formatter, deliverer=dummy_deliverer)

        report_id = uuid.uuid4()
        org_id = uuid.uuid4()
        report_mock = _make_report_mock(report_type="test_report", cron_expression="0 9 * * *")
        report_mock.id = report_id
        report_mock.organisation_id = org_id

        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = report_mock

        update_result = MagicMock()

        session = _MockSession(execute_side_effect=[select_result, update_result])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=_MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.reports.scheduler.compute_next_send",
                return_value=datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC),
            ),
        ):
            result = await _fire_scheduled_report(report_id=report_id, org_id=org_id)

        assert result["status"] == "sent"
        assert result["report_type"] == "test_report"
        assert len(result["delivery_results"]) == 1
        assert result["delivery_results"][0]["status"] == "delivered"

    async def test_updates_last_sent_and_next_send(self) -> None:
        async def dummy_generator(
            session: object, org_id: uuid.UUID, config: dict[str, object]
        ) -> dict[str, object]:
            return {"runs": 10}

        register_report_type("minimal", dummy_generator)
        org_id = uuid.uuid4()
        report_mock = _make_report_mock(report_type="minimal", cron_expression="0 9 * * *")
        report_mock.id = uuid.uuid4()

        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = report_mock

        update_result = MagicMock()

        session = _MockSession(execute_side_effect=[select_result, update_result])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=_MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.reports.scheduler.compute_next_send",
                return_value=datetime.datetime(2026, 7, 8, 9, 0, tzinfo=datetime.UTC),
            ),
        ):
            result = await _fire_scheduled_report(report_id=report_mock.id, org_id=org_id)

        assert result["status"] == "sent"
        assert result["next_send_at"] == "2026-07-08T09:00:00+00:00"


# ---------------------------------------------------------------------------
# Delivery function tests
# ---------------------------------------------------------------------------


class TestDeliverSlackWebhook:
    async def test_delivers_to_multiple_urls(self) -> None:
        import respx
        from httpx import Response

        url1 = "https://hooks.slack.com/services/T1/B1/xxx"
        url2 = "https://hooks.slack.com/services/T1/B2/yyy"

        with respx.mock:
            respx.post(url1).mock(return_value=Response(200, text="ok"))
            respx.post(url2).mock(return_value=Response(200, text="ok"))

            results = await _deliver_slack_webhook(
                {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]},
                [url1, url2],
            )

        assert len(results) == 2
        assert all(r["status"] == "delivered" for r in results)

    async def test_reports_failure(self) -> None:
        import respx
        from httpx import Response

        url = "https://hooks.slack.com/services/T1/B1/xxx"

        with respx.mock:
            respx.post(url).mock(return_value=Response(500, text="Internal Server Error"))

            results = await _deliver_slack_webhook({"text": "hello"}, [url])

        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] == 500


class TestDeliverWebhook:
    async def test_delivers_with_custom_headers(self) -> None:
        import respx
        from httpx import Response

        url = "https://hooks.example.com/report"
        config = {"urls": [url], "headers": {"X-Custom": "value"}}

        with respx.mock:
            route = respx.post(url).mock(return_value=Response(200, text="ok"))

            results = await _deliver_webhook({"report": "data"}, config)

        assert len(results) == 1
        assert results[0]["status"] == "delivered"
        assert route.calls.last.request.headers["X-Custom"] == "value"


class TestDeliverViaConfig:
    async def test_slack_webhook_type(self) -> None:
        import respx
        from httpx import Response

        url = "https://hooks.slack.com/services/T1/B1/xxx"
        config = {"type": "slack_webhook", "webhook_urls": [url]}

        with respx.mock:
            respx.post(url).mock(return_value=Response(200, text="ok"))

            results = await _deliver_via_config({"text": "hello"}, config, uuid.uuid4())

        assert len(results) == 1
        assert results[0]["status"] == "delivered"

    async def test_webhook_type_default(self) -> None:
        import respx
        from httpx import Response

        url = "https://hooks.example.com/report"
        config = {"urls": [url]}

        with respx.mock:
            respx.post(url).mock(return_value=Response(200, text="ok"))

            results = await _deliver_via_config({"report": "data"}, config, uuid.uuid4())

        assert len(results) == 1
        assert results[0]["status"] == "delivered"


# ---------------------------------------------------------------------------
# _get_engine tests
# ---------------------------------------------------------------------------


class TestGetEngine:
    def test_returns_cached_engine(self) -> None:
        import modulo.core.reports.scheduler as rsched

        saved = rsched._ENGINE
        try:
            rsched._ENGINE = None
            mock_engine = MagicMock()
            with (
                patch.object(rsched, "_ENGINE", None),
                patch.object(rsched, "create_async_engine", return_value=mock_engine) as mock_create,
                patch.object(rsched, "get_settings"),
            ):
                e1 = _get_engine()
                e2 = _get_engine()
                assert e1 is e2
                mock_create.assert_called_once()
        finally:
            rsched._ENGINE = saved
