"""Unit tests for the shared analytics service (FAR-102, ADR 020).

Covers the in-memory per-org rate limiter (window counting, per-org budget,
org-count cap + stale-org pruning) and bounds normalisation / hour-cap
validation — the pure logic shared by the REST route and the MCP tool. No DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.analytics.service as svc
from modulo.core.analytics.builder import AnalyticsGroupBy
from modulo.core.analytics.service import (
    AnalyticsValidationError,
    _check_hour_cap,
    _normalise_bounds,
    _rate_limited,
)


class TestRateLimiter:
    def _module(self):
        return svc

    def test_allows_up_to_budget_then_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc._rate_hits.clear()
        monkeypatch.setattr(svc, "_RATE_LIMIT_MAX_PER_ORG", 3)
        org = str(uuid.uuid4())
        try:
            assert [_rate_limited(org) for _ in range(3)] == [False, False, False]
            assert _rate_limited(org) is True, "the 4th hit within the window must be blocked"
        finally:
            svc._rate_hits.clear()

    def test_per_org_budgets_are_independent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc._rate_hits.clear()
        monkeypatch.setattr(svc, "_RATE_LIMIT_MAX_PER_ORG", 1)
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())
        try:
            assert _rate_limited(org_a) is False
            assert _rate_limited(org_a) is True, "org A's budget is exhausted"
            assert _rate_limited(org_b) is False, "org B must have its own budget"
        finally:
            svc._rate_hits.clear()

    def test_hits_expire_after_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc._rate_hits.clear()
        monkeypatch.setattr(svc, "_RATE_LIMIT_MAX_PER_ORG", 2)
        clock = [1000.0]
        monkeypatch.setattr(svc.time, "monotonic", lambda: clock[0])
        org = str(uuid.uuid4())
        try:
            assert _rate_limited(org) is False
            assert _rate_limited(org) is False
            assert _rate_limited(org) is True, "budget exhausted within the window"
            clock[0] += svc._RATE_LIMIT_WINDOW_SECONDS + 1
            assert _rate_limited(org) is False, "hits older than the window must not count"
        finally:
            svc._rate_hits.clear()

    def test_idle_orgs_are_pruned_and_cap_evicts_oldest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc._rate_hits.clear()
        monkeypatch.setattr(svc, "_RATE_LIMIT_MAX_ORGS", 2)
        clock = [1000.0]
        monkeypatch.setattr(svc.time, "monotonic", lambda: clock[0])
        org_new = str(uuid.uuid4())
        org_old = str(uuid.uuid4())
        try:
            # A stale org (hit long ago) plus a fresh org fill the cap.
            svc._rate_hits[org_old] = [clock[0] - svc._RATE_LIMIT_WINDOW_SECONDS - 1]
            svc._rate_hits[org_new] = [clock[0]]
            assert len(svc._rate_hits) == 2
            # A third org hit triggers pruning of the idle org + eviction.
            _rate_limited(org_new)
            assert org_old not in svc._rate_hits, "idle orgs must be pruned on the next hit"
            assert len(svc._rate_hits) <= 2, "the tracked-org cap must hold"
        finally:
            svc._rate_hits.clear()


class TestNormaliseBounds:
    def test_bare_dates_expand_to_whole_days(self) -> None:
        frm, to = _normalise_bounds(date(2026, 8, 1), date(2026, 8, 2))
        assert frm.hour == 0, "date_from must expand to midnight"
        assert frm.minute == 0, "date_from must expand to midnight"
        assert to.hour == 23, "date_to must expand to end-of-day"
        assert to.minute == 59, "date_to must expand to end-of-day"
        assert frm.date() == date(2026, 8, 1)
        assert to.date() == date(2026, 8, 2)

    def test_defaults_to_today_and_364_day_lookback(self) -> None:
        frm, to = _normalise_bounds(None, None)
        # _normalise_bounds defaults to UTC today (datetime.now(UTC).date()) — the
        # assertion must use UTC, not naive local date.today(), or it flakes across
        # a local-midnight boundary (UTC+1).
        assert to.date() == datetime.now(UTC).date()
        assert (to.date() - frm.date()).days == 364, "the default window is exactly 365 days inclusive"

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError, match="date_from must be <= date_to"):
            _normalise_bounds(date(2026, 8, 5), date(2026, 8, 1))

    def test_over_365_day_range_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError, match="365 days"):
            _normalise_bounds(date.today() - timedelta(days=500), date.today())

    def test_non_utc_offset_aware_bounds_normalised(self) -> None:
        frm = datetime(2026, 8, 1, 21, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        to = date(2026, 8, 3)
        normalised_frm, _ = _normalise_bounds(frm, to)
        assert normalised_frm.hour == 16, "+05:00 21:00 must convert to 16:00 UTC"


class TestCheckHourCap:
    def test_under_cap_passes(self) -> None:
        assert _check_hour_cap(AnalyticsGroupBy.HOUR, date(2026, 8, 1), date(2026, 8, 14)) is None

    def test_over_cap_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError, match="hour granularity"):
            _check_hour_cap(AnalyticsGroupBy.HOUR, date(2026, 8, 1), date(2026, 8, 16))

    def test_day_grouping_ignores_span(self) -> None:
        assert _check_hour_cap(AnalyticsGroupBy.DAY, date(2026, 1, 1), date(2026, 8, 1)) is None


# ── pool_reference resolution (FAR-134) ──────────────────────────────


class _FakePoolSession:
    """Async-session shaped fake: ``async with`` + ``begin()`` + ``execute()``.

    ``execute`` returns a result whose ``scalar_one_or_none`` yields
    *pipeline_max_concurrent* (the single-pipeline reference path). The org
    path bypasses ``execute`` entirely and calls the org-limit reader.
    """

    def __init__(self, pipeline_max_concurrent: int | None) -> None:
        self._pipeline_max_concurrent = pipeline_max_concurrent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    def begin(self):
        return self

    async def execute(self, stmt, params=None):
        result = AsyncMock()
        result.scalar_one_or_none = MagicMock(return_value=self._pipeline_max_concurrent)
        return result


class TestResolvePoolReference:
    """``_resolve_pool_reference``: no filter → org run_concurrency_limit; a
    single pipeline filter → that pipeline's max_concurrent_runs (FAR-134)."""

    def _settings(self):
        return MagicMock()

    def _call(self, factory, *, pipeline_ids=(), account_id=None, org_role=None):
        org = uuid.uuid4()
        return svc._resolve_pool_reference(
            factory,
            self._settings(),
            org_id=org,
            account_id=account_id,
            org_role=org_role,
            pipeline_ids=pipeline_ids,
        )

    async def test_no_pipeline_filter_reads_org_run_concurrency_limit(self) -> None:
        session = _FakePoolSession(pipeline_max_concurrent=None)
        factory = MagicMock(return_value=session)
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock) as mock_user,
            patch.object(svc, "get_org_run_concurrency_limit", new=AsyncMock(return_value=20)) as mock_org,
        ):
            value = await self._call(factory, account_id=uuid.uuid4(), org_role="admin")
        assert value == 20, "no pipeline filter must use the org's run_concurrency_limit"
        mock_org.assert_awaited_once()
        assert mock_org.await_args.args[1] is not None, "the org id must be passed to the org-limit reader"
        mock_rls.assert_awaited_once()
        mock_user.assert_awaited_once()

    async def test_single_pipeline_filter_reads_pipeline_max_concurrent_runs(self) -> None:
        session = _FakePoolSession(pipeline_max_concurrent=7)
        factory = MagicMock(return_value=session)
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock),
            patch.object(svc, "get_org_run_concurrency_limit", new=AsyncMock(return_value=20)) as mock_org,
        ):
            value = await self._call(factory, pipeline_ids=(uuid.uuid4(),))
        assert value == 7, "a single pipeline filter must use that pipeline's max_concurrent_runs"
        mock_org.assert_not_awaited(), "the org limit must NOT be read when a single pipeline is filtered"

    async def test_single_pipeline_missing_row_returns_none(self) -> None:
        session = _FakePoolSession(pipeline_max_concurrent=None)
        factory = MagicMock(return_value=session)
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock),
            patch.object(svc, "get_org_run_concurrency_limit", new=AsyncMock()) as mock_org,
        ):
            value = await self._call(factory, pipeline_ids=(uuid.uuid4(),))
        assert value is None, "a missing pipeline row must degrade to None, not raise"
        mock_org.assert_not_awaited()

    async def test_org_limit_reader_failure_degrades_to_none(self) -> None:
        session = _FakePoolSession(pipeline_max_concurrent=None)
        factory = MagicMock(return_value=session)
        from sqlalchemy.exc import SQLAlchemyError

        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock),
            patch.object(
                svc,
                "get_org_run_concurrency_limit",
                new=AsyncMock(side_effect=SQLAlchemyError("db down")),
            ),
        ):
            value = await self._call(factory)
        assert value is None, "a reference-read failure must degrade to None, never raise"

    async def test_pipeline_query_failure_degrades_to_none(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        async def _boom(stmt, params=None) -> None:
            raise SQLAlchemyError("db down")

        session = _FakePoolSession(pipeline_max_concurrent=None)
        session.execute = _boom  # type: ignore[method-assign]
        factory = MagicMock(return_value=session)
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock),
            patch.object(svc, "get_org_run_concurrency_limit", new=AsyncMock()) as mock_org,
        ):
            value = await self._call(factory, pipeline_ids=(uuid.uuid4(),))
        assert value is None, "a pipeline-query failure must degrade to None, never raise"
        mock_org.assert_not_awaited()


class TestConcurrencyRawRowCap:
    """The concurrency raw-row cap degrades to a typed error, never truncation.

    ``build_concurrency_query`` caps the scan at cap+1 rows; the service detects
    ``len(rows) > CONCURRENCY_MAX_RAW_ROWS`` and raises ``AnalyticsValidationError``
    (REST → 422, MCP → invalid_params). Exactly-cap rows proceed normally.
    """

    def _params(self):
        return svc.AnalyticsParams()

    async def test_over_cap_raises_validation_error(self) -> None:
        rows = [object() for _ in range(svc.CONCURRENCY_MAX_RAW_ROWS + 1)]
        with (
            patch.object(svc, "_execute_with_guards", new=AsyncMock(return_value=rows)) as mock_exec,
            patch.object(svc, "_resolve_pool_reference", new=AsyncMock(return_value=None)) as mock_pool,
            pytest.raises(AnalyticsValidationError, match="raw cap"),
        ):
            await svc.run_concurrency_query(
                org_id=uuid.uuid4(),
                params=self._params(),
                factory=MagicMock(),
                settings=MagicMock(),
            )
        mock_exec.assert_awaited_once()
        mock_pool.assert_not_awaited(), "overflow must reject before the pool reference resolves"

    async def test_at_cap_proceeds_without_raising(self) -> None:
        rows = [object() for _ in range(svc.CONCURRENCY_MAX_RAW_ROWS)]
        buckets = [
            {
                "date": "2026-08-06",
                "max_active": 0,
                "avg_active": 0.0,
                "max_queued": 0,
                "avg_queued": 0.0,
            }
        ]
        with (
            patch.object(svc, "_execute_with_guards", new=AsyncMock(return_value=rows)),
            patch.object(svc, "bucket_concurrency_rows", return_value=buckets) as mock_bucket,
            patch.object(svc, "_resolve_pool_reference", new=AsyncMock(return_value=20)) as mock_pool,
        ):
            result = await svc.run_concurrency_query(
                org_id=uuid.uuid4(),
                params=self._params(),
                factory=MagicMock(),
                settings=MagicMock(),
            )
        mock_bucket.assert_called_once()
        mock_pool.assert_awaited_once()
        assert result["pool_reference"] == 20
        assert result["buckets"][0]["key"] is None
        assert result["buckets"][0]["pool_reference"] == 20, "each bucket mirrors the top-level reference"
