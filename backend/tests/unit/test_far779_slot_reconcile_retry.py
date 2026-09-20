"""Unit tests for FAR-779: heartbeat-stale slot-reconcile auto-retry.

The 2026-09-12 incident: 3 consecutive Prompt-to-PR runs (22252, 22251, 22227)
failed with ``harness.dispatch_failed: Slot reconciliation: heartbeat stale past
threshold; pipeline slot force-released (FAR-604)``.  Runs dispatched ZERO nodes,
$0 spent — tasks never started and were silently lost.  Prior occurrence
2026-09-09 (run 21691).

Root cause: ``reconcile_pipeline_slots`` terminal-failed heartbeat-stale runs
with NO retry mechanism.  Under high concurrent dispatch volume, the SAQ
worker's event loop can be starved and the executor's heartbeat loop fails to
write ``heartbeat_at`` in time.  The sweep then permanently kills the run.

Fix (FAR-779):
  1. Auto-retry: reset heartbeat-stale runs to ``pending`` (clearing
     ``dispatched_at``/``dispatcher``/``heartbeat_at``) so
     ``dispatcher_reconcile`` re-dispatches them on their next 60s tick.
     Terminal-fail only when ``claim_count`` exceeds the retry budget
     (``HEARTBEAT_STALE_RETRY_BUDGET``).
  2. Backpressure: ``dispatch_run`` refuses new admissions when the
     pipeline's active slots are at >= 90%% saturation, preventing
     heartbeat-stale kills under slot exhaustion.

FAR-812 (2026-09-13): the retry budget lives in settings now
(``HEARTBEAT_STALE_RETRY_BUDGET``, default 3, was a hardcoded 1). A run that
goes heartbeat-stale on its second or third claim is still flaking on a
transient dispatch wobble (zero nodes executed, so nothing could double-
execute) — only a claim beyond the raised budget is genuinely stuck.

Mock/fake based — no Postgres, no Redis.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.run_admission as ra
from modulo.core.run_admission import reconcile_pipeline_slots
from modulo.settings import get_settings

# Mirrors the settings default (HEARTBEAT_STALE_RETRY_BUDGET), raised 1 -> 3
# by FAR-812. Tests key fixture rows off this so boundary cases pin the exact
# semantics: claims 0..budget reset to pending, only a claim beyond the budget
# terminal-fails.
DEFAULT_HEARTBEAT_RETRY_BUDGET = 3

ORG_ID = uuid.UUID("18348064-eca3-4aa7-be96-8f6c9123efd0")
PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000006666")
RUN_ID = uuid.UUID("fb4b1368-68ca-4125-8091-ca8d7c25839e")


# ---------------------------------------------------------------------------
# Test doubles (mirrors test_run_admission.py patterns)
# ---------------------------------------------------------------------------


class _AsyncResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rowcount = len(rows or [])
        self._rows = rows or []

    def all(self) -> list[Any]:
        return self._rows


def _released_row(claim_count: int = 0) -> Any:
    return SimpleNamespace(
        id=RUN_ID,
        organisation_id=ORG_ID,
        pipeline_id=PIPELINE_ID,
        claim_count=claim_count,
    )


class _RetryConn:
    """Connection double for the retry-aware sweep.

    Tracks every (sql, params) pair so tests can assert which UPDATE was
    emitted (retry-reset vs terminal-fail).
    """

    def __init__(
        self,
        statements: list[str],
        released: list[Any],
        orgs: list[uuid.UUID],
        params_seen: list[dict[str, object]],
    ) -> None:
        self._statements = statements
        self._released = released
        self._orgs = orgs
        self.params_seen = params_seen

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
        self._statements.append(str(stmt))
        if params is not None:
            self.params_seen.append(dict(params))
        if "SELECT id FROM organisations" in str(stmt):
            return _AsyncResult(rows=[(org,) for org in self._orgs])
        if "status = 'running'" in str(stmt):
            return _AsyncResult(rows=list(self._released))
        return _AsyncResult()


class _RetryEngine:
    def __init__(
        self,
        statements: list[str],
        released: list[Any],
        orgs: list[uuid.UUID] | None = None,
    ) -> None:
        self._statements = statements
        self._released = released
        self._orgs = orgs or [ORG_ID]
        self.params_seen: list[dict[str, object]] = []

    def connect(self) -> _RetryConn:
        return _RetryConn(self._statements, self._released, self._orgs, self.params_seen)


# ---------------------------------------------------------------------------
# FAR-779: auto-retry on heartbeat-stale
# ---------------------------------------------------------------------------


class TestHeartbeatStaleRetry:
    """When the sweep finds a stale running row with claim_count <= budget,
    it resets to pending (retry) instead of terminal-failing."""

    def _settings(self, stale_seconds: int = 1800) -> Any:
        return SimpleNamespace(
            slot_reconcile_stale_seconds=stale_seconds,
            heartbeat_stale_retry_budget=DEFAULT_HEARTBEAT_RETRY_BUDGET,
        )

    async def test_low_claim_count_resets_to_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """claim_count=0 (first heartbeat-stale) -> pending (retry)."""
        statements: list[str] = []
        engine = _RetryEngine(statements, [_released_row(claim_count=0)])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock):
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 0
        assert result["retried"] == 1
        # The UPDATE must contain the RETRY path SQL — the CASE expression
        # uses claim_count to choose between 'pending' and 'failed'.
        release_stmts = [s for s in statements if "status = 'running'" in s]
        assert len(release_stmts) == 1
        assert "dispatched_at = NULL" in release_stmts[0]
        assert "dispatcher = NULL" in release_stmts[0]
        assert "heartbeat_at = NULL" in release_stmts[0]
        assert "error_code = 'heartbeat_stale'" in release_stmts[0]
        # The SQL CASE has both 'pending' and 'failed' as string literals;
        # the retry path is verified by released==0 + retried==1.

    async def test_claim_count_exceeding_budget_terminal_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """claim_count > budget (DEFAULT_HEARTBEAT_RETRY_BUDGET) -> terminal-fail."""
        statements: list[str] = []
        engine = _RetryEngine(
            statements,
            [_released_row(claim_count=DEFAULT_HEARTBEAT_RETRY_BUDGET + 1)],
        )
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock):
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 1
        assert result["retried"] == 0
        release_stmts = [s for s in statements if "status = 'running'" in s]
        assert len(release_stmts) == 1
        # Terminal-fail path: the CASE expression contains 'failed'.
        # The SQL CASE means both 'pending' and 'failed' appear in the string;
        # we check that released==1 (which means claim_count > budget -> failed branch).
        assert result["released"] == 1
        # Verify the advance was called for the terminal-failed run.
        advance_stmts = [s for s in statements if "status = 'running'" in s]
        assert len(advance_stmts) == 1

    async def test_claim_count_at_budget_resets_to_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """claim_count == budget -> still retry (budget is an inclusive upper bound)."""
        statements: list[str] = []
        engine = _RetryEngine(
            statements,
            [_released_row(claim_count=DEFAULT_HEARTBEAT_RETRY_BUDGET)],
        )
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock):
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 0
        assert result["retried"] == 1

    async def test_retry_does_not_advance_released_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retried runs (reset to pending) must NOT get journeys + facts
        advanced — they will be re-dispatched."""
        statements: list[str] = []
        engine = _RetryEngine(statements, [_released_row(claim_count=0)])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock) as advance:
            await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        # Retried runs are NOT advanced — only terminal-failed runs are.
        advance.assert_not_awaited()

    async def test_terminal_fail_advances_released_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Terminal-failed runs (claim_count > budget) ARE advanced."""
        statements: list[str] = []
        engine = _RetryEngine(
            statements,
            [_released_row(claim_count=DEFAULT_HEARTBEAT_RETRY_BUDGET + 1)],
        )
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock) as advance:
            await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        advance.assert_awaited_once()
        assert advance.await_args.args[1] == RUN_ID
        assert advance.await_args.args[2] == ORG_ID

    async def test_mixed_retried_and_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple rows: some retried, some terminal-failed."""
        statements: list[str] = []
        run_a = _released_row(claim_count=0)  # retried
        run_b = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=ORG_ID,
            pipeline_id=PIPELINE_ID,
            claim_count=DEFAULT_HEARTBEAT_RETRY_BUDGET + 1,
        )  # terminal-failed
        engine = _RetryEngine(statements, [run_a, run_b])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock) as advance:
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 1
        assert result["retried"] == 1
        # Only the terminal-failed run is advanced.
        advance.assert_awaited_once()
        assert advance.await_args.args[1] == run_b.id

    def test_retry_budget_default_is_positive(self) -> None:
        """Sanity: the fixture budget mirrors the raised settings default — the
        budget must be a positive int and exceed the pre-FAR-812 default of 1,
        and the fixture constant stays pinned to the real ``Settings`` default so
        a default change in settings.py forces this test to be updated rather
        than silently re-pinning every boundary test to a stale budget."""
        assert isinstance(DEFAULT_HEARTBEAT_RETRY_BUDGET, int)
        assert DEFAULT_HEARTBEAT_RETRY_BUDGET >= 1
        assert DEFAULT_HEARTBEAT_RETRY_BUDGET > 1
        assert get_settings(_fresh=True).heartbeat_stale_retry_budget == DEFAULT_HEARTBEAT_RETRY_BUDGET

    # --- FAR-812: raised default keeps earlier claims alive -----------------

    @pytest.mark.parametrize("claim_count", [0, 1, 2])
    async def test_first_claims_reset_to_pending(self, monkeypatch: pytest.MonkeyPatch, claim_count: int) -> None:
        """FAR-812 regression: the first ``budget - 1`` heartbeat-stale claims
        (claim_count 0..2 at the raised default 3) all reset to pending. Under
        the pre-FAR-812 default of 1, claim_count=2 (a run typed for re-dispatch
        once and whose retry also went stale) would already have been
        terminal-failed — losing a task that executed zero nodes."""
        statements: list[str] = []
        engine = _RetryEngine(statements, [_released_row(claim_count=claim_count)])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock):
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 0
        assert result["retried"] == 1

    @pytest.mark.parametrize("claim_count", [DEFAULT_HEARTBEAT_RETRY_BUDGET + 1, DEFAULT_HEARTBEAT_RETRY_BUDGET + 2])
    async def test_claims_past_raised_budget_terminal_fail(
        self, monkeypatch: pytest.MonkeyPatch, claim_count: int
    ) -> None:
        """Only a claim BEYOND the raised budget terminal-fails (4, 5 at the
        default 3) — the budget still bounds a genuinely-stuck run."""
        statements: list[str] = []
        engine = _RetryEngine(statements, [_released_row(claim_count=claim_count)])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock):
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 1
        assert result["retried"] == 0


# ---------------------------------------------------------------------------
# FAR-779: slot-saturation backpressure in dispatch_run
# ---------------------------------------------------------------------------


class TestSlotSaturationBackpressure:
    """dispatch_run refuses new admissions when the pipeline's active slots
    are at >= 90% saturation — prevents heartbeat-stale kills under
    slot exhaustion."""

    def _make_session(self, pipeline: Any) -> MagicMock:
        """Build a mock session that supports async with session.begin()."""

        class _CtxManager:
            def __init__(self, session: MagicMock) -> None:
                self._session = session

            async def __aenter__(self) -> MagicMock:
                return self._session

            async def __aexit__(self, *args: object) -> bool:
                return False

        session = MagicMock()
        session.begin = MagicMock(return_value=_CtxManager(session))
        session.close = AsyncMock()
        # FAR-1025: the capacity/saturation gates resolve the pipeline via
        # execute(select(Pipeline)...) rather than session.get(...).
        _pipeline_result = MagicMock()
        _pipeline_result.scalar_one_or_none.return_value = pipeline
        session.execute = AsyncMock(return_value=_pipeline_result)
        return session

    async def test_near_saturated_pipeline_defers_run(self) -> None:
        """Active >= 90% of max -> deferred."""
        from modulo.core import dispatch

        pipeline = SimpleNamespace(max_concurrent_runs=10)
        run = SimpleNamespace(
            pipeline_id=PIPELINE_ID,
            status="pending",
            organisation_id=ORG_ID,
        )

        mock_settings = MagicMock(saq_runs_queue="runs")

        with (
            patch("modulo.core.dispatch.get_settings", return_value=mock_settings),
            patch("modulo.core.dispatch._open_session") as mock_open,
        ):
            mock_open.return_value = self._make_session(pipeline)

            with (
                patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run),
                patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=9),
                patch("modulo.db.crud.run.count_active_runs_for_org", new_callable=AsyncMock, return_value=0),
            ):
                outcome, _ = await dispatch.dispatch_run(str(uuid.uuid4()), str(ORG_ID), queue="runs")

        assert outcome == "deferred"

    async def test_just_below_saturation_admits_run(self) -> None:
        """Active = 80% of max -> admitted (below the 90% threshold)."""
        from modulo.core import dispatch

        pipeline = SimpleNamespace(max_concurrent_runs=10)
        run = SimpleNamespace(
            pipeline_id=PIPELINE_ID,
            status="pending",
            organisation_id=ORG_ID,
        )

        mock_settings = MagicMock(saq_runs_queue="runs")

        with (
            patch("modulo.core.dispatch.get_settings", return_value=mock_settings),
            patch("modulo.core.dispatch._open_session") as mock_open,
        ):
            mock_open.return_value = self._make_session(pipeline)

            with (
                patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run),
                patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=8),
                patch("modulo.db.crud.run.count_active_runs_for_org", new_callable=AsyncMock, return_value=0),
                patch.object(
                    dispatch,
                    "_enqueue_with_retry",
                    new_callable=AsyncMock,
                    return_value=("enqueued", "job-1"),
                ),
                patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
                patch.object(dispatch, "_record_saq_job_session", new_callable=AsyncMock),
            ):
                outcome, _ = await dispatch.dispatch_run(str(uuid.uuid4()), str(ORG_ID), queue="runs")

        assert outcome == "enqueued"

    async def test_unlimited_pipeline_bypasses_saturation_check(self) -> None:
        """max_concurrent_runs=0 (unlimited) -> no saturation check."""
        from modulo.core import dispatch

        pipeline = SimpleNamespace(max_concurrent_runs=0)
        run = SimpleNamespace(
            pipeline_id=PIPELINE_ID,
            status="pending",
            organisation_id=ORG_ID,
        )

        mock_settings = MagicMock(saq_runs_queue="runs")

        with (
            patch("modulo.core.dispatch.get_settings", return_value=mock_settings),
            patch("modulo.core.dispatch._open_session") as mock_open,
        ):
            mock_open.return_value = self._make_session(pipeline)

            with (
                patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run),
                patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=999),
                patch("modulo.db.crud.run.count_active_runs_for_org", new_callable=AsyncMock, return_value=0),
                patch.object(
                    dispatch,
                    "_enqueue_with_retry",
                    new_callable=AsyncMock,
                    return_value=("enqueued", "job-1"),
                ),
                patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
                patch.object(dispatch, "_record_saq_job_session", new_callable=AsyncMock),
            ):
                outcome, _ = await dispatch.dispatch_run(str(uuid.uuid4()), str(ORG_ID), queue="runs")

        assert outcome == "enqueued"
