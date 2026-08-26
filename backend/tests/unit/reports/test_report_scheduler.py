"""Unit tests for scheduled report framework — registry, entry, fire, delivery."""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from modulo.core.reports.scheduler import (
    _REPORT_HTTP_TIMEOUT,
    _coerce_timeout,
    _deliver_slack_webhook,
    _deliver_to_urls,
    _deliver_via_config,
    _deliver_webhook,
    _fire_scheduled_report,
    _get_engine,
    _parse_retry_after,
    _webhook_url_error,
    compute_next_send,
    get_deliverer,
    get_formatter,
    get_generator,
    register_report_type,
)
from tests.unit.reports.helpers import MockSession, MockSessionFactory, make_report_mock

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
        with pytest.raises(ValueError, match="columns has to be specified"):
            compute_next_send("not-a-cron")

    def test_raises_type_error_when_croniter_returns_unexpected_type(self) -> None:
        with patch("modulo.core.reports.scheduler.croniter") as mock_croniter:
            fake_cron = MagicMock()
            fake_cron.get_next.return_value = "2026-07-01"
            mock_croniter.return_value = fake_cron
            with pytest.raises(TypeError, match="croniter returned unexpected type"):
                compute_next_send("0 9 * * *")


# ---------------------------------------------------------------------------
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

        session = MockSession(execute_side_effect=[result_mock])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
        ):
            result = await _fire_scheduled_report(report_id=report_id, org_id=org_id)

        assert result["status"] == "skipped"
        assert result["reason"] == "report_inactive_or_missing"

    async def test_skips_when_report_inactive(self) -> None:
        report_id = uuid.uuid4()
        org_id = uuid.uuid4()
        report_mock = make_report_mock(active=False)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = report_mock

        session = MockSession(execute_side_effect=[result_mock])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
        ):
            result = await _fire_scheduled_report(report_id=report_id, org_id=org_id)

        assert result["status"] == "skipped"
        assert result["reason"] == "report_inactive_or_missing"

    async def test_fails_when_no_generator_registered(self) -> None:
        report_id = uuid.uuid4()
        org_id = uuid.uuid4()
        report_mock = make_report_mock(report_type="unknown_type")
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = report_mock

        session = MockSession(execute_side_effect=[result_mock])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=MockSessionFactory(session),
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
        report_mock = make_report_mock(report_type="test_report", cron_expression="0 9 * * *")
        report_mock.id = report_id
        report_mock.organisation_id = org_id

        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = report_mock

        update_result = MagicMock()

        session = MockSession(execute_side_effect=[select_result, update_result])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=MockSessionFactory(session),
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
        report_mock = make_report_mock(report_type="minimal", cron_expression="0 9 * * *")
        report_mock.id = uuid.uuid4()

        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = report_mock

        update_result = MagicMock()

        session = MockSession(execute_side_effect=[select_result, update_result])

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=MockSessionFactory(session),
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

    async def test_reraises_cancelled_error_from_generator(self) -> None:
        report_id = uuid.uuid4()
        org_id = uuid.uuid4()
        report_mock = make_report_mock(report_type="cancelled")
        report_mock.id = report_id
        report_mock.organisation_id = org_id

        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = report_mock

        session = MockSession(execute_side_effect=[select_result])

        async def _cancelled_generator(*args: object) -> dict[str, object]:
            raise asyncio.CancelledError

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=MockSessionFactory(session),
            ),
            patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.reports.scheduler.get_generator",
                return_value=_cancelled_generator,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _fire_scheduled_report(report_id=report_id, org_id=org_id)


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

    async def test_timeout_passes_through_recipient_config(self) -> None:
        """The recipient config ``timeout`` must reach the underlying HTTP
        client — and a boolean value must be rejected, not coerced to 1.0."""
        url = "https://hooks.example.com/report"

        with (
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client = AsyncMock()
            resp = MagicMock()
            resp.is_success = True
            resp.status_code = 200
            client.post = AsyncMock(return_value=resp)
            client_cls.return_value.__aenter__.return_value = client

            await _deliver_via_config(
                {"report": "data"},
                {"urls": [url], "timeout": True},
            )

        assert client_cls.call_args.kwargs["timeout"] == _REPORT_HTTP_TIMEOUT

        client_cls.reset_mock()
        with patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            resp = MagicMock()
            resp.is_success = True
            resp.status_code = 200
            client.post = AsyncMock(return_value=resp)
            client_cls.return_value.__aenter__.return_value = client

            await _deliver_via_config(
                {"report": "data"},
                {"urls": [url], "timeout": 4.25},
            )

        assert client_cls.call_args.kwargs["timeout"] == 4.25


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

    def test_engine_created_with_pool_pre_ping(self) -> None:
        import modulo.core.reports.scheduler as rsched

        saved = rsched._ENGINE
        try:
            rsched._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.modulo_db = "postgres"
            mock_engine = MagicMock()
            with (
                patch.object(rsched, "_ENGINE", None),
                patch.object(rsched, "create_async_engine", return_value=mock_engine) as mock_create,
                patch.object(rsched, "get_settings", return_value=settings_mock),
            ):
                _get_engine()
            _, kwargs = mock_create.call_args
            assert kwargs["pool_pre_ping"] is True
            assert kwargs["connect_args"]["statement_cache_size"] == 0
            assert kwargs["connect_args"]["ssl"] is False
        finally:
            rsched._ENGINE = saved


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
# _coerce_timeout tests — caller-supplied timeout validation gate
# ---------------------------------------------------------------------------


class TestCoerceTimeout:
    def test_accepts_positive_int(self) -> None:
        assert _coerce_timeout(10) == 10.0

    def test_accepts_positive_float(self) -> None:
        assert _coerce_timeout(2.5) == 2.5

    def test_accepts_numeric_string(self) -> None:
        assert _coerce_timeout("5.5") == 5.5
        assert _coerce_timeout("7") == 7.0

    def test_accepts_fractional_string_below_one(self) -> None:
        assert _coerce_timeout("0.5") == 0.5

    def test_rejects_bool_true(self) -> None:
        """A boolean must never coerce to ``1.0`` — ``float(True)`` is 1.0, so
        without the guard a ``{"timeout": true}`` config would silently become a
        1-second request timeout."""
        assert _coerce_timeout(True) is None

    def test_rejects_bool_false(self) -> None:
        assert _coerce_timeout(False) is None

    def test_rejects_zero(self) -> None:
        assert _coerce_timeout(0) is None
        assert _coerce_timeout(0.0) is None
        assert _coerce_timeout("0") is None

    def test_rejects_negative(self) -> None:
        assert _coerce_timeout(-1) is None
        assert _coerce_timeout(-0.5) is None

    def test_rejects_non_numeric(self) -> None:
        assert _coerce_timeout("abc") is None
        assert _coerce_timeout(None) is None
        assert _coerce_timeout(object()) is None


# ---------------------------------------------------------------------------
# _webhook_url_error tests — pre-flight URL validation gate
# ---------------------------------------------------------------------------


class TestWebhookUrlError:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://hooks.slack.com/services/T1/B1/xxx", None),
            ("http://hooks.example.com/report", None),
            ("https://hooks.example.com:8443/x?y=1", None),
            ("  https://hooks.example.com/x  ", None),
            (None, "url_not_a_string"),
            (123, "url_not_a_string"),
            ("", "url_empty"),
            ("   ", "url_empty"),
            (" \t ", "url_empty"),
            ("https://exa mple.com/x", "url_contains_whitespace"),
            ("http://[::1", "url_malformed"),
            ("ftp://hooks.example.com/x", "url_scheme_not_http"),
            ("file:///etc/passwd", "url_scheme_not_http"),
            ("//hooks.example.com/x", "url_scheme_not_http"),
            ("notaurl", "url_scheme_not_http"),
            ("https://", "url_missing_host"),
            ("https://#fragment", "url_missing_host"),
            ("https://user:pass@hooks.example.com/x", "url_contains_credentials"),
            ("https://user@hooks.example.com/x", "url_contains_credentials"),
            ("https://hooks.example.com:abc/x", "url_malformed"),
            ("https://hooks.example.com:99999/x", "url_malformed"),
        ],
        ids=[
            "valid-slack",
            "valid-http",
            "valid-port-query",
            "valid-padded",
            "none",
            "int",
            "empty",
            "blank",
            "tab-blank",
            "contains-whitespace",
            "unclosed-bracket",
            "ftp-scheme",
            "file-scheme",
            "protocol-relative",
            "notaurl",
            "missing-host",
            "fragment-missing-host",
            "contains-credentials",
            "user-only",
            "bad-port",
            "port-out-of-range",
        ],
    )
    def test_validation_matrix(self, url: object, expected: str | None) -> None:
        assert _webhook_url_error(url) == expected


class TestDeliverToUrlsRejectsInvalid:
    async def _spy_client(self) -> MagicMock:
        """Build a client whose ``post`` fails the test if ever called."""
        client = AsyncMock()
        client.post = AsyncMock(side_effect=AssertionError("post() must not be called for an invalid URL"))
        return client

    async def test_invalid_url_fails_fast_without_network_or_sleep(self) -> None:
        """A permanently-invalid URL must not enter the retry/backoff loop:
        no HTTP attempt, no exponential-backoff sleeps, just a typed failure."""
        client = await self._spy_client()
        with (
            patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls(["ftp://hooks.example.com/x"], {"a": 1})

        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] is None
        assert results[0]["error"] == "invalid_webhook_url: url_scheme_not_http"
        sleep.assert_not_awaited()
        client.post.assert_not_awaited()

    async def test_missing_host_and_embedded_credentials_are_rejected(self) -> None:
        client = await self._spy_client()
        urls = ["https://", "https://user:pass@hooks.example.com/x", ""]
        with patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls(urls, {"a": 1})

        assert [r["error"] for r in results] == [
            "invalid_webhook_url: url_missing_host",
            "invalid_webhook_url: url_contains_credentials",
            "invalid_webhook_url: url_empty",
        ]
        client.post.assert_not_awaited()

    async def test_invalid_url_does_not_block_valid_siblings(self) -> None:
        """Per-URL isolation: one bad URL must not prevent delivery to good URLs."""
        url = "https://hooks.example.com/x"
        good_client = _deliver_client([_ok_resp(200)])
        with (
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = good_client
            results = await _deliver_to_urls(["notaurl", url, "file:///tmp/x"], {"a": 1})

        assert [r["status"] for r in results] == ["failed", "delivered", "failed"]
        assert results[0]["error"] == "invalid_webhook_url: url_scheme_not_http"
        assert results[2]["error"] == "invalid_webhook_url: url_scheme_not_http"
        assert results[0]["status_code"] is None
        assert results[2]["status_code"] is None

    async def test_bad_port_urls_fail_fast_without_retry(self) -> None:
        """``urlsplit`` accepts ``host:abc`` and ``host:99999``, but httpx
        would raise ``InvalidURL`` — a permanent config error that must be
        caught pre-flight so it never enters the retry/backoff loop."""
        client = await self._spy_client()
        urls = ["https://hooks.example.com:abc/x", "https://hooks.example.com:99999/x"]
        with (
            patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls(urls, {"a": 1})

        assert [r["error"] for r in results] == [
            "invalid_webhook_url: url_malformed",
            "invalid_webhook_url: url_malformed",
        ]
        assert all(r["status_code"] is None for r in results)
        sleep.assert_not_awaited()
        client.post.assert_not_awaited()

    async def test_credentialed_url_is_redacted_in_result_and_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """A credentialed URL must not leak its ``user:pass`` into the result
        ``url`` field (which flows into the quality-report API and the SAQ job
        result) or into the warning log."""
        client = await self._spy_client()
        credentialed = "https://user:secret@hooks.example.com/x"
        with (
            caplog.at_level(logging.WARNING, logger="modulo.core.reports.scheduler"),
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = client
            results = await _deliver_to_urls([credentialed], {"a": 1})

        assert results[0]["error"] == "invalid_webhook_url: url_contains_credentials"
        assert results[0]["url"] == "https://hooks.example.com/x"
        assert "secret" not in results[0]["url"]
        assert "user:secret" not in caplog.text
        assert "https://hooks.example.com/x" in caplog.text
        client.post.assert_not_awaited()


class TestDeliverToUrlsTimeout:
    async def _client_timeout(self, request_timeout: object) -> float:
        """Call ``_deliver_to_urls`` with *request_timeout* and return the
        ``timeout`` the underlying ``httpx.AsyncClient`` was built with."""
        url = "https://hooks.example.com/x"
        client = _deliver_client([_ok_resp(200)])
        with patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__.return_value = client
            await _deliver_to_urls([url], {"a": 1}, request_timeout=request_timeout)  # type: ignore[arg-type]
        return client_cls.call_args.kwargs["timeout"]

    async def test_valid_float_timeout_is_honored(self) -> None:
        assert await self._client_timeout(2.5) == 2.5

    async def test_valid_int_timeout_is_honored(self) -> None:
        assert await self._client_timeout(15) == 15.0

    async def test_none_timeout_uses_default(self) -> None:
        assert await self._client_timeout(None) == _REPORT_HTTP_TIMEOUT

    async def test_bool_timeout_falls_back_to_default(self) -> None:
        """``{"timeout": true}`` in a recipient config must NOT become a 1s
        timeout — the bool guard in ``_coerce_timeout`` must fall back to the
        default 30s."""
        assert await self._client_timeout(True) == _REPORT_HTTP_TIMEOUT

    async def test_zero_timeout_falls_back_to_default(self) -> None:
        assert await self._client_timeout(0) == _REPORT_HTTP_TIMEOUT

    async def test_negative_timeout_falls_back_to_default(self) -> None:
        assert await self._client_timeout(-5) == _REPORT_HTTP_TIMEOUT

    async def test_non_numeric_timeout_falls_back_to_default(self) -> None:
        assert await self._client_timeout("abc") == _REPORT_HTTP_TIMEOUT


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

    async def test_reports_max_retries_exceeded_with_no_response(self) -> None:
        url = "https://hooks.example.com/x"

        with (
            patch("modulo.core.reports.scheduler._REPORT_MAX_RETRIES", 0),
            patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls,
        ):
            client_cls.return_value.__aenter__.return_value = _deliver_client([])
            results = await _deliver_to_urls([url], {"a": 1})

        assert results[0]["status"] == "failed"
        assert results[0]["status_code"] is None
        assert results[0]["error"] == "max_retries_exceeded"


# ---------------------------------------------------------------------------
# _sync_with_db tests
# ---------------------------------------------------------------------------


class TestFireInvalidCron:
    def setup_method(self) -> None:
        from modulo.core.reports import scheduler as sched_mod

        sched_mod._generators.clear()
        sched_mod._formatters.clear()
        sched_mod._deliverers.clear()

    async def _make_ctx(self, report: MagicMock) -> MockSession:
        select_result = MagicMock()
        select_result.scalar_one_or_none.return_value = report
        return MockSession(execute_side_effect=[select_result, MagicMock()])

    async def test_deactivates_report_on_invalid_cron(self) -> None:
        async def dummy_generator(session: object, org_id: uuid.UUID, config: dict[str, object]) -> dict[str, object]:
            return {"runs": 1}

        register_report_type("bad_cron", dummy_generator)

        report = make_report_mock(report_type="bad_cron", cron_expression="not-a-cron")
        report.config_json = {"schedule_type": "recurring"}
        report.id = uuid.uuid4()
        report.organisation_id = uuid.uuid4()

        session = await self._make_ctx(report)

        with (
            patch("modulo.core.reports.scheduler._get_engine"),
            patch(
                "modulo.core.reports.scheduler.async_sessionmaker",
                return_value=MockSessionFactory(session),
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

        update_stmt = session.execute.await_args_list[1].args[0]
        update_values = {column.key: value.value for column, value in update_stmt._values.items()}
        assert update_values["active"] is False
