"""Unit tests for the shared analytics service (FAR-102, ADR 020).

Covers the in-memory per-org rate limiter (window counting, per-org budget,
org-count cap + stale-org pruning) and bounds normalisation / hour-cap
validation — the pure logic shared by the REST route and the MCP tool. No DB.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

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
        assert to.date() == date.today()
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
        _check_hour_cap(AnalyticsGroupBy.HOUR, date(2026, 8, 1), date(2026, 8, 14))

    def test_over_cap_rejected(self) -> None:
        with pytest.raises(AnalyticsValidationError, match="hour granularity"):
            _check_hour_cap(AnalyticsGroupBy.HOUR, date(2026, 8, 1), date(2026, 8, 16))

    def test_day_grouping_ignores_span(self) -> None:
        _check_hour_cap(AnalyticsGroupBy.DAY, date(2026, 1, 1), date(2026, 8, 1))
