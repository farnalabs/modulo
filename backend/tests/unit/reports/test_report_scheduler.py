"""Unit tests for scheduled report framework — registry, entry, fire, delivery."""

from __future__ import annotations

import datetime
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from celery.beat import Scheduler

from modulo.core.pipeline_execution import SchedulerDBError
from modulo.core.reports.scheduler import (
    DatabaseReportEntry,
    DatabaseReportScheduler,
    ReportFireTask,
    _deliver_slack_webhook,
    _deliver_to_urls,
    _deliver_via_config,
    _deliver_webhook,
    _fire_scheduled_report,
    _get_engine,
    _parse_retry_after,
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

    def test_setup_schedule_syncs_with_db(self) -> None:
        scheduler = object.__new__(DatabaseReportScheduler)
        scheduler._schedule = {}
        scheduler.app = MagicMock()
        scheduler.data = {}
        with patch.object(scheduler, "_sync_with_db") as mock_sync:
            scheduler.setup_schedule()
        mock_sync.assert_called_once()


# ---------------------------------------------------------------------------
# _set_rls_org tests
# ---------------------------------------------------------------------------


class TestSetRlsOrg:
    async def test_delegates_to_db_rls_helper(self) -> None:
        from modulo.core.reports.scheduler import _set_rls_org

        session = MagicMock()
        org_id = uuid.uuid4()
        with patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock) as mock_set:
            await _set_rls_org(session, org_id)
        mock_set.assert_awaited_once_with(session, org_id)


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
        async def dummy_generator(session: object, org_id: uuid.UUID, config: dict[str, object]) -> dict[str, object]:
            return {"runs": 42, "pass_rate": 95.0}

        def dummy_formatter(data: dict[str, object]) -> str:
            return f"Report: {data['runs']} runs, {data['pass_rate']}% pass"

        async def dummy_deliverer(payload: str, config: dict[str, object]) -> list[dict[str, object]]:
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
        async def dummy_generator(session: object, org_id: uuid.UUID, config: dict[str, object]) -> dict[str, object]:
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

            results = await _deliver_via_config({"text": "hello"}, config)

        assert len(results) == 1
        assert results[0]["status"] == "delivered"

    async def test_webhook_type_default(self) -> None:
        import respx
        from httpx import Response

        url = "https://hooks.example.com/report"
        config = {"urls": [url]}

        with respx.mock:
            respx.post(url).mock(return_value=Response(200, text="ok"))

            results = await _deliver_via_config({"report": "data"}, config)

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

    def test_returns_test_engine_when_set(self) -> None:
        import modulo.core.reports.scheduler as rsched

        saved = rsched._TEST_ENGINE
        try:
            mock_engine = MagicMock()
            rsched._set_test_engine(mock_engine)
            assert _get_engine() is mock_engine
        finally:
            rsched._set_test_engine(saved)

    def test_reset_test_engine_restores_default(self) -> None:
        import modulo.core.reports.scheduler as rsched

        saved_engine = rsched._ENGINE
        saved_test = rsched._TEST_ENGINE
        try:
            rsched._TEST_ENGINE = None
            rsched._ENGINE = None
            real = MagicMock()
            with (
                patch.object(rsched, "create_async_engine", return_value=real) as mock_create,
                patch.object(rsched, "get_settings"),
            ):
                rsched._set_test_engine(real)
                assert _get_engine() is real
                rsched._set_test_engine(None)
                assert _get_engine() is real
                mock_create.assert_called_once()
        finally:
            rsched._ENGINE = saved_engine
            rsched._TEST_ENGINE = saved_test


# ---------------------------------------------------------------------------
# compute_next_send tests
# ---------------------------------------------------------------------------


class TestComputeNextSendAfter:
    def test_uses_after_when_provided(self) -> None:
        base = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.UTC)
        result = compute_next_send("0 9 * * *", after=base)
        assert result == datetime.datetime(2026, 7, 2, 9, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# _parse_retry_after tests
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def _resp(self, retry_after: str | None) -> MagicMock:
        resp = MagicMock()
        resp.headers = {} if retry_after is None else {"Retry-After": retry_after}
        return resp

    def test_parses_numeric_header(self) -> None:
        assert _parse_retry_after(self._resp("3")) == 3.0

    def test_parses_float_header(self) -> None:
        assert _parse_retry_after(self._resp("2.5")) == 2.5

    def test_defaults_when_header_missing(self) -> None:
        assert _parse_retry_after(self._resp(None)) == 5.0

    def test_defaults_on_non_numeric_header(self) -> None:
        assert _parse_retry_after(self._resp("soon")) == 5.0


# ---------------------------------------------------------------------------
# _deliver_to_urls tests
# ---------------------------------------------------------------------------


def _deliver_client(side_effect: list[object]) -> MagicMock:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=side_effect)
    return client


def _ok_resp(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.is_success = status_code < 400
    resp.status_code = status_code
    resp.text = "ok"
    resp.headers = {}
    return resp


class TestDeliverToUrls:
    async def test_retries_429_then_succeeds(self) -> None:
        url = "https://hooks.example.com/x"
        client = _deliver_client([_ok_resp(429), _ok_resp(200)])

        with (
            patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls([url], {"a": 1})

        assert results[0]["status"] == "delivered"
        sleep.assert_awaited_once()

    async def test_exhausts_retries_on_500(self) -> None:
        url = "https://hooks.example.com/x"
        client = _deliver_client([_ok_resp(500)] * 3)

        with (
            patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls([url], {"a": 1})

        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] == 500
        assert sleep.await_count == 3

    async def test_does_not_retry_4xx(self) -> None:
        url = "https://hooks.example.com/x"
        client = _deliver_client([_ok_resp(400)])

        with (
            patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls([url], {"a": 1})

        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] == 400
        sleep.assert_not_awaited()

    async def test_retries_transient_request_error_then_succeeds(self) -> None:
        url = "https://hooks.example.com/x"
        client = _deliver_client([httpx.RequestError("connection refused"), _ok_resp(200)])

        with (
            patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls([url], {"a": 1})

        assert results[0]["status"] == "delivered"
        sleep.assert_awaited_once()

    async def test_reports_error_when_all_request_attempts_fail(self) -> None:
        url = "https://hooks.example.com/x"
        client = _deliver_client([httpx.RequestError("down")] * 3)

        with (
            patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock),
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls([url], {"a": 1})

        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] is None
        assert results[0]["error"] == "down"


# ---------------------------------------------------------------------------
# _sync_with_db tests
# ---------------------------------------------------------------------------


class TestSyncWithDb:
    def _scheduler(self) -> DatabaseReportScheduler:
        scheduler = object.__new__(DatabaseReportScheduler)
        scheduler._schedule = {}
        return scheduler

    def test_adds_new_entries(self) -> None:
        scheduler = self._scheduler()
        rid = uuid.uuid4()
        rows = [
            {
                "report_id": rid,
                "org_id": uuid.uuid4(),
                "cron_expression": "0 9 * * 1",
                "next_send_at": datetime.datetime(2026, 7, 20, tzinfo=datetime.UTC),
            }
        ]
        with patch.object(scheduler, "_fetch_due_reports", return_value=rows):
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == 1
        assert f"report-{rid}" in scheduler._schedule

    def test_updates_changed_next_send_at(self) -> None:
        scheduler = self._scheduler()
        rid = uuid.uuid4()
        org_id = uuid.uuid4()
        first = datetime.datetime(2026, 7, 20, tzinfo=datetime.UTC)
        second = datetime.datetime(2026, 7, 27, tzinfo=datetime.UTC)
        rows1 = [
            {
                "report_id": rid,
                "org_id": org_id,
                "cron_expression": "0 9 * * 1",
                "next_send_at": first,
            }
        ]
        with patch.object(scheduler, "_fetch_due_reports", return_value=rows1):
            scheduler._sync_with_db()
        entry = scheduler._schedule[f"report-{rid}"]
        assert entry._next_send_at == first

        rows2 = [
            {
                "report_id": rid,
                "org_id": org_id,
                "cron_expression": "0 9 * * 1",
                "next_send_at": second,
            }
        ]
        with patch.object(scheduler, "_fetch_due_reports", return_value=rows2):
            scheduler._sync_with_db()
        assert scheduler._schedule[f"report-{rid}"]._next_send_at == second

    def test_skips_unmodified_entries(self) -> None:
        scheduler = self._scheduler()
        rid = uuid.uuid4()
        rows = [
            {
                "report_id": rid,
                "org_id": uuid.uuid4(),
                "cron_expression": "0 9 * * 1",
                "next_send_at": datetime.datetime(2026, 7, 20, tzinfo=datetime.UTC),
            }
        ]
        with patch.object(scheduler, "_fetch_due_reports", return_value=rows):
            scheduler._sync_with_db()
        original = scheduler._schedule[f"report-{rid}"]

        with patch.object(scheduler, "_fetch_due_reports", return_value=rows):
            scheduler._sync_with_db()
        assert scheduler._schedule[f"report-{rid}"] is original

    def test_removes_stale_entries(self) -> None:
        scheduler = self._scheduler()
        rid = uuid.uuid4()
        rows = [
            {
                "report_id": rid,
                "org_id": uuid.uuid4(),
                "cron_expression": "0 9 * * 1",
                "next_send_at": datetime.datetime(2026, 7, 20, tzinfo=datetime.UTC),
            }
        ]
        with patch.object(scheduler, "_fetch_due_reports", return_value=rows):
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == 1

        with patch.object(scheduler, "_fetch_due_reports", return_value=[]):
            scheduler._sync_with_db()
        assert len(scheduler._schedule) == 0

    def test_swallows_scheduler_db_error(self) -> None:
        scheduler = self._scheduler()
        with patch.object(scheduler, "_fetch_due_reports", side_effect=SchedulerDBError("db down")):
            scheduler._sync_with_db()
        assert scheduler._schedule == {}


# ---------------------------------------------------------------------------
# DatabaseReportEntry extra tests
# ---------------------------------------------------------------------------


class TestDatabaseReportEntryExtra:
    def test_kwargs_returns_empty_dict(self) -> None:
        entry = DatabaseReportEntry(
            report_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            cron_expression="0 9 * * *",
            next_send_at=datetime.datetime.now(datetime.UTC),
        )
        assert entry.kwargs == {}


# ---------------------------------------------------------------------------
# ReportFireTask tests
# ---------------------------------------------------------------------------


class TestReportFireTask:
    def test_run_delegates_to_fire_scheduled_report(self) -> None:
        task = object.__new__(ReportFireTask)
        report_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        with patch(
            "modulo.core.reports.scheduler._fire_scheduled_report",
            new_callable=AsyncMock,
            return_value={"status": "sent"},
        ) as mock_fire:
            result = task.run(report_id, org_id)

        assert result == {"status": "sent"}
        mock_fire.assert_awaited_once()
        assert mock_fire.await_args is not None
        args = mock_fire.await_args.kwargs
        assert str(args["report_id"]) == report_id
        assert str(args["org_id"]) == org_id


# ---------------------------------------------------------------------------
# _fetch_due_reports tests
# ---------------------------------------------------------------------------


class TestFetchDueReports:
    def test_maps_rows_to_report_dicts(self) -> None:
        scheduler = object.__new__(DatabaseReportScheduler)
        rid = uuid.uuid4()
        org_id = uuid.uuid4()
        next_send = datetime.datetime(2026, 7, 20, tzinfo=datetime.UTC)

        row = MagicMock()
        row.id = rid
        row.organisation_id = org_id
        row.cron_expression = "0 9 * * 1"
        row.next_send_at = next_send

        session = MagicMock()
        result = MagicMock()
        result.all.return_value = [row]
        session.execute.return_value = result

        with patch("modulo.core.pipeline_execution.get_beat_sync_session", return_value=session):
            rows = scheduler._fetch_due_reports()

        assert rows == [
            {
                "report_id": rid,
                "org_id": org_id,
                "cron_expression": "0 9 * * 1",
                "next_send_at": next_send,
            }
        ]
        session.close.assert_called_once()

    def test_queries_only_due_active_reports(self) -> None:
        scheduler = object.__new__(DatabaseReportScheduler)
        session = MagicMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute.return_value = result

        with patch("modulo.core.pipeline_execution.get_beat_sync_session", return_value=session):
            scheduler._fetch_due_reports()

        sql = str(session.execute.call_args[0][0])
        assert "scheduled_reports.active" in sql
        assert "scheduled_reports.next_send_at <=" in sql

    def test_raises_scheduler_db_error_on_operational_error(self) -> None:
        from sqlalchemy.exc import OperationalError

        scheduler = object.__new__(DatabaseReportScheduler)
        session = MagicMock()
        session.execute.side_effect = OperationalError("stmt", {}, Exception("db down"))

        with (
            patch("modulo.core.pipeline_execution.get_beat_sync_session", return_value=session),
            pytest.raises(SchedulerDBError, match="Report scheduler DB query failed"),
        ):
            scheduler._fetch_due_reports()

    def test_raises_scheduler_db_error_on_timeout(self) -> None:
        from sqlalchemy.exc import TimeoutError as SA_TimeoutError

        scheduler = object.__new__(DatabaseReportScheduler)
        session = MagicMock()
        session.execute.side_effect = SA_TimeoutError("db timeout")

        with (
            patch("modulo.core.pipeline_execution.get_beat_sync_session", return_value=session),
            pytest.raises(SchedulerDBError),
        ):
            scheduler._fetch_due_reports()


# ---------------------------------------------------------------------------
# _fire_scheduled_report: invalid cron expression deactivation
# ---------------------------------------------------------------------------


class TestFireInvalidCron:
    def setup_method(self) -> None:
        from modulo.core.reports import scheduler as sched_mod

        sched_mod._generators.clear()
        sched_mod._formatters.clear()
        sched_mod._deliverers.clear()

    async def _make_ctx(self, report: MagicMock) -> _MockSession:
        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = report
        return _MockSession(execute_side_effect=[select_result, MagicMock()])

    async def test_deactivates_report_on_invalid_cron(self) -> None:
        async def dummy_generator(session: object, org_id: uuid.UUID, config: dict[str, object]) -> dict[str, object]:
            return {"runs": 1}

        register_report_type("bad_cron", dummy_generator)

        report = _make_report_mock(report_type="bad_cron", cron_expression="not-a-cron")
        report.config_json = {"schedule_type": "recurring"}
        report.id = uuid.uuid4()
        report.organisation_id = uuid.uuid4()

        session = await self._make_ctx(report)

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=_MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.reports.scheduler.compute_next_send",
                side_effect=ValueError("invalid cron"),
            ),
        ):
            result = await _fire_scheduled_report(report_id=report.id, org_id=report.organisation_id)

        assert result["status"] == "failed"
        assert "invalid_cron" in result["reason"]

        update_stmt = session._execute_mock.await_args_list[1].args[0]
        update_values = {column.key: value.value for column, value in update_stmt._values.items()}
        assert update_values["active"] is False
