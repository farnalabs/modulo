"""Unit tests for the analytics query builder + bucketing (ADR 020).

Covers: allowlist rejection (injection strings can never construct an enum),
compiled-SQL assertions per dialect (org predicate present, bound params,
placeholder count, no string interpolation, no LIMIT before bucketing), and
the Python bucketing/zero-fill logic.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from modulo.core.analytics.builder import (
    AnalyticsDimension,
    AnalyticsGroupBy,
    AnalyticsQuery,
    AnalyticsStatus,
    AnalyticsTriggerType,
    bucket_rows,
    build_facts_query,
)

_ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _query(**overrides) -> AnalyticsQuery:
    defaults = {
        "org_id": _ORG,
        "group_by": AnalyticsGroupBy.DAY,
        "date_from": date(2026, 8, 1),
        "date_to": date(2026, 8, 31),
    }
    defaults.update(overrides)
    return AnalyticsQuery(**defaults)


def _row(
    day: date,
    count: int = 1,
    complete: int | None = None,
    total_cost_usd: float | None = None,
    total_tokens: int | None = None,
    avg_duration_ms: float | None = None,
    **extra,
) -> SimpleNamespace:
    return SimpleNamespace(
        run_date=day,
        count=count,
        complete_count=count if complete is None else complete,
        total_cost_usd=total_cost_usd,
        total_tokens=total_tokens,
        avg_duration_ms=avg_duration_ms,
        **extra,
    )


class TestAllowlistRejection:
    def test_group_by_rejects_sql_injection_string(self) -> None:
        with pytest.raises(ValueError):
            AnalyticsGroupBy("status; DROP TABLE runs")

    def test_dimension_rejects_sql_injection_string(self) -> None:
        with pytest.raises(ValueError):
            AnalyticsDimension("status; DROP TABLE runs")

    def test_trigger_type_rejects_sql_injection_string(self) -> None:
        with pytest.raises(ValueError):
            AnalyticsTriggerType("manual OR 1=1")

    def test_status_rejects_sql_injection_string(self) -> None:
        with pytest.raises(ValueError):
            AnalyticsStatus("complete; DROP TABLE runs")

    def test_non_allowlisted_dimension_literal_raises(self) -> None:
        # A dimension value that is NOT an AnalyticsDimension member can never
        # reach the allowlist dict lookup; a raw string is rejected.
        from modulo.core.analytics.builder import _DIMENSION_COLUMNS

        with pytest.raises(KeyError):
            _DIMENSION_COLUMNS["trigger_type; DROP TABLE runs"]  # type: ignore[index]


class TestCompiledSql:
    def _compile(self, query: AnalyticsQuery, dialect):
        stmt, _params = build_facts_query(query)
        return str(stmt.compile(dialect=dialect))

    def test_org_predicate_present_on_both_dialects(self) -> None:
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            sql = self._compile(_query(), dialect)
            assert "organisation_id" in sql, "every analytics statement must carry the org predicate"

    def test_org_value_never_interpolated_into_sql(self) -> None:
        # The org id must be a bound param, never an f-string literal.
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            sql = self._compile(_query(), dialect)
            assert str(_ORG) not in sql, "the org uuid must be a bound param, not interpolated"

    def test_no_limit_before_bucketing(self) -> None:
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            sql = self._compile(_query(limit=5), dialect)
            assert "LIMIT" not in sql.upper(), "limit must be applied post-bucketing, never in SQL"

    def test_bound_params_match_placeholder_count_postgres(self) -> None:
        stmt, params = build_facts_query(
            _query(
                trigger_type=AnalyticsTriggerType.CRON,
                status=AnalyticsStatus.FAILED,
                pipeline_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            )
        )
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        placeholder_count = len(re.findall(r"%\(\w+\)s", sql))
        # Every explicit param is bound (the aggregate FILTER adds an internal
        # status binding of its own, so the count is AT LEAST len(params)).
        assert placeholder_count >= len(params)
        assert "failed" not in sql.lower(), "filter values must be bound, never interpolated"
        assert "cron" not in sql.lower(), "filter values must be bound, never interpolated"

    def test_bound_params_match_placeholder_count_sqlite(self) -> None:
        stmt, _params = build_facts_query(_query(folder_id=uuid.UUID("33333333-3333-4333-8333-333333333333")))
        compiled = stmt.compile(dialect=sqlite.dialect())
        sql = str(compiled)
        placeholder_count = len(re.findall(r"\?", sql))
        assert placeholder_count >= 4

    def test_filters_are_allowlisted_bound_scalars(self) -> None:
        # The filter columns referenced in the SQL are the allowlisted literal
        # keys (enum members), rendered as bound comparisons — never raw text.
        stmt, _ = build_facts_query(_query(status=AnalyticsStatus.COMPLETE, trigger_type=AnalyticsTriggerType.WEBHOOK))
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "status" in sql and "trigger_type" in sql
        assert "'complete'" not in sql and "'webhook'" not in sql, "filter values must be bound, never literal"


class TestBucketing:
    def test_zero_fill_day_grid(self) -> None:
        day_from = date(2026, 8, 1)
        day_to = date(2026, 8, 3)
        out = bucket_rows(
            [],
            group_by=AnalyticsGroupBy.DAY,
            dimension=None,
            date_from=day_from,
            date_to=day_to,
        )
        assert [b["date"] for b in out] == ["2026-08-01", "2026-08-02", "2026-08-03"]
        assert all(b["count"] == 0 for b in out)
        assert all(b["total_cost_usd"] is None and b["success_rate"] is None for b in out)

    def test_week_bucketing_iso_monday(self) -> None:
        # 2026-08-03 is a Monday; 2026-08-05 (Wed) and 2026-08-06 (Thu) share it.
        rows = [
            _row(date(2026, 8, 3), count=2, complete=2, total_cost_usd=10.0, total_tokens=50),
            _row(date(2026, 8, 6), count=1, complete=1, total_cost_usd=5.0, total_tokens=25),
            _row(date(2026, 8, 10), count=1, complete=0),  # Monday of the next ISO week
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.WEEK,
            dimension=None,
            date_from=date(2026, 8, 3),
            date_to=date(2026, 8, 16),
        )
        weeks = [b["date"] for b in out]
        assert weeks == ["2026-08-03", "2026-08-10"]
        assert out[0]["count"] == 3, "Mon..Sun of the same ISO week must collapse into one bucket"
        assert out[0]["total_cost_usd"] == 15.0
        assert out[0]["total_tokens"] == 75
        assert out[1]["count"] == 1

    def test_success_rate(self) -> None:
        rows = [
            _row(date(2026, 8, 5), count=2, complete=1),
            _row(date(2026, 8, 6), count=1, complete=1),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=None,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 6),
        )
        assert out[0]["success_rate"] == 0.5
        assert out[1]["success_rate"] == 1.0

    def test_limit_applied_post_bucketing(self) -> None:
        rows = [_row(date(2026, 8, 1) + timedelta(days=i)) for i in range(10)]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=None,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 10),
            limit=3,
        )
        assert len(out) == 3, "limit must truncate AFTER bucketing"
        assert out[-1]["date"] == "2026-08-10", "the most recent buckets win"

    def test_dimension_bucket_keys(self) -> None:
        rows = [
            _row(date(2026, 8, 5), key_label="manual", trigger_type="manual"),
            _row(date(2026, 8, 5), key_label="cron", trigger_type="cron"),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=AnalyticsDimension.TRIGGER_TYPE,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 5),
        )
        assert {b["key"] for b in out} == {"cron", "manual"}
        assert sum(b["count"] for b in out) == 2
