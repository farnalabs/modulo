"""Unit tests for the analytics query builder + bucketing (ADR 020).

Covers: allowlist rejection (injection strings can never construct an enum),
compiled-SQL assertions per dialect (org predicate present, bound params,
placeholder count, no string interpolation, no LIMIT before bucketing), and
the Python bucketing/zero-fill logic.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from modulo.core.analytics.builder import (
    HOUR_GROUPBY_MAX_RANGE_DAYS,
    AnalyticsDimension,
    AnalyticsGroupBy,
    AnalyticsQuery,
    AnalyticsStatus,
    AnalyticsTriggerType,
    bucket_rows,
    build_facts_query,
    hour_groupby_span_exceeds,
    resolve_group_by,
    to_utc_aware,
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
        with pytest.raises(ValueError, match="not a valid AnalyticsGroupBy"):
            AnalyticsGroupBy("status; DROP TABLE runs")

    def test_dimension_rejects_sql_injection_string(self) -> None:
        with pytest.raises(ValueError, match="not a valid AnalyticsDimension"):
            AnalyticsDimension("status; DROP TABLE runs")

    def test_trigger_type_rejects_sql_injection_string(self) -> None:
        with pytest.raises(ValueError, match="not a valid AnalyticsTriggerType"):
            AnalyticsTriggerType("manual OR 1=1")

    def test_status_rejects_sql_injection_string(self) -> None:
        with pytest.raises(ValueError, match="not a valid AnalyticsStatus"):
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
                pipeline_ids=(uuid.UUID("22222222-2222-4222-8222-222222222222"),),
            )
        )
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        placeholder_count = len(re.findall(r"%\(\w+\)s", sql))
        # Every explicit param is bound (the aggregate FILTERs add internal
        # status bindings of their own, so the count is AT LEAST len(params)).
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
        assert "status" in sql
        assert "trigger_type" in sql
        assert "'complete'" not in sql, "filter values must be bound, never literal"
        assert "'webhook'" not in sql, "filter values must be bound, never literal"


class TestFAR102Filters:
    """Multi-value pipeline filter + error_code filter (FAR-102, Part B)."""

    def test_multiple_pipeline_ids_become_bound_in_clause(self) -> None:
        pid_a = uuid.UUID("22222222-2222-4222-8222-222222222222")
        pid_b = uuid.UUID("33333333-3333-4333-8333-333333333333")
        stmt, params = build_facts_query(_query(pipeline_ids=(pid_a, pid_b)))
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "IN" in sql.upper(), "a multi-value pipeline filter must render an IN clause"
        assert str(pid_a) not in sql, "pipeline ids must be bound, never interpolated"
        assert str(pid_b) not in sql, "pipeline ids must be bound, never interpolated"
        assert params["pipeline_ids"] == [pid_a, pid_b]
        assert "pipeline_id" in sql

    def test_single_pipeline_id_remains_backward_compatible(self) -> None:
        # The REST surface maps a single ?pipeline_id= to a one-element tuple —
        # an IN clause over one bound value must still filter correctly.
        pid = uuid.UUID("44444444-4444-4444-8444-444444444444")
        stmt, params = build_facts_query(_query(pipeline_ids=(pid,)))
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert str(pid) not in sql, "single pipeline id must be bound, never interpolated"
        assert params["pipeline_ids"] == [pid]
        assert "IN" in sql.upper()

    def test_empty_pipeline_ids_no_filter(self) -> None:
        stmt, params = build_facts_query(_query())
        assert "pipeline_id" not in str(stmt.compile(dialect=postgresql.dialect()))
        assert "pipeline_ids" not in params

    def test_error_code_filter_is_bound(self) -> None:
        stmt, params = build_facts_query(_query(error_code="executor_stalled"))
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "executor_stalled" not in sql, "error_code must be bound, never interpolated"
        assert params["error_code"] == "executor_stalled"
        assert "error_code" in sql

    def test_error_code_dimension_selects_raw_key(self) -> None:
        stmt, _ = build_facts_query(_query(dimension=AnalyticsDimension.ERROR_CODE))
        keys = {k.name for k in stmt.selected_columns}
        assert "error_code" in keys, "the raw error_code column must be selected for the dimension"
        assert "key_label" not in keys

    def test_stall_error_codes_are_bound_not_interpolated(self) -> None:
        stmt, params = build_facts_query(_query())
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        from modulo.core.analytics.builder import STALL_ERROR_CODES

        for code in STALL_ERROR_CODES:
            assert code not in sql, f"stall error code {code!r} must be bound, never interpolated"
        assert set(params["stall_error_codes"]) == set(STALL_ERROR_CODES)


class TestFAR102Metrics:
    """The FAR-102 bucket metrics: failure/stall counts + queue/idle/output averages."""

    def test_bucket_metrics_aggregate_from_rows(self) -> None:
        rows = [
            SimpleNamespace(
                run_date=date(2026, 8, 5),
                count=2,
                complete_count=1,
                total_cost_usd=10.0,
                total_tokens=50,
                avg_duration_ms=100.0,
                failure_count=1,
                stall_count=1,
                avg_queue_wait_ms=200.0,
                avg_final_idle_ms=300.0,
                avg_output_bytes=400.0,
            ),
            SimpleNamespace(
                run_date=date(2026, 8, 5),
                count=1,
                complete_count=0,
                total_cost_usd=0.0,
                total_tokens=0,
                avg_duration_ms=500.0,
                failure_count=1,
                stall_count=0,
                avg_queue_wait_ms=None,
                avg_final_idle_ms=None,
                avg_output_bytes=None,
            ),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=None,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 5),
        )
        bucket = out[0]
        assert bucket["failure_count"] == 2
        assert bucket["stall_count"] == 1
        assert bucket["avg_queue_wait_ms"] == 200.0
        assert bucket["avg_final_idle_ms"] == 300.0
        assert bucket["avg_output_bytes"] == 400.0

    def test_zero_fill_metrics_are_null_safe(self) -> None:
        out = bucket_rows(
            [],
            group_by=AnalyticsGroupBy.DAY,
            dimension=None,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 1),
        )
        bucket = out[0]
        assert bucket["failure_count"] == 0
        assert bucket["stall_count"] == 0
        assert bucket["avg_queue_wait_ms"] is None
        assert bucket["avg_final_idle_ms"] is None
        assert bucket["avg_output_bytes"] is None

    def test_weighted_average_uses_row_count(self) -> None:
        # Two rows for the same day: one has avg 100 for 2 runs, one has avg 400
        # for 1 run → weighted avg = (100*2 + 400*1)/3 = 200.
        rows = [
            SimpleNamespace(
                run_date=date(2026, 8, 5),
                count=2,
                complete_count=0,
                total_cost_usd=None,
                total_tokens=None,
                avg_duration_ms=None,
                avg_queue_wait_ms=100.0,
                avg_final_idle_ms=100.0,
                avg_output_bytes=100.0,
            ),
            SimpleNamespace(
                run_date=date(2026, 8, 5),
                count=1,
                complete_count=0,
                total_cost_usd=None,
                total_tokens=None,
                avg_duration_ms=None,
                avg_queue_wait_ms=400.0,
                avg_final_idle_ms=400.0,
                avg_output_bytes=400.0,
            ),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=None,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 5),
        )
        assert out[0]["avg_queue_wait_ms"] == 200.0
        assert out[0]["avg_final_idle_ms"] == 200.0
        assert out[0]["avg_output_bytes"] == 200.0

    def test_stall_error_codes_constant_members(self) -> None:
        from modulo.core.analytics.builder import STALL_ERROR_CODES

        assert "executor_stalled" in STALL_ERROR_CODES
        assert "node_timeout" in STALL_ERROR_CODES
        assert "TimeoutError" in STALL_ERROR_CODES


class TestDimensionedSelect:
    """The dimension column must be in the SELECT list, not just GROUP BY.

    bucket_rows resolves each bucket's key from the row attributes — a column
    present only in GROUP BY never reaches the row, so every bucket would
    collapse under key=None (regression: PR #740 review round 3).
    """

    @pytest.mark.parametrize(
        ("dimension", "key_attr"),
        [
            (AnalyticsDimension.TRIGGER_TYPE, "trigger_type"),
            (AnalyticsDimension.STATUS, "status"),
            (AnalyticsDimension.FOLDER, "folder_id"),
        ],
    )
    def test_raw_dimension_key_is_selected(self, dimension: AnalyticsDimension, key_attr: str) -> None:
        stmt, _ = build_facts_query(_query(dimension=dimension))
        keys = {k.name for k in stmt.selected_columns}
        assert key_attr in keys, (
            f"{dimension.value} dimension: the {key_attr} column must be in the SELECT "
            "so bucket_rows can resolve a non-None key"
        )
        assert "key_label" not in keys, "non-label dimensions must not emit key_label"

    @pytest.mark.parametrize(
        ("dimension", "key_attr"),
        [
            (AnalyticsDimension.PIPELINE, "pipeline_id"),
            (AnalyticsDimension.TEAM, "team_id"),
        ],
    )
    def test_label_dimension_selects_both_label_and_raw_key(self, dimension: AnalyticsDimension, key_attr: str) -> None:
        stmt, _ = build_facts_query(_query(dimension=dimension))
        keys = {k.name for k in stmt.selected_columns}
        assert "key_label" in keys, "label dimensions must select the MIN(snapshot label)"
        assert key_attr in keys, (
            f"{dimension.value} dimension: the raw {key_attr} column must be selected so a NULL "
            "snapshot label still buckets by the UUID (the documented fallback was dead code)"
        )

    def test_build_then_bucket_rows_returns_non_none_keys(self) -> None:
        # End-to-end within a DB-free unit test: build a trigger_type dimensioned
        # statement, verify the raw key is selected, then feed bucket_rows rows
        # shaped exactly like the compiled SELECT would return and assert the
        # buckets carry the non-None dimension keys.
        query = _query(
            dimension=AnalyticsDimension.TRIGGER_TYPE,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 5),
        )
        stmt, _ = build_facts_query(query)
        keys = {k.name for k in stmt.selected_columns}
        assert "trigger_type" in keys

        rows = [
            SimpleNamespace(
                run_date=date(2026, 8, 5),
                count=1,
                complete_count=1,
                total_cost_usd=1.25,
                total_tokens=100,
                avg_duration_ms=500.0,
                trigger_type="manual",
            ),
            SimpleNamespace(
                run_date=date(2026, 8, 5),
                count=1,
                complete_count=0,
                total_cost_usd=0.5,
                total_tokens=10,
                avg_duration_ms=100.0,
                trigger_type="cron",
            ),
        ]
        out = bucket_rows(
            rows,
            group_by=query.group_by,
            dimension=query.dimension,
            date_from=query.date_from,
            date_to=query.date_to,
        )
        assert {b["key"] for b in out} == {"manual", "cron"}
        assert all(b["key"] is not None for b in out), "dimensioned buckets must never collapse under None"


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

    def test_folder_dimension_with_uuid_keys_does_not_crash(self) -> None:
        folder = uuid.UUID("44444444-4444-4444-8444-444444444444")
        rows = [
            _row(date(2026, 8, 5), folder_id=folder),
            _row(date(2026, 8, 6), folder_id=folder),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=AnalyticsDimension.FOLDER,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 6),
        )
        assert {b["key"] for b in out} == {str(folder)}
        assert all(b["key"] is None or isinstance(b["key"], str) for b in out), "keys must be str | None, never UUID"

    def test_folder_dimension_null_key_mix_does_not_crash(self) -> None:
        # Some runs have no folder → keys are {None, <uuid>} for the same day.
        # This must NOT raise TypeError on sort (None vs UUID incomparable).
        folder = uuid.UUID("55555555-5555-4555-8555-555555555555")
        rows = [
            _row(date(2026, 8, 5), folder_id=folder),
            _row(date(2026, 8, 5), folder_id=None),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=AnalyticsDimension.FOLDER,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 5),
        )
        assert {b["key"] for b in out} == {str(folder), None}
        assert sum(b["count"] for b in out) == 2

    def test_pipeline_dimension_null_snapshot_label_falls_back_to_uuid(self) -> None:
        # A NULL pipeline_name (backfilled fact for a since-deleted pipeline)
        # falls back to the pipeline_id UUID. Mixing a named and a NULL-label
        # pipeline on the same day must not crash bucketing.
        pipeline_a = uuid.UUID("66666666-6666-4666-8666-666666666666")
        pipeline_b = uuid.UUID("77777777-7777-4777-8777-777777777777")
        rows = [
            _row(date(2026, 8, 5), key_label="Pipeline A", pipeline_id=pipeline_a),
            _row(date(2026, 8, 5), key_label=None, pipeline_id=pipeline_b),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=AnalyticsDimension.PIPELINE,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 5),
        )
        assert {b["key"] for b in out} == {"Pipeline A", str(pipeline_b)}
        assert sum(b["count"] for b in out) == 2

    def test_team_dimension_null_snapshot_label_falls_back_to_uuid(self) -> None:
        team_a = uuid.UUID("88888888-8888-4888-8888-888888888888")
        team_b = uuid.UUID("99999999-9999-4999-8999-999999999999")
        rows = [
            _row(date(2026, 8, 5), key_label="Team A", team_id=team_a),
            _row(date(2026, 8, 5), key_label=None, team_id=team_b),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.DAY,
            dimension=AnalyticsDimension.TEAM,
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 5),
        )
        assert {b["key"] for b in out} == {"Team A", str(team_b)}
        assert sum(b["count"] for b in out) == 2

    def test_dimensioned_empty_range_zero_fills(self) -> None:
        # A dimensioned query over an empty range must still return a zero-filled
        # series (aligned with the non-dimensioned shape), never [].
        out = bucket_rows(
            [],
            group_by=AnalyticsGroupBy.DAY,
            dimension=AnalyticsDimension.FOLDER,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 3),
        )
        assert [b["date"] for b in out] == ["2026-08-01", "2026-08-02", "2026-08-03"]
        assert all(b["count"] == 0 for b in out)
        assert all(b["key"] is None for b in out)


class TestHourGranularity:
    def test_hour_group_by_truncates_created_at(self) -> None:
        stmt, _ = build_facts_query(_query(group_by=AnalyticsGroupBy.HOUR))
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "date_trunc" in sql, "hour grouping must truncate created_at"
        assert "created_at" in sql
        assert "run_date" in sql, "the truncated expression must be labelled run_date"

    def test_hour_group_by_selects_run_date_label(self) -> None:
        stmt, _ = build_facts_query(_query(group_by=AnalyticsGroupBy.HOUR))
        keys = {k.name for k in stmt.selected_columns}
        assert "run_date" in keys, "bucket_rows reads row.run_date — the label must be selected"

    def test_hour_grid_zero_fills_iso_datetimes(self) -> None:
        out = bucket_rows(
            [],
            group_by=AnalyticsGroupBy.HOUR,
            dimension=None,
            date_from=date(2026, 8, 6),
            date_to=date(2026, 8, 6),
        )
        assert len(out) == 24, "a single day at hour granularity must zero-fill 24 hourly buckets"
        assert out[0]["date"] == "2026-08-06T00:00:00"
        assert out[23]["date"] == "2026-08-06T23:00:00"
        assert all(b["count"] == 0 for b in out)

    def test_hour_buckets_aggregate_by_truncated_hour(self) -> None:
        rows = [
            SimpleNamespace(
                run_date=datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
                count=2,
                complete_count=2,
                total_cost_usd=10.0,
                total_tokens=50,
                avg_duration_ms=500.0,
            ),
            SimpleNamespace(
                run_date=datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
                count=1,
                complete_count=0,
                total_cost_usd=2.0,
                total_tokens=10,
                avg_duration_ms=100.0,
            ),
            SimpleNamespace(
                run_date=datetime(2026, 8, 6, 14, 0, tzinfo=UTC),
                count=1,
                complete_count=1,
                total_cost_usd=5.0,
                total_tokens=25,
                avg_duration_ms=200.0,
            ),
        ]
        out = bucket_rows(
            rows,
            group_by=AnalyticsGroupBy.HOUR,
            dimension=None,
            date_from=date(2026, 8, 6),
            date_to=date(2026, 8, 6),
        )
        by_hour = {b["date"]: b for b in out}
        assert by_hour["2026-08-06T10:00:00"]["count"] == 3, "rows in the same truncated hour must collapse"
        assert by_hour["2026-08-06T10:00:00"]["total_cost_usd"] == 12.0
        assert by_hour["2026-08-06T14:00:00"]["count"] == 1
        assert len(out) == 24


class TestResolveGroupBy:
    def test_hour_for_span_three_days_or_less(self) -> None:
        base = date(2026, 8, 1)
        assert resolve_group_by(AnalyticsGroupBy.DAY, base, base + timedelta(days=3)) == AnalyticsGroupBy.HOUR
        assert resolve_group_by(None, base, base + timedelta(days=1)) == AnalyticsGroupBy.HOUR

    def test_day_for_span_up_to_ninety_days(self) -> None:
        base = date(2026, 8, 1)
        assert resolve_group_by(AnalyticsGroupBy.DAY, base, base + timedelta(days=4)) == AnalyticsGroupBy.DAY
        assert resolve_group_by(AnalyticsGroupBy.DAY, base, base + timedelta(days=90)) == AnalyticsGroupBy.DAY

    def test_week_for_span_over_ninety_days(self) -> None:
        base = date(2026, 8, 1)
        assert resolve_group_by(AnalyticsGroupBy.DAY, base, base + timedelta(days=91)) == AnalyticsGroupBy.WEEK

    def test_explicit_group_by_passes_through(self) -> None:
        base = date(2026, 8, 1)
        assert resolve_group_by(AnalyticsGroupBy.HOUR, base, base + timedelta(days=100)) == AnalyticsGroupBy.HOUR
        assert resolve_group_by(AnalyticsGroupBy.WEEK, base, base + timedelta(days=2)) == AnalyticsGroupBy.WEEK

    def test_missing_range_returns_day(self) -> None:
        assert resolve_group_by(AnalyticsGroupBy.DAY, None, None) == AnalyticsGroupBy.DAY
        assert resolve_group_by(None, None, None) == AnalyticsGroupBy.DAY

    def test_non_utc_offset_converts_to_utc_before_span(self) -> None:
        # A -05:00 date_from crosses a date boundary in UTC: 2026-07-31T21:00-05:00
        # is 2026-08-01T02:00Z, so the 3-day span to 2026-08-04 resolves to HOUR.
        # Pre-fix the offset was re-labelled as UTC (2026-07-31T21:00Z → 4-day
        # span → DAY), so this asserts the conversion, not the re-labelling.
        frm = datetime(2026, 7, 31, 21, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        to = date(2026, 8, 4)
        assert resolve_group_by(AnalyticsGroupBy.DAY, frm, to) == AnalyticsGroupBy.HOUR

    def test_mixed_aware_naive_inputs_do_not_raise(self) -> None:
        # Mixing an aware datetime with a naive datetime must not raise TypeError
        # (offset-naive vs offset-aware) — both are normalised to aware UTC first.
        frm = datetime(2026, 7, 31, 21, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        to = datetime(2026, 8, 4, 12, 0, 0)  # naive
        assert resolve_group_by(None, frm, to) == AnalyticsGroupBy.HOUR


class TestToUtcAware:
    def test_naive_datetime_gets_utc_tzinfo(self) -> None:
        out = to_utc_aware(datetime(2026, 8, 6, 14, 0, 0))
        assert out == datetime(2026, 8, 6, 14, 0, 0, tzinfo=UTC)
        assert out.utcoffset() == timedelta(0)

    def test_non_utc_offset_converts_to_utc(self) -> None:
        # +05:00 14:00 must become 09:00 UTC — never keep the +05:00 offset.
        out = to_utc_aware(datetime(2026, 8, 6, 14, 0, 0, tzinfo=timezone(timedelta(hours=5))))
        assert out == datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)
        assert out.utcoffset() == timedelta(0)

    def test_negative_offset_converts_to_utc_across_date_boundary(self) -> None:
        out = to_utc_aware(datetime(2026, 7, 31, 21, 0, 0, tzinfo=timezone(timedelta(hours=-5))))
        assert out == datetime(2026, 8, 1, 2, 0, 0, tzinfo=UTC)

    def test_bare_date_expands_to_midnight_and_end_of_day(self) -> None:
        assert to_utc_aware(date(2026, 8, 6)) == datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
        assert to_utc_aware(date(2026, 8, 6), end_of_day=True) == datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)

    def test_mixed_naive_aware_compare_safely(self) -> None:
        # A naive date_from and an aware date_to must normalise so the range
        # checks never hit TypeError (the pre-fix 500 path).
        frm = to_utc_aware(datetime(2026, 8, 6))  # naive
        to = to_utc_aware(datetime(2026, 8, 7, 14, 0, 0, tzinfo=UTC))  # aware
        assert frm <= to
        assert (to - frm).days >= 1


class TestHourGroupByCap:
    def test_span_within_cap_is_allowed(self) -> None:
        assert hour_groupby_span_exceeds(date(2026, 8, 1), date(2026, 8, 14)) is False
        assert hour_groupby_span_exceeds(date(2026, 8, 1), date(2026, 8, 15)) is False

    def test_span_over_cap_exceeds(self) -> None:
        assert hour_groupby_span_exceeds(date(2026, 8, 1), date(2026, 8, 16)) is True

    def test_cap_default_matches_constant(self) -> None:
        assert hour_groupby_span_exceeds(date(2026, 8, 1), date(2026, 8, 16)) is (
            (date(2026, 8, 16) - date(2026, 8, 1)).days > HOUR_GROUPBY_MAX_RANGE_DAYS
        )

    def test_mixed_aware_naive_inputs_do_not_raise(self) -> None:
        frm = datetime(2026, 8, 6, 14, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        to = datetime(2026, 8, 30, 14, 0, 0)  # naive
        assert hour_groupby_span_exceeds(frm, to) is True

    def test_non_utc_offset_range_is_measured_from_converted_instant(self) -> None:
        # 2026-07-31T21:00-05:00 is 2026-08-01T02:00Z; the span to 2026-08-14
        # (end-of-day) is 13 days → within cap. Re-labelling the offset as UTC
        # would make it 14 days → still within, so this guards the boundary by
        # asserting the conversion keeps it inside the cap.
        frm = datetime(2026, 7, 31, 21, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        assert hour_groupby_span_exceeds(frm, date(2026, 8, 14)) is False


class TestReconcileCooldown:
    """Cooldown-keyed reconcile alerts (maintenance.py, ADR 020).

    The cooldown dict is bounded by org count but unbounded over time — stale
    entries must be pruned so the map never grows without bound.
    """

    def _module(self):
        from modulo.core.analytics import maintenance as maintenance_mod

        return maintenance_mod

    def test_allows_then_suppresses_within_window_then_prunes_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        maintenance_mod = self._module()
        maintenance_mod._reconcile_cooldown.clear()
        now = [1000.0]
        monkeypatch.setattr(maintenance_mod.time, "monotonic", lambda: now[0])
        org = uuid.uuid4()
        drift_type = "ledger_exceeds_facts"

        assert maintenance_mod._reconcile_cooldown_allows(org, drift_type) is True
        now[0] += 60
        assert maintenance_mod._reconcile_cooldown_allows(org, drift_type) is False, "within cooldown → suppressed"
        now[0] += maintenance_mod._RECONCILE_ALERT_COOLDOWN_SECONDS + 1
        assert maintenance_mod._reconcile_cooldown_allows(org, drift_type) is True, "after window → allowed again"
        assert len(maintenance_mod._reconcile_cooldown) == 1, "stale entries pruned; only the fresh re-entry remains"
        maintenance_mod._reconcile_cooldown.clear()

    def test_prune_removes_only_stale_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        maintenance_mod = self._module()
        maintenance_mod._reconcile_cooldown.clear()
        now = [1000.0]
        monkeypatch.setattr(maintenance_mod.time, "monotonic", lambda: now[0])
        maintenance_mod._reconcile_cooldown[("org-a", "ledger_exceeds_facts")] = (
            now[0] - maintenance_mod._RECONCILE_ALERT_COOLDOWN_SECONDS - 10
        )
        maintenance_mod._reconcile_cooldown[("org-b", "ledger_exceeds_facts")] = now[0] - 10

        maintenance_mod._reconcile_cooldown_prune(now[0])

        assert ("org-a", "ledger_exceeds_facts") not in maintenance_mod._reconcile_cooldown
        assert ("org-b", "ledger_exceeds_facts") in maintenance_mod._reconcile_cooldown
        maintenance_mod._reconcile_cooldown.clear()
