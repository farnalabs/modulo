"""Unit tests for the advisory guardrail scorecard (FAR-217).

Covers the pure aggregation/drift derivation (``_rate``, ``_drift_indicator``,
``_assemble_scorecard``) and the orchestration surface
(``run_guardrail_scorecard``: RLS context, statement-timeout preamble, the
typed error map, and the first-try-pass vs corrected-pass SEPARATION contract
— the two are never merged into a single pass rate).

Hermetic: a fake async session routes aggregate rows and can raise the
SQLAlchemy error classes on demand; no database required.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError

from modulo.core.analytics import guardrails as gr
from modulo.core.analytics.service import (
    AnalyticsDatabaseError,
    AnalyticsMigrationRequiredError,
    AnalyticsQueryTimeoutError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
)

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()

# asyncpg's canceled-error class name is what drives _is_query_canceled, so the
# stand-in must literally be named "QueryCanceledError".
_QueryCanceledError: type[Exception] = type("QueryCanceledError", (Exception,), {})


def _runs_row(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "runs_total": 4,
        "runs_with_guardrail": 4,
        "runs_with_violations": 2,
        "first_try_pass_runs": 2,
        "runs_blocked": 1,
        "bound_total": 8,
        "evaluated_total": 8,
        "passed_total": 4,
        "violated_total": 4,
        "observed_total": 1,
        "errored_total": 1,
        "redacted_total": 2,
        "skipped_total": 1,
        "expected_skips_total": 0,
        "unexpected_skips_total": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _corrections_row(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "corrections_total": 10,
        "converged_clean": 6,
        "escalated_hitl": 3,
        "dismissed": 1,
        "in_flight": 0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# SQL builders — dialect-aware JSON extraction + explicit org predicate
# ---------------------------------------------------------------------------


class TestStatementBuilders:
    def _bounds(self) -> tuple[datetime, datetime]:
        return datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 7, tzinfo=UTC)

    def test_runs_stmt_has_org_predicate_and_json_keys(self) -> None:
        from modulo.core.pipeline_engine.error_codes import expand_code_variants

        date_from, date_to = self._bounds()
        stmt = gr._build_runs_scorecard_stmt("postgresql", _ORG, date_from, date_to)
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "runs.organisation_id = " in sql
        assert "guardrail_summary_json ->> 'bound'" in sql
        assert "guardrail_summary_json ->> 'violated'" in sql
        for code in expand_code_variants("eval.blocked"):
            assert f"'{code}'" in sql, f"blocked variant {code} missing from IN clause"
        for label in ("bound", "evaluated", "passed", "observed", "errored", "redacted", "skipped"):
            assert f"->> '{label}'" in sql, f"json key {label} missing"

    def test_budget_exhausted_stmt_uses_real_verdict_key_on_postgres(self) -> None:
        """Regression: the postgres branch must extract the *passed* key, not the
        literal 'key' — otherwise budget_exhausted is always 0 in production."""
        date_from, date_to = self._bounds()
        stmt = gr._build_budget_exhausted_stmt("postgresql", _ORG, date_from, date_to)
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "audit_events.organisation_id = " in sql
        assert "guardrail.correction_escalated" in sql
        assert "payload_json ->> 'verdict'" in sql
        assert "->> 'key'" not in sql
        assert "'budget_exhausted'" in sql

    def test_corrections_stmt_counts_ai_handlers_only(self) -> None:
        date_from, date_to = self._bounds()
        stmt = gr._build_corrections_stmt(_ORG, date_from, date_to)
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "feedback_records.organisation_id = " in sql
        assert "'ai_correction'" in sql
        assert "'ai_correction_with_human_review'" in sql
        for status in ("pending", "routing", "correcting"):
            assert f"'{status}'" in sql, f"in-flight status {status} missing"


# ---------------------------------------------------------------------------
# _rate — null-safe ratio
# ---------------------------------------------------------------------------


class TestRate:
    def test_denominator_zero_returns_none(self) -> None:
        assert gr._rate(5, 0) is None

    def test_absent_denominator_returns_none(self) -> None:
        assert gr._rate(5, None) is None

    def test_rounds_to_4dp(self) -> None:
        assert gr._rate(1, 3) == 0.3333

    def test_zero_numerator_returns_zero(self) -> None:
        assert gr._rate(0, 10) == 0.0


# ---------------------------------------------------------------------------
# _drift_indicator — advisory only, never a gate
# ---------------------------------------------------------------------------


class TestDriftIndicator:
    def test_unexpected_skips_always_flag_drift(self) -> None:
        flagged, label = gr._drift_indicator(unexpected_skips=1, current_rate=0.0, baseline_rate=0.0)
        assert flagged is True
        assert label == "drift"

    def test_rate_above_baseline_by_margin_flags_drift(self) -> None:
        flagged, _ = gr._drift_indicator(unexpected_skips=0, current_rate=0.15, baseline_rate=0.05)
        assert flagged is True

    def test_rate_within_margin_is_in_band(self) -> None:
        flagged, label = gr._drift_indicator(unexpected_skips=0, current_rate=0.06, baseline_rate=0.05)
        assert flagged is False
        assert label == "in_band"

    def test_no_baseline_is_informational_only(self) -> None:
        flagged, label = gr._drift_indicator(unexpected_skips=0, current_rate=0.06, baseline_rate=None)
        assert flagged is False
        assert label == "no_baseline"


# ---------------------------------------------------------------------------
# _assemble_scorecard — the separation contract
# ---------------------------------------------------------------------------


class TestAssembleScorecard:
    def _call(self, **kw: Any) -> dict[str, Any]:
        return gr._assemble_scorecard(
            kw.get("runs_row", _runs_row()),
            kw.get("corrections_row", _corrections_row()),
            kw.get("budget_exhausted", 2),
            kw.get("baseline_row", _runs_row(errored_total=1, bound_total=8)),
            date_from="2026-08-01T00:00:00+00:00",
            date_to="2026-08-07T23:59:59+00:00",
            baseline_window_days=7,
        )

    def test_advisory_contract_surfaces(self) -> None:
        result = self._call()
        assert result["advisory_only"] is True
        assert "never" in result["rates"]["note"]
        assert result["self_correction"]["note"]
        assert result["evasion_band_drift"]["advisory_only"] is True
        assert result["evasion_band_drift"]["note"]

    def test_fire_counts_are_summed(self) -> None:
        result = self._call()
        counts = result["fire_counts"]
        assert counts["bound"] == 8
        assert counts["violated"] == 4
        assert counts["observed"] == 1
        assert counts["errored"] == 1
        assert counts["redacted"] == 2
        assert counts["skipped"] == 1
        assert counts["unexpected_skips"] == 1
        assert result["scope"]["runs_with_guardrail"] == 4
        assert result["scope"]["runs_with_violations"] == 2
        assert result["scope"]["runs_blocked"] == 1

    def test_first_try_pass_and_corrected_pass_are_separate(self) -> None:
        """The core Goodhart contract: first-try-pass and corrected-pass must
        never collapse into a single pass rate."""
        result = self._call()
        rates = result["rates"]
        corrections = result["self_correction"]
        # first-try-pass: 2 clean runs of 4 guarded = 0.5
        assert rates["first_try_pass_rate"] == 0.5
        # corrected-pass: 6 converged of 10 corrections = 0.6
        assert corrections["corrected_pass_rate"] == 0.6
        # They are DIFFERENT numbers in SEPARATE objects — no combined figure.
        assert "corrected_pass_rate" not in rates
        assert "first_try_pass_rate" not in corrections

    def test_raw_violation_rate_is_detection_only(self) -> None:
        result = self._call()
        assert result["rates"]["raw_violation_rate"] == 0.5  # 4 violated / 8 bound

    def test_no_guardrails_yields_none_rates(self) -> None:
        result = self._call(
            runs_row=_runs_row(
                runs_total=0,
                runs_with_guardrail=0,
                runs_with_violations=0,
                first_try_pass_runs=0,
                runs_blocked=0,
                bound_total=0,
                violated_total=0,
                unexpected_skips_total=0,
            ),
            corrections_row=_corrections_row(corrections_total=0, converged_clean=0, escalated_hitl=0),
            baseline_row=_runs_row(bound_total=0, errored_total=0),
        )
        assert result["rates"]["raw_violation_rate"] is None
        assert result["rates"]["first_try_pass_rate"] is None
        assert result["self_correction"]["corrected_pass_rate"] is None
        assert result["evasion_band_drift"]["current_errored_rate"] is None
        assert result["evasion_band_drift"]["baseline_errored_rate"] is None
        assert result["evasion_band_drift"]["drift_indicator"] == "no_baseline"

    def test_drift_detected_from_unexpected_skips(self) -> None:
        result = self._call(runs_row=_runs_row(unexpected_skips_total=2))
        drift = result["evasion_band_drift"]
        assert drift["drift_detected"] is True
        assert drift["drift_indicator"] == "drift"

    def test_budget_exhausted_is_reported_within_self_correction(self) -> None:
        result = self._call(budget_exhausted=2)
        assert result["self_correction"]["budget_exhausted"] == 2


# ---------------------------------------------------------------------------
# run_guardrail_scorecard — orchestration + typed error map
# ---------------------------------------------------------------------------


class _Ctx:
    """Reusable async context manager returned by ``session.begin()``."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Async-session shaped fake returning a per-statement queue of rows.

    ``row_batches`` is a list of result batches — one per REAL statement (the
    scorecard runs 4: runs, corrections, budget-exhausted scalar, baseline).
    Postgres ``set_config`` preamble statements (``TextClause``) are recognised
    and answered with a benign result without consuming a batch. ``exc`` (if
    set) is raised from every ``execute``.
    """

    def __init__(
        self,
        *,
        dialect: str = "postgresql",
        row_batches: list[Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._dialect = dialect
        self._row_batches = list(row_batches or [])
        self._exc = exc
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
        from sqlalchemy.sql.elements import TextClause

        if isinstance(stmt, TextClause):
            result.one.return_value = ("ok",)
            result.scalar.return_value = "ok"
            return result
        batch = self._row_batches.pop(0) if self._row_batches else [("x",)]
        result.one.return_value = batch[0]
        result.scalar.return_value = batch[0]
        return result


def _factory(session: _FakeSession) -> MagicMock:
    factory = MagicMock()
    factory.return_value = session
    return factory


def _settings(**overrides: Any) -> SimpleNamespace:
    kwargs: dict[str, Any] = {"analytics_query_statement_timeout_ms": 1234}
    kwargs.update(overrides)
    return SimpleNamespace(**kwargs)


def _default_batches() -> list[list[Any]]:
    """The 4 statement results the scorecard expects, in order."""
    return [
        [_runs_row()],
        [_corrections_row()],
        [2],
        [_runs_row(errored_total=1, bound_total=8)],
    ]


class TestRunGuardrailScorecard:
    async def _call(
        self,
        session: _FakeSession,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict[str, Any]:
        return await gr.run_guardrail_scorecard(
            org_id=_ORG,
            factory=_factory(session),
            settings=_settings(),
            account_id=_ACCOUNT,
            org_role="admin",
            date_from=date_from,
            date_to=date_to,
        )

    async def test_happy_path_aggregates_and_sets_rls(self) -> None:
        session = _FakeSession(row_batches=_default_batches())
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch.object(gr, "set_rls_user_context", new_callable=AsyncMock) as mock_user,
            patch.object(gr, "_rate_limited", return_value=False),
        ):
            result = await self._call(
                session,
                date_from=datetime(2026, 8, 1, tzinfo=UTC),
                date_to=datetime(2026, 8, 7, tzinfo=UTC),
            )
        assert result["advisory_only"] is True
        assert result["scope"]["runs_with_guardrail"] == 4
        assert result["rates"]["first_try_pass_rate"] == 0.5
        assert result["self_correction"]["corrected_pass_rate"] == 0.6
        assert result["self_correction"]["budget_exhausted"] == 2
        assert result["evasion_band_drift"]["baseline_window_days"] == 6
        mock_rls.assert_awaited_once()
        mock_user.assert_awaited_once()
        # Postgres preamble: timezone + statement_timeout + 4 statements.
        assert len(session.executed) == 6

    async def test_rate_limited_raises(self) -> None:
        session = _FakeSession(row_batches=_default_batches())
        with (
            patch.object(gr, "_rate_limited", return_value=True),
            pytest.raises(AnalyticsRateLimitedError, match="Rate limit"),
        ):
            await self._call(session)

    async def test_inverted_bounds_raise_validation(self) -> None:
        session = _FakeSession(row_batches=_default_batches())
        with (
            patch.object(gr, "_rate_limited", return_value=False),
            pytest.raises(AnalyticsValidationError, match="date_from must be <= date_to"),
        ):
            await self._call(
                session,
                date_from=datetime(2026, 8, 5, tzinfo=UTC),
                date_to=datetime(2026, 8, 1, tzinfo=UTC),
            )

    async def test_programming_error_maps_to_migration_required(self) -> None:
        session = _FakeSession(exc=ProgrammingError("stmt", {}, "relation does not exist"))
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock),
            patch.object(gr, "_rate_limited", return_value=False),
            pytest.raises(AnalyticsMigrationRequiredError, match="migrations"),
        ):
            await self._call(session)

    async def test_canceled_dbapi_error_maps_to_query_timeout(self) -> None:
        session = _FakeSession(exc=DBAPIError("stmt", {}, _QueryCanceledError("canceled")))
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock),
            patch.object(gr, "_rate_limited", return_value=False),
            pytest.raises(AnalyticsQueryTimeoutError, match="timeout"),
        ):
            await self._call(session)

    async def test_dbapi_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=DBAPIError("stmt", {}, ConnectionError("down")))
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock),
            patch.object(gr, "_rate_limited", return_value=False),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await self._call(session)

    async def test_sqlalchemy_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=SQLAlchemyError("boom"))
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock),
            patch.object(gr, "_rate_limited", return_value=False),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await self._call(session)

    async def test_unexpected_error_maps_to_database_error(self) -> None:
        session = _FakeSession(exc=RuntimeError("kaboom"))
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock),
            patch.object(gr, "_rate_limited", return_value=False),
            pytest.raises(AnalyticsDatabaseError, match="Database temporarily"),
        ):
            await self._call(session)

    async def test_cancelled_error_propagates_untouched(self) -> None:
        session = _FakeSession(exc=asyncio.CancelledError())
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock),
            patch.object(gr, "_rate_limited", return_value=False),
            pytest.raises(asyncio.CancelledError),
        ):
            await self._call(session)

    async def test_no_user_context_when_account_id_absent(self) -> None:
        session = _FakeSession(row_batches=_default_batches())
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch.object(gr, "set_rls_user_context", new_callable=AsyncMock) as mock_user,
            patch.object(gr, "_rate_limited", return_value=False),
        ):
            await gr.run_guardrail_scorecard(org_id=_ORG, factory=_factory(session), settings=_settings())
        mock_rls.assert_awaited_once()
        mock_user.assert_not_awaited()

    async def test_skips_set_config_preamble_on_non_postgres(self) -> None:
        session = _FakeSession(dialect="sqlite", row_batches=_default_batches())
        with (
            patch.object(gr, "set_rls_org", new_callable=AsyncMock),
            patch.object(gr, "set_rls_user_context", new_callable=AsyncMock),
            patch.object(gr, "_rate_limited", return_value=False),
        ):
            result = await self._call(session)
        assert result["scope"]["runs_with_guardrail"] == 4
        assert len(session.executed) == 4, "no timezone/statement_timeout set_configs on sqlite"
