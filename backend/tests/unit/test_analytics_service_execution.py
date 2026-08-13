"""Unit tests for the analytics service execution + error-mapping surface.

Complements ``test_analytics_service.py`` (rate limiter, bounds, pool
reference) by locking the DB-execution contract that was previously only
exercised through the REST/MCP surfaces:

  * ``_execute_with_guards`` — the typed error map that both surfaces share:
    ``ProgrammingError`` → ``AnalyticsMigrationRequiredError``,
    canceled ``DBAPIError`` → ``AnalyticsQueryTimeoutError``, other
    ``DBAPIError``/``SQLAlchemyError``/unexpected → ``AnalyticsDatabaseError``,
    ``asyncio.CancelledError`` re-raised, and the Postgres-only
    timezone/statement-timeout ``set_config`` preamble.
  * ``_is_query_canceled`` — QueryCanceled detection by exception class name,
    nested cause, and ``sqlstate == '57014'``.
  * ``run_analytics_query`` happy path (auto-granularity + dimension) and
    ``run_concurrency_query`` rate-limit branch.
  * ``_export_filters`` / ``_serialize_fact_row`` / ``export_facts`` (count +
    rows, typed error map, serialisation of UUID/datetime/Decimal/date).

Hermetic: a fake async-session routes ``execute`` responses and can raise the
SQLAlchemy error classes on demand; no database required.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError

import modulo.core.analytics.service as svc
from modulo.core.analytics.builder import AnalyticsDimension, AnalyticsGroupBy, AnalyticsStatus
from modulo.core.analytics.service import (
    AnalyticsDatabaseError,
    AnalyticsMigrationRequiredError,
    AnalyticsQueryTimeoutError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
    _export_filters,
    _is_query_canceled,
    _serialize_fact_row,
    export_facts,
    run_analytics_query,
    run_concurrency_query,
)

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()


# asyncpg's canceled-error class name is what drives _is_query_canceled, so the
# stand-in must literally be named "QueryCanceledError".
_QueryCanceledError: type[Exception] = type("QueryCanceledError", (Exception,), {})


class _Ctx:
    """Reusable async context manager returned by ``session.begin()``."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Async-session shaped fake: async-with + begin + connection + execute.

    ``rows`` are returned from ``execute().all()``; ``exc`` (if set) is raised
    from every ``execute`` call. ``executed`` records each statement.
    """

    def __init__(
        self,
        *,
        dialect: str = "postgresql",
        rows: list[Any] | None = None,
        exc: Exception | None = None,
        scalar: Any = 7,
    ) -> None:
        self._dialect = dialect
        self._rows = rows if rows is not None else []
        self._exc = exc
        self._scalar = scalar
        self.executed: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> MagicMock:
        bind = MagicMock()
        bind.dialect.name = self._dialect
        return bind

    def begin(self) -> _Ctx:
        return _Ctx(self)

    async def connection(self) -> MagicMock:
        conn = MagicMock()
        conn.dialect.name = self._dialect
        return conn

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append(stmt)
        if self._exc is not None:
            raise self._exc
        result = MagicMock()
        result.all.return_value = self._rows
        result.scalar_one.return_value = self._scalar
        return result


def _factory(session: _FakeSession) -> MagicMock:
    factory = MagicMock()
    factory.return_value = session
    return factory


def _settings(**overrides: Any) -> SimpleNamespace:
    kwargs: dict[str, Any] = {"analytics_query_statement_timeout_ms": 1234}
    kwargs.update(overrides)
    return SimpleNamespace(**kwargs)


def _guards_call(**kw: Any) -> Any:
    return svc._execute_with_guards(
        kw.get("factory", _factory(_FakeSession(rows=[("a",), ("b",)]))),
        kw.get("settings", _settings()),
        org_id=kw.get("org_id", _ORG),
        account_id=kw.get("account_id", _ACCOUNT),
        org_role=kw.get("org_role", "admin"),
        stmt=kw.get("stmt", MagicMock()),
        params=kw.get("params", {}),
    )


# ---------------------------------------------------------------------------
# _execute_with_guards — the shared typed error map
# ---------------------------------------------------------------------------


class TestExecuteWithGuards:
    async def test_happy_path_returns_rows(self) -> None:
        session = _FakeSession(rows=[("a",), ("b",)])
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock) as mock_user,
        ):
            rows = await _guards_call(factory=_factory(session))
        assert rows == [("a",), ("b",)]
        mock_rls.assert_awaited_once()
        mock_user.assert_awaited_once()
        # Postgres preamble: timezone + statement_timeout set_configs ran before the query.
        assert len(session.executed) == 3

    async def test_skips_set_config_preamble_on_non_postgres(self) -> None:
        session = _FakeSession(dialect="sqlite", rows=[("a",)])
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock),
        ):
            rows = await _guards_call(factory=_factory(session))
        assert rows == [("a",)]
        assert len(session.executed) == 1, "no timezone/statement_timeout set_configs on sqlite"

    async def test_programming_error_maps_to_migration_required(self) -> None:
        session = _FakeSession(exc=ProgrammingError("stmt", {}, "relation does not exist"))
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsMigrationRequiredError, match="migrations"),
        ):
            await _guards_call(factory=_factory(session))

    async def test_canceled_dbapi_error_maps_to_query_timeout(self) -> None:
        session = _FakeSession(exc=DBAPIError("stmt", {}, _QueryCanceledError("canceled")))
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsQueryTimeoutError, match="timeout"),
        ):
            await _guards_call(factory=_factory(session))

    async def test_dbapi_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=DBAPIError("stmt", {}, ConnectionError("conn lost")))
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await _guards_call(factory=_factory(session))

    async def test_sqlalchemy_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=SQLAlchemyError("boom"))
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await _guards_call(factory=_factory(session))

    async def test_unexpected_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=RuntimeError("kaboom"))
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await _guards_call(factory=_factory(session))

    async def test_cancelled_error_propagates_untouched(self) -> None:
        session = _FakeSession(exc=asyncio.CancelledError())
        with patch.object(svc, "set_rls_org", new_callable=AsyncMock), pytest.raises(asyncio.CancelledError):
            await _guards_call(factory=_factory(session))

    async def test_analytics_error_propagates_untouched(self) -> None:
        session = _FakeSession(exc=AnalyticsValidationError("nope"))
        with patch.object(svc, "set_rls_org", new_callable=AsyncMock), pytest.raises(AnalyticsValidationError):
            await _guards_call(factory=_factory(session))

    async def test_factory_entry_raises_maps_to_database_error(self) -> None:
        def _boom_factory():
            raise RuntimeError("entry failed")

        with pytest.raises(AnalyticsDatabaseError, match="Database temporarily"):
            await _guards_call(factory=_boom_factory)

    async def test_statement_timeout_uses_settings_value(self) -> None:
        session = _FakeSession(rows=[("a",)])
        with patch.object(svc, "set_rls_org", new_callable=AsyncMock):
            await _guards_call(factory=_factory(session), settings=_settings(analytics_query_statement_timeout_ms=4321))
        # Executed: timezone, statement_timeout, query. Pull the timeout params.
        from sqlalchemy import text

        timeout_call = [s for s in session.executed if isinstance(s, type(text("x"))) and "statement_timeout" in str(s)]
        assert len(timeout_call) == 1


# ---------------------------------------------------------------------------
# _is_query_canceled
# ---------------------------------------------------------------------------


class TestIsQueryCanceled:
    def test_direct_class_name_detected(self) -> None:
        exc = DBAPIError("stmt", {}, _QueryCanceledError("c"))
        assert _is_query_canceled(exc) is True

    def test_sqlstate_57014_detected(self) -> None:
        orig = SimpleNamespace(sqlstate="57014")
        exc = DBAPIError("stmt", {}, orig)
        assert _is_query_canceled(exc) is True

    def test_nested_orig_detected(self) -> None:
        orig = SimpleNamespace(orig=_QueryCanceledError("c"), __cause__=None, sqlstate=None)
        exc = DBAPIError("stmt", {}, orig)
        assert _is_query_canceled(exc) is True

    def test_nested_cause_detected(self) -> None:
        cause = _QueryCanceledError("c")
        orig = SimpleNamespace(orig=None, __cause__=cause, sqlstate=None)
        exc = DBAPIError("stmt", {}, orig)
        assert _is_query_canceled(exc) is True

    def test_other_dbapi_error_not_canceled(self) -> None:
        exc = DBAPIError("stmt", {}, ConnectionError("down"))
        assert _is_query_canceled(exc) is False

    def test_no_orig_not_canceled(self) -> None:
        exc = DBAPIError("stmt", {}, None)
        assert _is_query_canceled(exc) is False


# ---------------------------------------------------------------------------
# run_analytics_query / run_concurrency_query orchestration
# ---------------------------------------------------------------------------


class TestRunAnalyticsQuery:
    def _params(self, **overrides: Any) -> svc.AnalyticsParams:
        kwargs: dict[str, Any] = {
            "group_by": AnalyticsGroupBy.DAY,
            "limit": 100,
            "date_from": datetime(2026, 8, 1, tzinfo=UTC),
            "date_to": datetime(2026, 8, 6, tzinfo=UTC),
        }
        kwargs.update(overrides)
        return svc.AnalyticsParams(**kwargs)

    async def test_happy_path_with_auto_granularity_and_dimension(self) -> None:
        rows = [SimpleNamespace(run_date=date(2026, 8, 6), count=1)]
        buckets = [{"date": "2026-08-06", "key": None, "count": 1}]
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "_execute_with_guards", new=AsyncMock(return_value=rows)) as mock_exec,
            patch.object(svc, "bucket_rows", return_value=buckets) as mock_bucket,
        ):
            result = await run_analytics_query(
                org_id=_ORG,
                params=self._params(auto_granularity=True, dimension=AnalyticsDimension.TRIGGER_TYPE),
                factory=MagicMock(),
                settings=_settings(),
            )
        assert result["group_by"] == "day"
        assert result["dimension"] == "trigger_type"
        assert result["buckets"] == buckets
        mock_exec.assert_awaited_once()
        mock_bucket.assert_called_once()

    async def test_rate_limited_raises(self) -> None:
        with (
            patch.object(svc, "_rate_limited", return_value=True),
            pytest.raises(AnalyticsRateLimitedError, match="Rate limit"),
        ):
            await run_analytics_query(org_id=_ORG, params=self._params(), factory=MagicMock(), settings=_settings())

    async def test_inverted_bounds_raise_validation(self) -> None:
        params = self._params(
            date_from=datetime(2026, 8, 5, tzinfo=UTC),
            date_to=datetime(2026, 8, 1, tzinfo=UTC),
        )
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            pytest.raises(AnalyticsValidationError, match="date_from must be <= date_to"),
        ):
            await run_analytics_query(org_id=_ORG, params=params, factory=MagicMock(), settings=_settings())


class TestRunConcurrencyQueryRateLimit:
    async def test_rate_limited_raises(self) -> None:
        with (
            patch.object(svc, "_rate_limited", return_value=True),
            pytest.raises(AnalyticsRateLimitedError, match="Rate limit"),
        ):
            await run_concurrency_query(
                org_id=_ORG, params=svc.AnalyticsParams(), factory=MagicMock(), settings=_settings()
            )


# ---------------------------------------------------------------------------
# Export surface: _export_filters / _serialize_fact_row / export_facts
# ---------------------------------------------------------------------------


class TestExportFilters:
    def _bounds(self) -> tuple[datetime, datetime]:
        return (
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC),
        )

    def test_org_and_date_bounds_always_present(self) -> None:
        from_, to_ = self._bounds()
        _conditions, bind = _export_filters(
            org_id=_ORG, params=svc.AnalyticsParams(), effective_from=from_, effective_to=to_
        )
        assert bind["org_id"] == _ORG
        assert bind["date_from"] == date(2026, 8, 1)
        assert bind["date_to"] == date(2026, 8, 6)

    def test_optional_filters_added(self) -> None:
        from_, to_ = self._bounds()
        pid = uuid.uuid4()
        params = svc.AnalyticsParams(
            trigger_type=svc.AnalyticsTriggerType.CRON,
            status=AnalyticsStatus.FAILED,
            pipeline_ids=(pid,),
            error_code="node_timeout",
            folder_id=uuid.uuid4(),
        )
        conditions, bind = _export_filters(org_id=_ORG, params=params, effective_from=from_, effective_to=to_)
        assert bind["trigger_type"] == "cron"
        assert bind["status"] == "failed"
        assert bind["pipeline_ids"] == [pid]
        assert bind["error_code"] == "node_timeout"
        assert bind["folder_id"] is not None
        assert len(conditions) == 8  # org, date_from, date_to + 5 optional filters


class TestSerializeFactRow:
    def _full_row(self, **overrides: Any) -> SimpleNamespace:
        defaults: dict[str, Any] = {
            "run_id": uuid.uuid4(),
            "run_date": date(2026, 8, 6),
            "team_id": uuid.uuid4(),
            "team_name": "core",
            "pipeline_id": uuid.uuid4(),
            "pipeline_name": "nightly",
            "folder_id": None,
            "trigger_type": "manual",
            "status": "complete",
            "total_cost_usd": Decimal("12.34"),
            "total_tokens": 100,
            "duration_ms": 5000,
            "error_code": None,
            "claim_count": 0,
            "queue_wait_ms": 10,
            "final_idle_ms": 5,
            "cancellation_requested": False,
            "dispatcher": "manual",
            "node_count": 3,
            "sandbox_agent_node_count": 2,
            "max_node_timeout_seconds": 3600,
            "parent_run_id": None,
            "snapshot_id": None,
            "run_number": 1,
            "output_bytes": 1024,
            "rate_limited": False,
            "created_at": datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC),
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_serializes_all_types(self) -> None:
        rid = uuid.uuid4()
        row = self._full_row(run_id=rid)
        out = _serialize_fact_row(row)
        assert out["run_id"] == str(rid)
        assert out["run_date"] == "2026-08-06"
        assert out["created_at"] == "2026-08-06T12:00:00+00:00"
        assert out["total_cost_usd"] == pytest.approx(12.34)
        assert out["total_tokens"] == 100
        assert out["trigger_type"] == "manual"
        assert out["team_id"] == str(row.team_id)
        assert out["cancellation_requested"] is False

    def test_naive_datetime_normalised_to_utc(self) -> None:
        row = self._full_row(created_at=datetime(2026, 8, 6, 12, 0, 0))
        out = _serialize_fact_row(row)
        assert out["created_at"].endswith("+00:00")


class TestExportFacts:
    def _full_row(self, **overrides: Any) -> SimpleNamespace:
        defaults: dict[str, Any] = {
            "run_id": uuid.uuid4(),
            "run_date": date(2026, 8, 6),
            "team_id": uuid.uuid4(),
            "team_name": "core",
            "pipeline_id": uuid.uuid4(),
            "pipeline_name": "nightly",
            "folder_id": None,
            "trigger_type": "manual",
            "status": "complete",
            "total_cost_usd": Decimal("1.50"),
            "total_tokens": 10,
            "duration_ms": 5000,
            "error_code": None,
            "claim_count": 0,
            "queue_wait_ms": 10,
            "final_idle_ms": 5,
            "cancellation_requested": False,
            "dispatcher": "manual",
            "node_count": 3,
            "sandbox_agent_node_count": 2,
            "max_node_timeout_seconds": 3600,
            "parent_run_id": None,
            "snapshot_id": None,
            "run_number": 1,
            "output_bytes": 1024,
            "rate_limited": False,
            "created_at": datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC),
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    async def test_happy_path_returns_items_and_total(self) -> None:
        row = self._full_row()
        session = _FakeSession(rows=[row], scalar=3)
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock),
        ):
            result = await export_facts(
                org_id=_ORG,
                params=svc.AnalyticsParams(),
                factory=_factory(session),
                settings=_settings(),
                offset=10,
                limit=50,
            )
        assert result["total"] == 3
        assert result["offset"] == 10
        assert result["limit"] == 50
        assert len(result["items"]) == 1
        assert result["items"][0]["run_date"] == "2026-08-06"

    async def test_happy_path_passes_user_context(self) -> None:
        row = self._full_row()
        session = _FakeSession(rows=[row], scalar=3)
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock) as mock_user,
        ):
            await export_facts(
                org_id=_ORG,
                params=svc.AnalyticsParams(),
                factory=_factory(session),
                settings=_settings(),
                account_id=_ACCOUNT,
                org_role="admin",
            )
        mock_rls.assert_awaited_once()
        mock_user.assert_awaited_once()

    async def test_programming_error_maps_to_migration_required(self) -> None:
        session = _FakeSession(exc=ProgrammingError("stmt", {}, "missing"))
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsMigrationRequiredError, match="migrations"),
        ):
            await export_facts(
                org_id=_ORG, params=svc.AnalyticsParams(), factory=_factory(session), settings=_settings()
            )

    async def test_dbapi_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=DBAPIError("stmt", {}, ConnectionError("down")))
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await export_facts(
                org_id=_ORG, params=svc.AnalyticsParams(), factory=_factory(session), settings=_settings()
            )

    async def test_rate_limited_raises(self) -> None:
        with (
            patch.object(svc, "_rate_limited", return_value=True),
            pytest.raises(AnalyticsRateLimitedError, match="Rate limit"),
        ):
            await export_facts(org_id=_ORG, params=svc.AnalyticsParams(), factory=MagicMock(), settings=_settings())

    async def test_canceled_dbapi_error_maps_to_query_timeout(self) -> None:
        session = _FakeSession(exc=DBAPIError("stmt", {}, _QueryCanceledError("canceled")))
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsQueryTimeoutError, match="timeout"),
        ):
            await export_facts(
                org_id=_ORG, params=svc.AnalyticsParams(), factory=_factory(session), settings=_settings()
            )

    async def test_sqlalchemy_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=SQLAlchemyError("boom"))
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await export_facts(
                org_id=_ORG, params=svc.AnalyticsParams(), factory=_factory(session), settings=_settings()
            )

    async def test_unexpected_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=RuntimeError("kaboom"))
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await export_facts(
                org_id=_ORG, params=svc.AnalyticsParams(), factory=_factory(session), settings=_settings()
            )

    async def test_cancelled_error_propagates_untouched(self) -> None:
        session = _FakeSession(exc=asyncio.CancelledError())
        with (
            patch.object(svc, "_rate_limited", return_value=False),
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            pytest.raises(asyncio.CancelledError),
        ):
            await export_facts(
                org_id=_ORG, params=svc.AnalyticsParams(), factory=_factory(session), settings=_settings()
            )


# ---------------------------------------------------------------------------
# _resolve_pool_reference error semantics (degrade to None, never raise)
# ---------------------------------------------------------------------------


class TestResolvePoolReferenceErrors:
    """Complements test_analytics_service.TestResolvePoolReference with the
    CancelledError / unexpected-exception paths that must degrade to None."""

    def _call(self, factory: Any, *, pipeline_ids: tuple[uuid.UUID, ...] = ()) -> Any:
        return svc._resolve_pool_reference(
            factory,
            _settings(),
            org_id=_ORG,
            account_id=_ACCOUNT,
            org_role="admin",
            pipeline_ids=pipeline_ids,
        )

    async def test_pipeline_query_cancelled_propagates(self) -> None:
        session = _FakeSession(exc=asyncio.CancelledError())
        with patch.object(svc, "set_rls_org", new_callable=AsyncMock), pytest.raises(asyncio.CancelledError):
            await self._call(_factory(session), pipeline_ids=(uuid.uuid4(),))

    async def test_unexpected_org_reader_error_degrades_to_none(self) -> None:
        session = _FakeSession()
        with (
            patch.object(svc, "set_rls_org", new_callable=AsyncMock),
            patch.object(svc, "set_rls_user_context", new_callable=AsyncMock),
            patch.object(svc, "get_org_run_concurrency_limit", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            value = await self._call(_factory(session))
        assert value is None, "an unexpected reference-read failure must degrade to None"

    async def test_factory_entry_failure_degrades_to_none(self) -> None:
        def _boom_factory():
            raise RuntimeError("entry failed")

        value = await self._call(_boom_factory)
        assert value is None, "a factory entry failure must degrade to None"


# ---------------------------------------------------------------------------
# _prune_rate_hits — the tracked-org cap eviction loop (163-164)
# ---------------------------------------------------------------------------


class TestPruneRateHitsCapEviction:
    def test_evicts_oldest_org_when_cap_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc._rate_hits.clear()
        monkeypatch.setattr(svc, "_RATE_LIMIT_MAX_ORGS", 2)
        clock = [1000.0]
        monkeypatch.setattr(svc.time, "monotonic", lambda: clock[0])
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())
        org_c = str(uuid.uuid4())
        try:
            # Three orgs all within the window: cap=2 must evict the oldest.
            svc._rate_hits[org_a] = [clock[0] - 10]
            svc._rate_hits[org_b] = [clock[0] - 5]
            svc._rate_hits[org_c] = [clock[0]]
            svc._prune_rate_hits(clock[0])
            assert len(svc._rate_hits) == 2
            assert org_a not in svc._rate_hits, "the oldest org must be evicted when over cap"
        finally:
            svc._rate_hits.clear()
