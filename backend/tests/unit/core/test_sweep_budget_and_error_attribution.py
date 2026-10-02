"""Tests for FAR-904 (dispatcher_reconcile row budget + timeout attribution) and
FAR-905 (runner sweeps error attribution).

FAR-904: The dispatcher_reconcile sweep processes all orgs in a single tick
without a cross-org row budget.  When the per-org work is large, the tick
hits the inner deadline and times out with an empty TimeoutError message.
The fix adds a cumulative row budget (max_rows) and attaches budget/elapsed
context to the timeout error.

FAR-1425: That budget was only checked BETWEEN orgs, so on a single-org
deployment (the self-hosted default) it never bound and ``_reconcile_org``
fetched and processed every matching row while the timeout message still
advertised ``max_rows=500``.  The remainder of the budget is now handed to
each org, which LIMITs the row select and stops the per-row loop at it.

FAR-905: The runner_marker_sweep and runner_workspace_reconcile SAQ wrappers
persist ``"error": "sweep_failed"`` as a static string without the actual
exception details.  The fix embeds the exception type and message in the
error string so /healthz/ready is diagnostic.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> MagicMock:
    base: dict[str, Any] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "fernet_key": "b" * 44,
        "modulo_telemetry_enabled": False,
        "saq_reenqueue_window": 5,
        "saq_job_heartbeat": 30,
        "saq_claimed_nodeless_minutes": 15,
        "hitl_review_cancel_grace_seconds": 3600,
        "dispatcher_reconcile_budget_seconds": 95,
        "dispatcher_reconcile_terminalize_max_per_tick": 25,
        "dispatcher_reconcile_facts_max_per_tick": 25,
        "dispatcher_reconcile_max_rows_per_tick": 500,
    }
    base.update(overrides)
    return MagicMock(**base)


# ---------------------------------------------------------------------------
# FAR-904: row budget tests
# ---------------------------------------------------------------------------


class TestDispatcherReconcileRowBudget:
    """The row budget bounds cumulative rows across all orgs so the sweep
    always completes within the inner deadline."""

    def test_max_rows_setting_read(self) -> None:
        """The max_rows setting is read from Settings."""
        settings = _make_settings(dispatcher_reconcile_max_rows_per_tick=100)
        assert (
            ch._int_setting(
                getattr(settings, "dispatcher_reconcile_max_rows_per_tick", None),
                ch._RECONCILE_MAX_ROWS_DEFAULT,
            )
            == 100
        )

    def test_max_rows_default_fallback(self) -> None:
        """A missing setting falls back to the coded default."""
        settings = _make_settings()
        del settings.dispatcher_reconcile_max_rows_per_tick  # type: ignore[misc]
        assert (
            ch._int_setting(
                getattr(settings, "dispatcher_reconcile_max_rows_per_tick", None),
                ch._RECONCILE_MAX_ROWS_DEFAULT,
            )
            == ch._RECONCILE_MAX_ROWS_DEFAULT
        )

    @pytest.mark.asyncio
    async def test_budget_breaks_org_loop(self) -> None:
        """When rows_processed >= max_rows, remaining orgs are skipped."""
        settings = _make_settings(dispatcher_reconcile_max_rows_per_tick=5)
        summary = ch._dispatcher_summary()

        org_ids = [uuid.uuid4() for _ in range(5)]

        call_count = 0

        async def fake_reconcile_org(*args: Any, **kwargs: Any) -> int:
            nonlocal call_count
            call_count += 1
            # Simulate processing 3 rows per org
            summary["scanned"] += 3
            return 0

        tuning = ch.ReconcileTuning(
            nodeless_window=15,
            max_age_minutes=135,
            claim_cap=3,
            stale_window=90,
            capacity_redispatch_seconds=60,
            hitl_review_cancel_grace_seconds=3600,
        )
        with (
            patch.object(ch, "_collect_org_ids", new_callable=AsyncMock, return_value=org_ids),
            patch.object(ch, "_reconcile_org", side_effect=fake_reconcile_org),
            patch.object(ch, "reconciler_recovery_predicate"),
            patch.object(ch, "_open_system_factory"),
            patch("modulo.core.cron_helpers.AsyncRedis"),
        ):
            await ch._dispatcher_reconcile_body(
                _settings=settings,
                factory=MagicMock(),
                queue_name="runs",
                reenqueue_window=5,
                tuning=tuning,
                terminalize_max=25,
                facts_max=25,
                max_rows=5,
                redis_client=MagicMock(),
                summary=summary,
                terminalized_run_ids=[],
            )

        # First org processes 3 rows, second org processes 3 rows (total 6 >= 5).
        # Third org is skipped.
        assert call_count == 2
        assert summary["scanned"] == 6
        # rows_deferred counts the remaining orgs (3 orgs skipped)
        assert summary.get("rows_deferred", 0) == 3

    @pytest.mark.asyncio
    async def test_budget_not_exceeded_all_orgs_processed(self) -> None:
        """When rows_processed < max_rows, all orgs are processed."""
        settings = _make_settings(dispatcher_reconcile_max_rows_per_tick=100)
        summary = ch._dispatcher_summary()

        org_ids = [uuid.uuid4() for _ in range(3)]

        async def fake_reconcile_org(*args: Any, **kwargs: Any) -> int:
            summary["scanned"] += 2
            return 0

        tuning = ch.ReconcileTuning(
            nodeless_window=15,
            max_age_minutes=135,
            claim_cap=3,
            stale_window=90,
            capacity_redispatch_seconds=60,
            hitl_review_cancel_grace_seconds=3600,
        )
        with (
            patch.object(ch, "_collect_org_ids", new_callable=AsyncMock, return_value=org_ids),
            patch.object(ch, "_reconcile_org", side_effect=fake_reconcile_org),
            patch.object(ch, "reconciler_recovery_predicate"),
            patch.object(ch, "_open_system_factory"),
            patch("modulo.core.cron_helpers.AsyncRedis"),
        ):
            await ch._dispatcher_reconcile_body(
                _settings=settings,
                factory=MagicMock(),
                queue_name="runs",
                reenqueue_window=5,
                tuning=tuning,
                terminalize_max=25,
                facts_max=25,
                max_rows=100,
                redis_client=MagicMock(),
                summary=summary,
                terminalized_run_ids=[],
            )

        # All 3 orgs processed (6 rows total < 100 budget)
        assert summary["scanned"] == 6
        assert summary.get("rows_deferred", 0) == 0


# ---------------------------------------------------------------------------
# FAR-904: timeout error attribution
# ---------------------------------------------------------------------------


class TestDispatcherReconcileTimeoutAttribution:
    """The timeout error message includes budget and elapsed time context."""

    def test_timeout_error_includes_context(self) -> None:
        """When the inner deadline fires, last_error includes budget and elapsed."""
        summary = ch._dispatcher_summary()

        # Simulate the timeout handler from dispatcher_reconcile
        try:
            raise TimeoutError
        except TimeoutError:
            elapsed = 42.3  # Simulated
            budget = 95
            max_rows = 500
            summary["status"] = "timeout"
            summary["last_error"] = (
                f"TimeoutError: inner deadline fired after {elapsed:.1f}s (budget={budget}s, max_rows={max_rows})"
            )[:200]

        assert summary["status"] == "timeout"
        assert "TimeoutError:" in summary["last_error"]
        assert "42.3s" in summary["last_error"]
        assert "budget=95s" in summary["last_error"]
        assert "max_rows=500" in summary["last_error"]

    def test_bare_timeout_error_has_empty_message(self) -> None:
        """A bare asyncio.timeout TimeoutError has no message (the root cause
        of the unactionable health check detail)."""
        exc = TimeoutError()
        assert not str(exc)

    def test_summarize_reconcile_error_for_bare_timeout(self) -> None:
        """_summarize_reconcile_error produces 'TimeoutError: ' for a bare
        TimeoutError (no context)."""
        exc = TimeoutError()
        result = ch._summarize_reconcile_error(exc)
        assert result == "TimeoutError: "


# ---------------------------------------------------------------------------
# FAR-905: runner sweeps error attribution
# ---------------------------------------------------------------------------


class TestRunnerSweepsErrorAttribution:
    """The SAQ wrappers persist actual exception details in the error string."""

    @pytest.mark.asyncio
    async def test_marker_sweep_error_includes_exception(self) -> None:
        """runner_marker_sweep persists exception type + message in the error."""
        from modulo.core.runner_capacity import RunnerMarkerSweepError

        with (
            patch("modulo.core.saq_worker._make_session_factory", return_value=MagicMock()),
            patch(
                "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
                new_callable=AsyncMock,
                side_effect=RunnerMarkerSweepError(scanned=5, cleared=2, transitioned=1, org_failures=1),
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import runner_marker_sweep

            with pytest.raises(RunnerMarkerSweepError):
                await runner_marker_sweep({})

        # Verify the persisted error string includes exception details
        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        stats_blob = call_args[0][1]  # Second positional arg is the stats dict
        error_str = stats_blob["error"]
        assert "sweep_failed" in error_str
        assert "RunnerMarkerSweepError" in error_str
        assert "org_failures=1" in error_str

    @pytest.mark.asyncio
    async def test_workspace_reconcile_error_includes_exception(self) -> None:
        """runner_workspace_reconcile persists exception type + message."""
        from modulo.core.bundled_runner.runner_reconciler import ReconcilerSweepError

        with (
            patch("modulo.core.saq_worker._get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.bundled_runner.runner_reconciler.reconcile_runner_workspaces",
                new_callable=AsyncMock,
                side_effect=ReconcilerSweepError(
                    "Docker daemon unreachable",
                    scanned=3,
                    destroyed=0,
                ),
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import runner_workspace_reconcile

            with pytest.raises(ReconcilerSweepError):
                await runner_workspace_reconcile({})

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        stats_blob = call_args[0][1]
        error_str = stats_blob["error"]
        assert "sweep_failed" in error_str
        assert "ReconcilerSweepError" in error_str
        assert "Docker daemon unreachable" in error_str

    @pytest.mark.asyncio
    async def test_workspace_reconcile_engineless_skip_persists_clean_stats(self) -> None:
        """FAR-1201: the engine-less skip envelope flows through the wrapper as
        a NORMAL outcome — fresh stats, NO error key, no re-raise (so SAQ
        retries never engage), and the skip reason rides the job result."""
        skip_envelope = {
            "scanned": 0,
            "orphans_destroyed": 0,
            "skipped": "no Docker endpoint configured (MODULO_DOCKER_HOST unset)",
        }
        with (
            patch("modulo.core.saq_worker._get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.bundled_runner.runner_reconciler.reconcile_runner_workspaces",
                new_callable=AsyncMock,
                return_value=skip_envelope,
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import runner_workspace_reconcile

            result = await runner_workspace_reconcile({})  # no exception -> no SAQ retry

        mock_persist.assert_called_once()
        stats_blob = mock_persist.call_args[0][1]
        assert "error" not in stats_blob
        assert stats_blob["scanned"] == 0
        assert stats_blob["orphans_destroyed"] == 0
        assert result["skipped"].startswith("no Docker endpoint configured")

    @pytest.mark.asyncio
    async def test_slot_reconciliation_error_includes_exception(self) -> None:
        """slot_reconciliation persists exception type + message."""
        from modulo.core.run_admission import SlotReconciliationError

        with (
            patch("modulo.core.saq_worker._get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.run_admission.reconcile_pipeline_slots",
                new_callable=AsyncMock,
                side_effect=SlotReconciliationError("lock timeout", released=2, per_pipeline={}),
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import slot_reconciliation

            with pytest.raises(SlotReconciliationError):
                await slot_reconciliation({})

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        stats_blob = call_args[0][1]
        error_str = stats_blob["error"]
        assert "sweep_failed" in error_str
        assert "SlotReconciliationError" in error_str
        assert "lock timeout" in error_str

    @pytest.mark.asyncio
    async def test_hitl_park_error_includes_exception(self) -> None:
        """hitl_park_sweep persists exception type + message."""
        from modulo.core.run_admission import HitlParkError

        with (
            patch("modulo.core.saq_worker._get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.run_admission.park_expired_hitl_runs",
                new_callable=AsyncMock,
                side_effect=HitlParkError("gate expired", parked=1),
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import hitl_park_sweep

            with pytest.raises(HitlParkError):
                await hitl_park_sweep({})

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        stats_blob = call_args[0][1]
        error_str = stats_blob["error"]
        assert "sweep_failed" in error_str
        assert "HitlParkError" in error_str
        assert "gate expired" in error_str


# ---------------------------------------------------------------------------
# FAR-905: health check surfaces enriched error
# ---------------------------------------------------------------------------


class TestHealthCheckSurfacesEnrichedError:
    """The health check's _check_sweep_stats_advisory surfaces the enriched
    error string from the stats blob."""

    @pytest.mark.asyncio
    async def test_enriched_error_shown_in_detail(self) -> None:
        """When the stats blob carries an enriched error, the health check
        shows it in the detail string."""
        from modulo.api.routes.health import _check_sweep_stats_advisory

        enriched_error = "sweep_failed (RunnerMarkerSweepError: org_index failed)"
        stats = {
            "last_run_at": datetime.now(UTC).isoformat(),
            "scanned": 5,
            "cleared": 0,
            "error": enriched_error,
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=json.dumps(stats).encode())
        fake_redis.aclose = AsyncMock()

        with (
            patch("modulo.api.routes.health.get_settings", return_value=MagicMock(redis_url="redis://localhost")),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake_redis),
        ):
            result = await _check_sweep_stats_advisory(
                "saq:cron:stats:runner_marker_sweep",
                900,
                "cleared",
            )

        assert result.status == "degraded"
        assert result.detail is not None
        assert "RunnerMarkerSweepError" in result.detail
        assert "org_index failed" in result.detail

    @pytest.mark.asyncio
    async def test_legacy_sweep_failed_still_shown(self) -> None:
        """Older stats blobs with plain 'sweep_failed' still display."""
        from modulo.api.routes.health import _check_sweep_stats_advisory

        stats = {
            "last_run_at": datetime.now(UTC).isoformat(),
            "scanned": 0,
            "error": "sweep_failed",
        }
        fake_redis = MagicMock()
        fake_redis.get = AsyncMock(return_value=json.dumps(stats).encode())
        fake_redis.aclose = AsyncMock()

        with (
            patch("modulo.api.routes.health.get_settings", return_value=MagicMock(redis_url="redis://localhost")),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=fake_redis),
        ):
            result = await _check_sweep_stats_advisory(
                "saq:cron:stats:runner_workspace_reconcile",
                900,
                "orphans_destroyed",
            )

        assert result.status == "degraded"
        assert result.detail is not None
        assert "sweep_failed" in result.detail


# ---------------------------------------------------------------------------
# FAR-909: runner_health_probe error attribution
# ---------------------------------------------------------------------------


class TestRunnerHealthProbeErrorAttribution:
    """runner_health_probe persists actual exception details in the error string."""

    @pytest.mark.asyncio
    async def test_probe_error_includes_exception(self) -> None:
        """runner_health_probe persists exception type + message in the error."""
        with (
            patch("modulo.core.saq_worker._cleanup_session_factory", return_value=MagicMock()),
            patch(
                "modulo.core.bundled_runner.health_probe.run_runner_health_probe",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Docker socket unreachable"),
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import runner_health_probe

            with pytest.raises(RuntimeError):
                await runner_health_probe({})

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        stats_blob = call_args[0][1]
        error_str = stats_blob["error"]
        assert "probe_failed" in error_str
        assert "RuntimeError" in error_str
        assert "Docker socket unreachable" in error_str


# ---------------------------------------------------------------------------
# FAR-909: stale_run_recovery_sweep error attribution
# ---------------------------------------------------------------------------


class TestStaleRunRecoverySweepErrorAttribution:
    """stale_run_recovery_sweep returns enriched error details on failure."""

    @pytest.mark.asyncio
    async def test_sweep_error_includes_exception(self) -> None:
        """stale_run_recovery_sweep returns error with exception type + message."""
        with patch(
            "modulo.core.pipeline_execution.get_settings",
            return_value=MagicMock(
                saq_never_dispatched_window=300,
                saq_worker_lost_window=600,
                saq_capacity_timeout_ttl_minutes=30,
                saq_redis_pool_size=5,
                redis_url="redis://localhost",
            ),
        ):
            from modulo.core.pipeline_execution import stale_run_recovery_sweep

            # Build a proper async context manager that raises on __aenter__
            class _FailingConnector:
                async def __aenter__(self):
                    raise RuntimeError("DB connection lost")

                async def __aexit__(self, *args):
                    return False

            mock_engine = MagicMock()
            mock_engine.connect = MagicMock(return_value=_FailingConnector())

            result = await stale_run_recovery_sweep(mock_engine)

        assert isinstance(result, dict)
        assert "error" in result
        assert "sweep_failed" in result["error"]
        assert "RuntimeError" in result["error"]
        assert "DB connection lost" in result["error"]

    @pytest.mark.asyncio
    async def test_sweep_success_no_error_key(self) -> None:
        """stale_run_recovery_sweep returns counts without error on success."""
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_conn.execute = AsyncMock(return_value=mock_result)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_begin = MagicMock()
        mock_begin.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_begin.__aexit__ = AsyncMock(return_value=False)
        mock_conn.begin = MagicMock(return_value=mock_begin)

        fake_engine = MagicMock()
        fake_engine.connect = MagicMock(return_value=mock_conn)

        with patch(
            "modulo.core.pipeline_execution.get_settings",
            return_value=MagicMock(
                saq_never_dispatched_window=300,
                saq_worker_lost_window=600,
                saq_capacity_timeout_ttl_minutes=30,
            ),
        ):
            from modulo.core.pipeline_execution import stale_run_recovery_sweep

            result = await stale_run_recovery_sweep(fake_engine)

        assert isinstance(result, dict)
        assert "error" not in result


# ---------------------------------------------------------------------------
# FAR-909: stale_run_recovery wrapper surfaces error at top level
# ---------------------------------------------------------------------------


class TestStaleRunRecoveryWrapperErrorSurfacing:
    """The stale_run_recovery wrapper surfaces the sweep's error at the top
    level of the stats dict so the health check can read it."""

    @pytest.mark.asyncio
    async def test_wrapper_surfaces_error_at_top_level(self) -> None:
        """When the sweep returns an error dict, the wrapper persists the error
        at the top level (not nested inside 'recovered')."""
        sweep_result = {
            "never_dispatched_swept": 0,
            "worker_lost_swept": 0,
            "capacity_timeout_swept": 0,
            "stranded_capacity_redispatched": 0,
            "redispatch_outcomes": {},
            "error": "sweep_failed (RuntimeError: DB down)",
        }

        with (
            patch("modulo.core.saq_worker._get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.pipeline_execution.stale_run_recovery_sweep",
                new_callable=AsyncMock,
                return_value=sweep_result,
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import stale_run_recovery

            await stale_run_recovery({})

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        stats_blob = call_args[0][1]
        # Error must be at top level, not nested inside 'recovered'
        assert "error" in stats_blob
        assert "sweep_failed" in stats_blob["error"]
        assert "RuntimeError" in stats_blob["error"]
        assert stats_blob["recovered"] == 0

    @pytest.mark.asyncio
    async def test_wrapper_success_has_no_error(self) -> None:
        """On success the wrapper persists recovered count without error."""
        with (
            patch("modulo.core.saq_worker._get_async_engine", return_value=MagicMock()),
            patch(
                "modulo.core.pipeline_execution.stale_run_recovery_sweep",
                new_callable=AsyncMock,
                return_value=5,
            ),
            patch("modulo.core.saq_worker._persist_sweep_stats", new_callable=AsyncMock) as mock_persist,
        ):
            from modulo.core.saq_worker import stale_run_recovery

            await stale_run_recovery({})

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        stats_blob = call_args[0][1]
        assert "error" not in stats_blob
        assert stats_blob["recovered"] == 5


# ---------------------------------------------------------------------------
# FAR-909: library_sync error attribution
# ---------------------------------------------------------------------------


class TestLibrarySyncErrorAttribution:
    """library_sync persists actual exception details in the error string."""

    @pytest.mark.asyncio
    async def test_library_sync_error_includes_exception(self) -> None:
        """library_sync returns error with exception type + message when
        sync_library raises (not returns a failure SyncResult)."""
        from modulo.core.saq_worker import library_sync

        mock_settings = MagicMock()
        mock_settings.modulo_library_endpoint = "https://library.example.com"
        with (
            patch("modulo.core.saq_worker.get_settings", return_value=mock_settings),
            patch("modulo.core.saq_worker._make_session_factory", return_value=MagicMock()),
            patch(
                "modulo.core.library_sync.sync_library",
                new_callable=AsyncMock,
                side_effect=ConnectionError("Network unreachable"),
            ),
        ):
            result = await library_sync({})

        assert result["status"] == "failed"
        assert "unexpected cron failure" in result["error"]
        assert "ConnectionError" in result["error"]
        assert "Network unreachable" in result["error"]


# ---------------------------------------------------------------------------
# FAR-1425: the row budget must be RESPECTED by a single-org sweep
# ---------------------------------------------------------------------------


class _BudgetSession:
    """Session double for the ``_reconcile_org`` row-budget tests.

    With the batch terminalizer registry emptied the row select is the ONLY
    statement executed, so every recorded statement can be treated as it.
    """

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: Any, params: Any = None) -> MagicMock:
        self.statements.append(stmt)
        result = MagicMock()
        result.all.return_value = list(self.rows)
        return result


def _budget_tuning() -> ch.ReconcileTuning:
    return ch.ReconcileTuning(
        nodeless_window=15,
        max_age_minutes=135,
        claim_cap=3,
        stale_window=90,
        capacity_redispatch_seconds=60,
        hitl_review_cancel_grace_seconds=3600,
    )


async def _run_org(rows: list[Any], row_budget: int | None) -> tuple[dict[str, Any], _BudgetSession, Any]:
    """Drive the REAL ``_reconcile_org`` with *row_budget* applied."""
    session = _BudgetSession(rows)
    summary: dict[str, Any] = {"scanned": 0}
    with (
        patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        patch.object(ch, "_BATCH_TERMINALIZER_SPECS", ()),
        patch.object(ch, "_reconcile_one_row", new_callable=AsyncMock, return_value=0) as one_row,
    ):
        await ch._reconcile_org(
            factory=MagicMock(return_value=session),
            q=MagicMock(),
            redis_client=MagicMock(),
            org_id=uuid.uuid4(),
            re_dispatch_predicate=ch.text("1 = 1"),
            tuning=_budget_tuning(),
            enqueue_failed_redispatched=0,
            summary=summary,
            terminalized_run_ids=[],
            row_budget=row_budget,
        )
    return summary, session, one_row


class TestSingleOrgRowBudget:
    """FAR-1425: ``max_rows`` is advertised in the inner-deadline message, so
    it must bound ONE org's scan — the inter-org gate alone never binds on a
    single-org deployment (the self-hosted default, and prod's shape)."""

    @pytest.mark.asyncio
    async def test_row_select_is_limited_to_the_budget(self) -> None:
        """The fetch itself is bounded — the two correlated NOT EXISTS legs
        are SELECT-list expressions evaluated once per FETCHED row."""
        _summary, session, _one_row = await _run_org([object() for _ in range(10)], row_budget=3)
        assert len(session.statements) == 1
        assert "LIMIT" in str(session.statements[0])

    @pytest.mark.asyncio
    async def test_row_loop_never_exceeds_the_budget(self) -> None:
        """A single org processes at most ``row_budget`` rows even when the
        session hands back more, and reports the capped org as deferred."""
        summary, _session, one_row = await _run_org([object() for _ in range(10)], row_budget=3)
        assert one_row.await_count == 3
        assert summary["scanned"] == 3
        assert summary.get("rows_deferred", 0) == 1

    @pytest.mark.asyncio
    async def test_under_budget_processes_every_row_and_defers_nothing(self) -> None:
        """The budget bounds the scan; it does not truncate work that fits."""
        summary, session, one_row = await _run_org([object() for _ in range(2)], row_budget=5)
        assert one_row.await_count == 2
        assert summary["scanned"] == 2
        assert summary.get("rows_deferred", 0) == 0
        assert "LIMIT" in str(session.statements[0])

    @pytest.mark.asyncio
    async def test_direct_call_without_a_budget_stays_unbounded(self) -> None:
        """``row_budget=None`` is the pre-FAR-1425 contract for direct
        callers — no LIMIT, no defer accounting."""
        summary, session, one_row = await _run_org([object() for _ in range(7)], row_budget=None)
        assert one_row.await_count == 7
        assert summary["scanned"] == 7
        assert summary.get("rows_deferred", 0) == 0
        assert "LIMIT" not in str(session.statements[0])

    @pytest.mark.asyncio
    async def test_single_org_receives_the_whole_budget(self) -> None:
        """The tick hands the ONE org ``max_rows`` itself, rather than only
        gating at the (never reached) inter-org boundary."""
        summary = ch._dispatcher_summary()
        captured: list[int] = []

        async def fake_reconcile_org(*args: Any, **kwargs: Any) -> int:
            captured.append(int(kwargs["row_budget"]))
            summary["scanned"] += 7
            return 0

        with (
            patch.object(ch, "_collect_org_ids", new_callable=AsyncMock, return_value=[uuid.uuid4()]),
            patch.object(ch, "_reconcile_org", side_effect=fake_reconcile_org),
            patch.object(ch, "reconciler_recovery_predicate"),
            patch.object(ch, "_open_system_factory"),
            patch("modulo.core.cron_helpers.AsyncRedis"),
        ):
            await ch._dispatcher_reconcile_body(
                _settings=_make_settings(dispatcher_reconcile_max_rows_per_tick=500),
                factory=MagicMock(),
                queue_name="runs",
                reenqueue_window=5,
                tuning=_budget_tuning(),
                terminalize_max=25,
                facts_max=25,
                max_rows=500,
                redis_client=MagicMock(),
                summary=summary,
                terminalized_run_ids=[],
            )

        assert captured == [500]
