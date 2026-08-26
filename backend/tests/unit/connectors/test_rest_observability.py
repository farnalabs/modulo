"""Unit tests for REST connector observability (FAR-413).

Covers ``modulo.connectors.rest.rest_metrics`` (OTel instrument registration +
record helpers), the connector's metric wiring (that a real request emits the
exact metric names/attributes), and ``modulo.connectors.rest.rest_rollback``
(threshold evaluator + cause-code classification).

These are pure unit tests: no network, no DB, no real meter provider. The OTel
meter is injected via ``sys.modules``/``_get_meter`` the same way
``test_error_metrics.py`` stubs OTel.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest

import modulo.connectors.rest.rest_metrics as rest_metrics
import modulo.connectors.rest.rest_rollback as rest_rollback
from modulo.connectors.base import ConnectorQuery
from modulo.connectors.rest import RESTConnectError, RestConnector, RESTResponseTooLargeError, SecurityGuard
from modulo.connectors.rest.rest_rollback import RestRollbackSignal, evaluate_rest_rollback, is_unknown_like


@pytest.fixture(autouse=True)
def _reset_rest_metric_handles() -> Iterator[None]:
    """Save/restore rest_metrics module handles so tests never leak state."""
    saved = tuple(getattr(rest_metrics, name, None) for name in _HANDLE_NAMES)
    for name in _HANDLE_NAMES:
        setattr(rest_metrics, name, None)
    yield
    for name, value in zip(_HANDLE_NAMES, saved, strict=False):
        setattr(rest_metrics, name, value)


_HANDLE_NAMES = (
    "_requests_histogram",
    "_outcome_total",
    "_retry_total",
    "_ssrf_blocked_total",
    "_redaction_total",
)


def _storage_meter() -> tuple[MagicMock, dict[str, MagicMock], dict[str, MagicMock]]:
    """A meter whose create_* store handles by instrument name so a test can
    assert emission attributes per exact metric name."""
    histograms: dict[str, MagicMock] = {}
    counters: dict[str, MagicMock] = {}
    meter = MagicMock()

    def mk_hist(name: str, *, description: str = "", unit: str = "1") -> MagicMock:
        handle = MagicMock()
        handle.name = name
        histograms[name] = handle
        return handle

    def mk_counter(name: str, *, description: str = "", unit: str = "1") -> MagicMock:
        handle = MagicMock()
        handle.name = name
        counters[name] = handle
        return handle

    meter.create_histogram.side_effect = mk_hist
    meter.create_counter.side_effect = mk_counter
    return meter, histograms, counters


def _noop_guard() -> SecurityGuard:
    async def validate_url(url: str) -> None:
        return None

    def filter_strings(values: list[str], resource: str) -> None:
        return None

    return SecurityGuard(validate_url=validate_url, filter_strings=filter_strings)


def _run_query(meter: MagicMock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
    )
    with patch.object(rest_metrics, "_get_meter", return_value=meter):
        import asyncio

        asyncio.run(c.query(ConnectorQuery(resource="default")))


# ── classify_status ─────────────────────────────────────────────────────────


class TestClassifyStatus:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (200, rest_metrics.SUCCESS_OUTCOME),
            (204, rest_metrics.SUCCESS_OUTCOME),
            (301, rest_metrics.CAUSE_3XX),
            (404, rest_metrics.CAUSE_4XX),
            (429, rest_metrics.CAUSE_429),
            (500, rest_metrics.CAUSE_5XX),
        ],
    )
    def test_maps_status_to_stable_taxonomy(self, status: int, expected: str) -> None:
        assert rest_metrics.classify_status(status) == expected


# ── instrument registration ─────────────────────────────────────────────────


class TestInstrumentRegistration:
    def test_registers_exact_metric_names(self) -> None:
        meter, histograms, counters = _storage_meter()
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            rest_metrics._init()
        assert "modulo_rest_request_duration_seconds" in histograms
        assert "modulo_rest_outcome_total" in counters
        assert "modulo_rest_retry_total" in counters
        assert "modulo_rest_ssrf_blocked_total" in counters
        assert "modulo_rest_redaction_events_total" in counters


# ── record helpers ──────────────────────────────────────────────────────────


class TestRecordHelpers:
    def test_record_request_duration_emits_histogram_and_outcome(self) -> None:
        meter, histograms, counters = _storage_meter()
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            rest_metrics.record_request_duration(1.25, host="api.example.com", method="GET", outcome="success")
        histograms["modulo_rest_request_duration_seconds"].record.assert_called_once_with(
            1.25, attributes={"host": "api.example.com", "method": "GET", "outcome": "success"}
        )
        counters["modulo_rest_outcome_total"].add.assert_called_once_with(
            1, attributes={"cause_code": "success", "host": "api.example.com"}
        )

    def test_record_retry_emits_reason(self) -> None:
        meter, _h, counters = _storage_meter()
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            rest_metrics.record_retry("http_429")
        counters["modulo_rest_retry_total"].add.assert_called_once_with(1, attributes={"reason": "http_429"})

    def test_record_ssrf_blocked_emits_host(self) -> None:
        meter, _h, counters = _storage_meter()
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            rest_metrics.record_ssrf_blocked("169.254.169.254")
        counters["modulo_rest_ssrf_blocked_total"].add.assert_called_once_with(
            1, attributes={"host": "169.254.169.254"}
        )

    def test_record_redaction_event_emits_no_attributes(self) -> None:
        meter, _h, counters = _storage_meter()
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            rest_metrics.record_redaction_event()
        counters["modulo_rest_redaction_events_total"].add.assert_called_once_with(1)

    def test_noop_when_no_meter_provider(self) -> None:
        with patch.object(rest_metrics, "_get_meter", return_value=None):
            rest_metrics.record_request_duration(1.0, host="h", method="GET", outcome="success")
            rest_metrics.record_retry("http_429")
            rest_metrics.record_ssrf_blocked("h")
            rest_metrics.record_redaction_event()
        # No handle was created, so nothing was recorded and nothing raised.
        assert rest_metrics._requests_histogram is None


# ── connector metric wiring (end-to-end over a MockTransport) ───────────────


class TestConnectorMetricWiring:
    def test_successful_query_emits_exact_metric_names_and_attributes(self) -> None:
        meter, histograms, counters = _storage_meter()
        _run_query(meter)
        # Histogram sample carries host/method/outcome.
        hist = histograms["modulo_rest_request_duration_seconds"]
        assert hist.record.call_count == 1
        _, kwargs = hist.record.call_args
        assert kwargs["attributes"] == {"host": "api.example.com", "method": "GET", "outcome": "success"}
        # Outcome counter carries the stable cause-code taxonomy.
        outcome = counters["modulo_rest_outcome_total"]
        assert outcome.add.call_count == 1
        _, kwargs = outcome.add.call_args
        assert kwargs["attributes"] == {"cause_code": "success", "host": "api.example.com"}

    def test_failed_query_emits_cause_code(self) -> None:
        meter, _h, counters = _storage_meter()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        c = RestConnector(
            {"base_url": "https://api.example.com", "path": "/items"},
            {"auth_mode": "bearer", "token": "t"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=lambda url: None,
            security_guard=_noop_guard(),
        )
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            import asyncio

            with pytest.raises(RESTConnectError):
                asyncio.run(c.query(ConnectorQuery(resource="default")))

        outcome = counters["modulo_rest_outcome_total"]
        _, kwargs = outcome.add.call_args
        assert kwargs["attributes"] == {"cause_code": rest_metrics.CAUSE_CONNECT, "host": "api.example.com"}

    def test_retry_counts_recorded(self) -> None:
        meter, _h, counters = _storage_meter()
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 2:
                return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})
            return httpx.Response(200, json={"ok": True})

        async def fake_sleep(delay: float) -> None:
            return None

        c = RestConnector(
            {"base_url": "https://api.example.com", "path": "/items"},
            {"auth_mode": "bearer", "token": "t"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=lambda url: None,
            security_guard=_noop_guard(),
            sleep=fake_sleep,
        )
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            import asyncio

            asyncio.run(c.query(ConnectorQuery(resource="default")))

        retry = counters["modulo_rest_retry_total"]
        assert retry.add.call_count == 1
        _, kwargs = retry.add.call_args
        assert kwargs["attributes"] == {"reason": "http_429"}

    def test_failed_then_succeeded_retry_emits_single_terminal_sample(self) -> None:
        """A retried op that succeeds emits ONE success sample — the intermediate
        failed attempts must not leak extra samples into p95/success-rate."""
        meter, histograms, counters = _storage_meter()
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 2:
                return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})
            return httpx.Response(200, json={"ok": True})

        async def fake_sleep(delay: float) -> None:
            return None

        c = RestConnector(
            {"base_url": "https://api.example.com", "path": "/items"},
            {"auth_mode": "bearer", "token": "t"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=lambda url: None,
            security_guard=_noop_guard(),
            sleep=fake_sleep,
        )
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            import asyncio

            asyncio.run(c.query(ConnectorQuery(resource="default")))

        hist = histograms["modulo_rest_request_duration_seconds"]
        assert hist.record.call_count == 1
        _, kwargs = hist.record.call_args
        assert kwargs["attributes"]["outcome"] == rest_metrics.SUCCESS_OUTCOME
        outcome = counters["modulo_rest_outcome_total"]
        assert outcome.add.call_count == 1

    def test_response_too_large_emits_cause_code(self) -> None:
        meter, _h, counters = _storage_meter()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="x" * 2000, headers={"content-type": "text/plain"})

        c = RestConnector(
            {"base_url": "https://api.example.com", "path": "/items", "max_response_size": 50},
            {"auth_mode": "bearer", "token": "t"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=lambda url: None,
            security_guard=_noop_guard(),
        )
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            import asyncio

            with pytest.raises(RESTResponseTooLargeError):
                asyncio.run(c.query(ConnectorQuery(resource="default")))

        outcome = counters["modulo_rest_outcome_total"]
        assert outcome.add.call_count == 1
        _, kwargs = outcome.add.call_args
        assert kwargs["attributes"] == {"cause_code": rest_metrics.CAUSE_TOO_LARGE, "host": "api.example.com"}

    def test_ssrf_blocked_recorded(self) -> None:
        meter, _h, counters = _storage_meter()

        async def reject(url: str) -> None:
            raise ValueError("private/internal network address")

        guard = SecurityGuard(validate_url=reject, filter_strings=lambda values, resource: None)
        c = RestConnector(
            {"base_url": "http://169.254.169.254", "path": "/meta"},
            {"auth_mode": "bearer", "token": "t"},
            security_guard=guard,
        )
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            import asyncio

            with pytest.raises(ValueError):
                asyncio.run(c.query(ConnectorQuery(resource="default")))
        ssrf = counters["modulo_rest_ssrf_blocked_total"]
        assert ssrf.add.call_count == 1
        _, kwargs = ssrf.add.call_args
        assert kwargs["attributes"] == {"host": "169.254.169.254"}

    def test_redaction_event_recorded_on_error(self) -> None:
        meter, _h, counters = _storage_meter()
        secret = "super-secret-token"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed for {secret}")

        c = RestConnector(
            {"base_url": "https://api.example.com", "path": "/items"},
            {"auth_mode": "bearer", "token": secret},
            transport=httpx.MockTransport(handler),
            ssrf_validator=lambda url: None,
            security_guard=_noop_guard(),
        )
        with patch.object(rest_metrics, "_get_meter", return_value=meter):
            import asyncio

            with pytest.raises(RESTConnectError):
                asyncio.run(c.query(ConnectorQuery(resource="default")))
        redaction = counters["modulo_rest_redaction_events_total"]
        assert redaction.add.called  # the secret was redacted, emitting an event


# ── rollback threshold evaluator ────────────────────────────────────────────


class TestRestRollback:
    def test_below_min_samples_never_triggers(self) -> None:
        assert (
            evaluate_rest_rollback(url="https://a.example", total_requests=10, error_requests=10, unknown_requests=0)
            is None
        )

    def test_zero_requests_never_triggers(self) -> None:
        signal = evaluate_rest_rollback(url="https://a.example", total_requests=0, error_requests=0, unknown_requests=0)
        assert signal is None

    def test_error_rate_crossed_triggers_signal(self) -> None:
        signal = evaluate_rest_rollback(
            url="https://a.example", total_requests=100, error_requests=20, unknown_requests=0
        )
        assert isinstance(signal, RestRollbackSignal)
        assert signal.error_rate == pytest.approx(0.2)
        assert signal.action == "operator_review"
        assert signal.url == "https://a.example"

    def test_unknown_rate_crossed_triggers_signal(self) -> None:
        signal = evaluate_rest_rollback(
            url="https://a.example", total_requests=100, error_requests=0, unknown_requests=10
        )
        assert isinstance(signal, RestRollbackSignal)
        assert signal.unknown_rate == pytest.approx(0.1)

    def test_healthy_rates_do_not_trigger(self) -> None:
        assert (
            evaluate_rest_rollback(url="https://a.example", total_requests=100, error_requests=1, unknown_requests=1)
            is None
        )

    def test_error_and_unknown_buckets_are_disjoint(self) -> None:
        """A request counted as UNKNOWN must be judged against the unknown
        threshold independently, not double-counted as an error."""
        signal = evaluate_rest_rollback(
            url="https://a.example", total_requests=100, error_requests=6, unknown_requests=3
        )
        assert isinstance(signal, RestRollbackSignal)
        assert signal.error_rate == pytest.approx(0.06)
        assert signal.unknown_rate == pytest.approx(0.03)

    def test_unknown_like_classification(self) -> None:
        # A transport timeout is a deterministic failure, classified as an error
        # (never double-counted as UNKNOWN) — so it is NOT unknown-like.
        assert is_unknown_like("timeout") is False
        assert is_unknown_like("unknown") is True
        assert is_unknown_like("http_429") is False
        assert is_unknown_like("success") is False

    def test_unknown_outcome_not_double_counted_in_error_bucket(self) -> None:
        """``unknown`` must never be classified as an error. A producer that
        builds error_requests from ``_ERROR_CAUSE_CODES`` and unknown_requests
        from ``is_unknown_like`` must not count the same cause in both — the
        two cause sets are genuinely disjoint."""
        error_causes = frozenset(rest_metrics._ERROR_CAUSE_CODES)
        unknown_like = frozenset(rest_rollback._DEFAULT_UNKNOWN_LIKE)

        # The two buckets share no cause value at all.
        assert error_causes.isdisjoint(unknown_like)
        assert rest_metrics.CAUSE_UNKNOWN not in rest_metrics._ERROR_CAUSE_CODES
        assert is_unknown_like(rest_metrics.CAUSE_UNKNOWN) is True

        # Every error cause is deterministic; none is UNKNOWN-like.
        assert rest_metrics.CAUSE_TIMEOUT in error_causes
        assert is_unknown_like(rest_metrics.CAUSE_TIMEOUT) is False

        # A producer bucketising a batch never doubles an outcome: an
        # ``unknown`` outcome is counted in the UNKNOWN bucket only.
        batch = ["success", rest_metrics.CAUSE_429, rest_metrics.CAUSE_UNKNOWN]
        counted_errors = [c for c in batch if c in error_causes]
        counted_unknown = [c for c in batch if is_unknown_like(c)]
        assert counted_errors == [rest_metrics.CAUSE_429]
        assert counted_unknown == [rest_metrics.CAUSE_UNKNOWN]

    def test_logs_structured_warning_on_trigger(self, caplog: pytest.LogCaptureFixture) -> None:
        evaluate_rest_rollback(url="https://a.example", total_requests=100, error_requests=50, unknown_requests=0)
        assert "rest_rollback.threshold_triggered" in caplog.text
