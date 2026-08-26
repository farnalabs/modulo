"""Unit tests for REST connector observability (FAR-413) + shared rate-limit budget (FAR-439).

FAR-413 observability: covers ``modulo.connectors.rest.rest_metrics`` (OTel
instrument registration + record helpers), the connector's metric wiring (that a
real request emits the exact metric names/attributes), and
``modulo.connectors.rest.rest_rollback`` (threshold evaluator + cause-code
classification).

FAR-439 shared budget: covers the SHARED Redis-backed per-destination rate
limiter so multiple workers enforce ONE budget per destination, keyed per-tenant,
with a connector-local bucket fallback and no lost-token race. Tests the
primitives directly (:class:`RedisTokenBucket` / :class:`PerDestinationRateLimiter`)
and the RestConnector composition. No real Redis is used -- a fake Redis client
replicates the Lua script's atomic semantics in-process so multiple limiter
instances sharing one store simulate a multi-worker fleet.

These are pure unit tests: no network, no DB, no real meter provider. The OTel
meter is injected via ``sys.modules``/``_get_meter`` the same way
``test_error_metrics.py`` stubs OTel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from redis.exceptions import RedisError

import modulo.connectors.rest.rest_metrics as rest_metrics
import modulo.connectors.rest.rest_rollback as rest_rollback
from modulo.connectors._rate_bucket import PerDestinationRateLimiter, RedisTokenBucket, SharedBudgetUnavailableError
from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.rest import (
    RESTConnectError,
    RestConnector,
    RESTFanOutFailureError,
    RESTResponseTooLargeError,
    SecurityGuard,
)
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


class _FakeRedis:
    """In-process stand-in for ``redis.asyncio.Redis``.

    ``register_script`` returns an async callable that reproduces the token-bucket
    Lua semantics atomically under a lock. ``store`` is shared by every client, so
    multiple limiter instances pointing at the same store simulate a fleet of
    workers hitting one Redis.

    ``clock`` mirrors the Lua's server-side ``redis.call('TIME')`` refill base;
    pass a controllable clock (or a fixed ``lambda``) to make refill determinism
    testable. ``pexpire_ttl`` records the PEXPIRE TTL the script was told to use
    (ARGV[4]), so a test can assert it receives the computed reclaim TTL rather
    than a stale worker ``now`` value.
    """

    def __init__(self, store: dict[str, dict[str, float]] | None = None, clock: Any = time.time) -> None:
        self._store: dict[str, dict[str, float]] = store if store is not None else {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self.pexpire_ttl: float | None = None

    def register_script(self, script: str) -> Any:
        of_self = self

        async def run(keys: list[str], args: list[Any]) -> int:
            key = keys[0]
            rate = float(args[0])
            burst = float(args[1])
            cost = float(args[2])
            ttl_ms = float(args[3])
            of_self.pexpire_ttl = ttl_ms
            now = of_self._clock()
            async with of_self._lock:
                st = of_self._store.get(key)
                if st is None:
                    st = {"tokens": burst, "ts": now}
                elapsed = max(0.0, now - st["ts"])
                st["tokens"] = min(burst, st["tokens"] + elapsed * rate)
                st["ts"] = now
                if st["tokens"] >= cost:
                    st["tokens"] -= cost
                    of_self._store[key] = st
                    return 1
                of_self._store[key] = st
                return 0

        return run


class _BrokenRedis:
    """A Redis client whose script execution always fails (unavailable)."""

    def register_script(self, script: str) -> Any:
        async def run(keys: list[str], args: list[Any]) -> int:
            raise RedisError("Redis unavailable")

        return run


def _make_connector(
    config: dict[str, Any] | None,
    creds: dict[str, Any] | None,
    *,
    redis_client: Any = None,
    tenant_id: str | None = None,
) -> RestConnector:
    return RestConnector(
        config,
        creds,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
        redis_client=redis_client,
        tenant_id=tenant_id,
    )


# ── RedisTokenBucket: shared budget + no lost-token race ─────────────────────


async def test_redis_token_bucket_no_lost_token_race_under_concurrency() -> None:
    """Concurrent consumes never over-spend the shared budget (atomic in Redis).

    With a fixed ``now`` (no refill), exactly ``burst`` concurrent consumes
    succeed — never more — proving the token check-and-decrement is not racy.
    """
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store, clock=lambda: 1000.0)
    bucket = RedisTokenBucket(redis, rate=0.0001, burst=5, key_prefix="rl:")

    grant_count = sum(await asyncio.gather(*[bucket.consume("k", tokens=1.0) for _ in range(50)]))
    assert grant_count == 5
    assert store["rl:k"]["tokens"] >= 0


async def test_redis_token_bucket_refills_over_wall_clock() -> None:
    """A shared bucket refills continuously, so a later call re-acquires.

    The refill base is driven by the fake's clock (mirroring the Lua's server-side
    ``redis.call('TIME')``) rather than an injected worker ``now``. It also asserts
    that PEXPIRE receives the computed reclaim TTL — NOT a stale worker ``now``
    (~1.75e9), which would give every key a ~20-day expiry instead of ~60s.
    """
    store: dict[str, dict[str, float]] = {}
    now = [1000.0]
    redis = _FakeRedis(store, clock=lambda: now[0])
    bucket = RedisTokenBucket(redis, rate=2.0, burst=1, key_prefix="rl:")

    assert await bucket.consume("k", tokens=1.0) is True
    # Immediately after spend, no refill -> denied.
    assert await bucket.consume("k", tokens=1.0) is False
    # One second later the 2/s rate refilled a token.
    now[0] += 1.0
    assert await bucket.consume("k", tokens=1.0) is True
    # ttl = max(60, (burst/rate)*2)*1000 = max(60, (1/2)*2)*1000 = 60000 ms.
    assert redis.pexpire_ttl == 60000


# ── PerDestinationRateLimiter: shared budget across simulated workers ─────────


async def test_shared_budget_enforced_across_simulated_workers() -> None:
    """Two limiter instances (two workers) share ONE Redis budget."""
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)
    worker_a = PerDestinationRateLimiter(rate=0.0001, burst=3, redis_client=redis, tenant_id="org-1")
    worker_b = PerDestinationRateLimiter(rate=0.0001, burst=3, redis_client=redis, tenant_id="org-1")

    results = []
    for _ in range(5):
        results.append(await worker_a.consume("api.example.com/x"))
        results.append(await worker_b.consume("api.example.com/x"))

    # 3-token budget shared by both workers -> 3 grants, 7 denies (no refill).
    assert results.count(True) == 3
    assert not worker_a.buckets  # never fell back to local


async def test_per_tenant_weighting_separates_budgets() -> None:
    """Different tenants get independent shared budgets for the same destination."""
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)
    tenant_a = PerDestinationRateLimiter(rate=0.0001, burst=2, redis_client=redis, tenant_id="org-A")
    tenant_b = PerDestinationRateLimiter(rate=0.0001, burst=2, redis_client=redis, tenant_id="org-B")

    assert await tenant_a.consume("dest") is True
    assert await tenant_a.consume("dest") is True
    assert await tenant_a.consume("dest") is False  # org-A budget exhausted

    # org-B still has its own full budget.
    assert await tenant_b.consume("dest") is True
    assert await tenant_b.consume("dest") is True
    assert await tenant_b.consume("dest") is False

    assert tenant_a.key("dest") != tenant_b.key("dest")


async def test_shared_limiter_without_tenant_raises() -> None:
    """A shared (Redis) limiter without a tenant_id FAILS LOUDLY (FAR-439).

    Silently coercing a missing tenant to "default" would bucket every
    organisation into ONE cross-tenant Redis budget — a cross-org leak. A
    ``redis_client`` wired without a ``tenant_id`` must raise at construction
    rather than share a budget; the connector-local (no-Redis) path is exempt
    because a per-process bucket is inherently per-tenant.
    """
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)
    with pytest.raises(ValueError, match="requires a non-empty tenant_id"):
        PerDestinationRateLimiter(rate=1.0, burst=2, redis_client=redis, tenant_id=None)

    # A local (non-shared) limiter still accepts a missing tenant — it is
    # per-process and cannot leak across orgs.
    local = PerDestinationRateLimiter(rate=1.0, burst=2, redis_client=None, tenant_id=None)
    assert await local.consume("dest") is True


async def test_redis_outage_fails_closed_when_configured(caplog: Any) -> None:
    """A Redis outage when configured FAILS CLOSED — never mints a per-worker budget.

    Falling back to each worker's own full-burst bucket would multiply the
    effective cap by the worker count (N x burst), defeating the single-budget
    guarantee. With a ``redis_client`` configured the limiter must refuse to
    charge rather than fail open.
    """
    with caplog.at_level(logging.WARNING, logger="modulo.connectors._rate_bucket"):
        limiter = PerDestinationRateLimiter(rate=1.0, burst=2, redis_client=_BrokenRedis(), tenant_id="org-1")

        with pytest.raises(SharedBudgetUnavailableError):
            await limiter.consume("dest")

    assert not limiter.buckets  # never created a per-process fallback bucket
    assert "rest.rate_limit.degraded" in [r.message for r in caplog.records]
    assert "rest.rate_limit.redis_error" in [r.message for r in caplog.records]


async def test_saturation_signal_recorded_on_deny(caplog: Any) -> None:
    """A denied consume records saturation (counter + structured alert log)."""
    store: dict[str, dict[str, float]] = {}
    limiter = PerDestinationRateLimiter(rate=1.0, burst=1, redis_client=_FakeRedis(store), tenant_id="org-1")

    assert await limiter.consume("dest") is True
    with caplog.at_level(logging.WARNING, logger="modulo.connectors._rate_bucket"):
        assert await limiter.consume("dest") is False

    assert limiter.saturation_count == 1
    assert limiter.saturations["dest"] == 1
    alerts = [r for r in caplog.records if r.message == "rest.rate_limit.saturated"]
    assert alerts
    assert alerts[0].destination == "dest"
    assert alerts[0].tenant == "org-1"


async def test_saturation_warning_throttled_on_deny_after_grant(caplog: Any) -> None:
    """A repeated deny does NOT re-emit the saturated WARNING (counters still rise).

    The WARNING fires only on the deny-after-grant transition; back-to-back
    denies keep incrementing the counters (the metric) without flooding an alert
    per deny.
    """
    store: dict[str, dict[str, float]] = {}
    limiter = PerDestinationRateLimiter(rate=0.0001, burst=1, redis_client=_FakeRedis(store), tenant_id="org-1")

    assert await limiter.consume("dest") is True  # grant — resets the transition state
    with caplog.at_level(logging.WARNING, logger="modulo.connectors._rate_bucket"):
        assert await limiter.consume("dest") is False  # deny — transition, WARNING
        assert await limiter.consume("dest") is False  # deny — still saturated, no WARNING

    assert limiter.saturation_count == 2
    assert limiter.saturations["dest"] == 2
    alerts = [r for r in caplog.records if r.message == "rest.rate_limit.saturated"]
    assert len(alerts) == 1


# ── RestConnector composition ─────────────────────────────────────────────────


def test_rest_connector_uses_shared_redis_budget_per_destination() -> None:
    """The connector enforces one shared Redis budget across its fan-out."""
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items", "per_item_timeout": 0.001},
            "rate_limit": {"requests_per_second": 0.001, "burst": 2},
        },
        {"auth_mode": "bearer", "token": "t"},
        redis_client=redis,
        tenant_id="org-1",
    )

    with pytest.raises(RESTFanOutFailureError, match="rate-limit wait exceeded") as exc:
        asyncio.run(
            c.write(ConnectorPayload(resource="default", data={"items": [{"name": f"n{i}"} for i in range(3)]}))
        )
    assert "rate-limit wait exceeded" in exc.value.failed_error
    # Budget keyed per <tenant, host+path>.
    assert any("org-1" in k and "https://api.example.com/users" in k for k in store)
    assert len(store) == 1


def test_templated_path_and_query_share_single_canonical_bucket() -> None:
    """A templated path + query strings map to ONE canonical budget key (FAR-439).

    The destination key is derived from the STATIC path template, so different
    rendered IDs and query strings never fragment the shared budget.
    """
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users/{{ user_id }}",
            "params": {"page": "{{ page }}", "q": "{{ q }}"},
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items", "per_item_timeout": 0.001},
            "rate_limit": {"requests_per_second": 1000.0, "burst": 100},
        },
        {"auth_mode": "bearer", "token": "t"},
        redis_client=redis,
        tenant_id="org-1",
    )

    result = asyncio.run(
        c.write(
            ConnectorPayload(
                resource="default",
                data={
                    "items": [
                        {"user_id": 1, "page": 1, "q": "a", "name": "n1"},
                        {"user_id": 2, "page": 2, "q": "b", "name": "n2"},
                    ]
                },
            )
        )
    )
    assert result["success_count"] == 2
    assert len(store) == 1
    (key,) = store.keys()
    # Canonical key = base_url + the STATIC template (rendered ids/query dropped),
    # prefixed by the shared-bucket key prefix.
    assert key == "rest_rate_limit:org-1:https://api.example.com/users/{{ user_id }}"
    assert "page" not in key
    assert "q=" not in key


def test_rest_connector_local_bucket_when_no_redis() -> None:
    """Without a redis_client the connector stays on the per-process bucket."""
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items", "per_item_timeout": 0.001},
            "rate_limit": {"requests_per_second": 0.001, "burst": 2},
        },
        {"auth_mode": "bearer", "token": "t"},
    )

    with pytest.raises(RESTFanOutFailureError, match="rate-limit wait exceeded"):
        asyncio.run(
            c.write(ConnectorPayload(resource="default", data={"items": [{"name": f"n{i}"} for i in range(3)]}))
        )
    # Local store carried the destination bucket.
    assert len(c._rate_buckets) == 1


def test_rest_connector_redis_failure_fails_closed() -> None:
    """A broken Redis client makes the connector fail closed (no per-worker budget)."""
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items", "per_item_timeout": 0.001},
            "rate_limit": {"requests_per_second": 0.001, "burst": 2},
        },
        {"auth_mode": "bearer", "token": "t"},
        redis_client=_BrokenRedis(),
        tenant_id="org-1",
    )

    with pytest.raises(RESTFanOutFailureError, match="rate-limit budget unavailable"):
        asyncio.run(
            c.write(ConnectorPayload(resource="default", data={"items": [{"name": f"n{i}"} for i in range(3)]}))
        )
    # With Redis configured, the connector must NOT fall back to a per-process bucket.
    assert not c._rate_buckets
