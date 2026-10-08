"""FAR-1601 — bounded row-lock waits on the executor-path ``runs`` writers.

``pipeline_execution``'s hot-``runs`` writers (claim / resume claim /
heartbeat / ``mark_complete`` / ``fail_run_terminal`` / the node-deadline
watchdog-firing counter / the stale-run recovery sweep) used to wait
UNBOUNDED on a contended ``runs`` row lock — the silent-wait class that let
FAR-1524's prod sessions reach the Fly HAProxy 30-minute cull window. They
now issue the shared transaction-scoped ``SET LOCAL lock_timeout``
(``Settings.mutation_row_lock_timeout_ms`` via
``db.crud.row_lock.set_mutation_row_lock_timeout``) at the top of their
transaction, exactly like the dispatch writers (FAR-1584 / FAR-1592).

These tests pin, per writer:

1. **Wiring + ORDER** (fail-first): the bound statement is issued BEFORE the
   writer's ``UPDATE runs`` takes its row lock, carries ``is_local => true``
   (transaction-scoped, never leaks onto a pooled connection), and its value
   comes from the operator knob — a postgresql-reporting connection double
   takes the helper's LIVE branch, so the real bound statement is recorded.
   Without the source change no bound statement exists and every ordering
   assertion fails.
2. **The 55P03 contract**, chosen per path: idempotent/recovery writers skip
   + WARN + re-process (claim, resume claim, heartbeat beat, mark_complete);
   the best-effort watchdog counter skips + WARN (its existing fail-soft);
   the terminal-fail writer and the periodic sweep PROPAGATE so the failure
   is visible (SAQ job failure / sweep-failure alerting), with
   reconciliation as the designed backstop.
3. **The phase writer is already bounded** by the 2 s app-level
   ``PHASE_WRITE_TIMEOUT_SECONDS`` — deliberately NOT given the DB bound
   (2 s < the 5 s default knob, so it could never fire first).

The real-Postgres contention behaviour (a held row lock actually timing out)
is an integration concern; the unit seam here drives the same 55P03 the bound
produces.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

import modulo.core.pipeline_execution as pe
from modulo.core.pipeline_execution import (
    _record_node_deadline_watchdog_firing,
    claim_resume_run_async,
    claim_run_async,
    fail_run_terminal,
    heartbeat_once,
    mark_complete,
    stale_run_recovery_sweep,
)

_LOGGER = "modulo.core.pipeline_execution"


def _lock_timeout_error(statement: str) -> OperationalError:
    """Simulated row-lock contention: asyncpg's real ``LockNotAvailableError``
    (SQLSTATE 55P03) wrapped exactly the way SQLAlchemy surfaces it."""
    from asyncpg import exceptions as asyncpg_exceptions

    driver_error = asyncpg_exceptions.LockNotAvailableError("canceling statement due to lock timeout")
    return OperationalError(statement, {}, driver_error)


class _PgResult:
    def __init__(self, row: tuple[Any, ...] | None = None, *, rowcount: int = 0, rows: list[Any] | None = None) -> None:
        self._row = row
        self.rowcount = rowcount
        self._rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def all(self) -> list[Any]:
        return self._rows


class _PgRecordingConn:
    """Async connection double reporting the postgresql dialect, recording SQL.

    Takes the LIVE branch of ``set_mutation_row_lock_timeout``'s dialect gate
    (``get_bind().dialect.name == "postgresql"``), so the real bound statement
    is recorded alongside each writer's UPDATE for ORDER/value assertions.
    ``raise_on_contains`` raises a 55P03 lock-timeout error when the named
    statement fragment is executed (the bound statement itself never matches).
    """

    def __init__(
        self,
        *,
        claim_row: tuple[Any, ...] | None = ("id",),
        org_rows: list[Any] | None = None,
        stranded_row: Any = None,
        raise_on_contains: str | None = None,
    ) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any] | None] = []
        self._claim_row = claim_row
        self._org_rows = org_rows or []
        self._stranded_row = stranded_row
        self._raise_on_contains = raise_on_contains

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def commit(self) -> None:
        return None

    async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> _PgResult:
        sql = str(stmt)
        self.statements.append(sql)
        self.params.append(params)
        if self._raise_on_contains is not None and self._raise_on_contains in sql:
            raise _lock_timeout_error(sql)
        if "SELECT id FROM organisations" in sql:
            return _PgResult(rows=self._org_rows)
        if "SELECT claim_count FROM runs" in sql:
            return _PgResult(row=(0,))
        return _PgResult(row=self._claim_row, rowcount=1, rows=[self._stranded_row] if self._stranded_row else [])


class _PgEngine:
    def __init__(self, conn: _PgRecordingConn) -> None:
        self.conn = conn

    def connect(self) -> _PgRecordingConn:
        return self.conn


def _bound_statements(conn: _PgRecordingConn) -> list[int]:
    return [i for i, sql in enumerate(conn.statements) if "set_config('lock_timeout'" in sql]


def _update_statements(conn: _PgRecordingConn, marker: str) -> list[int]:
    return [i for i, sql in enumerate(conn.statements) if marker in sql]


def _assert_bound_precedes_update(conn: _PgRecordingConn, update_marker: str) -> int:
    """Shared wiring assertion: the bound fires first, locally, at knob value."""
    bound_at = _bound_statements(conn)
    update_at = _update_statements(conn, update_marker)
    assert bound_at, f"no transaction-local lock_timeout bound issued; statements={conn.statements}"
    assert update_at, f"the writer's UPDATE never ran; statements={conn.statements}"
    assert bound_at[0] < update_at[0], (
        f"the bound must be set BEFORE the row lock (lock_timeout at {bound_at[0]}, UPDATE at {update_at[0]})"
    )
    # SET LOCAL semantics: set_config(..., is_local => true) — the bound is
    # transaction-scoped and reverts on COMMIT/ROLLBACK.
    assert ", true)" in conn.statements[bound_at[0]]
    # The value comes from the operator knob, not a hardcoded literal.
    bound_params = conn.params[bound_at[0]]
    assert bound_params is not None
    assert bound_params["val"] == "5000ms"
    return bound_at[0]


def _patched_row_lock_settings() -> Any:
    return patch(
        "modulo.db.crud.row_lock.get_settings",
        return_value=MagicMock(mutation_row_lock_timeout_ms=5000),
    )


# ---------------------------------------------------------------------------
# Wiring + ORDER — every writer issues the bound before its row lock
# ---------------------------------------------------------------------------


class TestWritersBoundTheirRowLockWait:
    async def test_claim_run_async_bounds_before_the_claim_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _PgRecordingConn()
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))
        with patch.object(pe, "_maybe_alert_retry_storm", new=AsyncMock()), _patched_row_lock_settings():
            token = await claim_run_async(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        assert token is not None
        _assert_bound_precedes_update(conn, "UPDATE runs SET status='running'")

    async def test_claim_resume_run_async_bounds_before_the_claim_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _PgRecordingConn()
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))
        with _patched_row_lock_settings():
            token = await claim_resume_run_async(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        assert token is not None
        _assert_bound_precedes_update(conn, "UPDATE runs SET status='running'")

    async def test_heartbeat_once_bounds_before_the_heartbeat_update(self) -> None:
        conn = _PgRecordingConn()
        with _patched_row_lock_settings():
            await heartbeat_once(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        _assert_bound_precedes_update(conn, "UPDATE runs SET heartbeat_at=now()")

    async def test_mark_complete_bounds_before_the_complete_update(self) -> None:
        conn = _PgRecordingConn()
        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock),
            _patched_row_lock_settings(),
        ):
            await mark_complete(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        _assert_bound_precedes_update(conn, "UPDATE runs SET status='complete'")

    async def test_fail_run_terminal_bounds_before_the_failed_update(self) -> None:
        conn = _PgRecordingConn()
        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock),
            patch.object(pe, "_record_fact_for_terminal_failed_run", new_callable=AsyncMock),
            _patched_row_lock_settings(),
        ):
            ok = await fail_run_terminal(  # type: ignore[arg-type]
                _PgEngine(conn),
                "run-1",
                "org-1",
                error_code="executor_stalled",
                error_detail="boom",
            )
        assert ok is True
        _assert_bound_precedes_update(conn, "UPDATE runs SET status='failed'")

    async def test_watchdog_firing_counter_bounds_before_its_update(self) -> None:
        conn = _PgRecordingConn()
        with _patched_row_lock_settings():
            await _record_node_deadline_watchdog_firing(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        _assert_bound_precedes_update(
            conn, "node_deadline_watchdog_fired_count = node_deadline_watchdog_fired_count + 1"
        )

    async def test_stale_run_recovery_sweep_bounds_before_its_first_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        org_id = uuid.uuid4()
        conn = _PgRecordingConn(org_rows=[(org_id,)])
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock())
        with _patched_row_lock_settings():
            result = await stale_run_recovery_sweep(_PgEngine(conn))  # type: ignore[arg-type]
        assert "error" not in result
        _assert_bound_precedes_update(conn, "never_dispatched")


# ---------------------------------------------------------------------------
# 55P03 contracts — non-silent, path-appropriate
# ---------------------------------------------------------------------------


class TestLockTimeoutHandling:
    async def test_claim_lock_timeout_returns_none_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The claim 55P03s → returns None (the existing not-claimable outcome),
        leaving the run claimable for dispatcher_reconcile; WARNING, never silent."""
        conn = _PgRecordingConn(raise_on_contains="UPDATE runs SET status='running'")
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))
        with (
            patch.object(pe, "_maybe_alert_retry_storm", new=AsyncMock()),
            caplog.at_level("WARNING", logger=_LOGGER),
            _patched_row_lock_settings(),
        ):
            token = await claim_run_async(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        assert token is None
        assert any("claim_lock_timeout" in message for message in caplog.messages)
        # sanity: the bound DID precede the failing UPDATE (the 55P03 came from it)
        _assert_bound_precedes_update(conn, "UPDATE runs SET status='running'")

    async def test_claim_resume_lock_timeout_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conn = _PgRecordingConn(raise_on_contains="UPDATE runs SET status='running'")
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock(run_claim_stale_seconds=450, saq_run_claim_cap=20))
        with caplog.at_level("WARNING", logger=_LOGGER), _patched_row_lock_settings():
            token = await claim_resume_run_async(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        assert token is None
        assert any("resume_claim_lock_timeout" in message for message in caplog.messages)

    async def test_heartbeat_once_propagates_lock_timeout(self) -> None:
        """The heartbeat writer stays DUMB (FAR-1584 style): 55P03 propagates
        to ``_heartbeat_round``, which owns the contract."""
        conn = _PgRecordingConn(raise_on_contains="UPDATE runs SET heartbeat_at=now()")
        with _patched_row_lock_settings(), pytest.raises(OperationalError):
            await heartbeat_once(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]

    async def test_heartbeat_round_skips_the_beat_without_counting_a_strike(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A 55P03 beat is contention, not a health failure: the round keeps
        going with the consecutive-failure counter UNCHANGED (never the
        3-strikes fail-closed), and says so at WARNING."""
        with (
            patch.object(pe, "heartbeat_once", new=AsyncMock(side_effect=_lock_timeout_error("UPDATE runs"))),
            caplog.at_level("WARNING", logger=_LOGGER),
        ):
            keep, failures = await pe._heartbeat_round(
                MagicMock(),  # type: ignore[arg-type]
                "run-1",
                "org-1",
                interval_seconds=0,
                job=None,
                claim_token="tok-a",
                superseded=None,
                health_failed=None,
                consecutive_failures=2,
            )
        assert keep is True
        assert failures == 2  # NOT counted toward the fail-closed threshold
        assert any("heartbeat_lock_timeout" in message for message in caplog.messages)

    async def test_heartbeat_round_generic_failure_still_counts(self) -> None:
        """Non-lock DB failures keep the pre-existing fail-closed contract."""
        with patch.object(pe, "heartbeat_once", new=AsyncMock(side_effect=RuntimeError("db down"))):
            keep, failures = await pe._heartbeat_round(
                MagicMock(),  # type: ignore[arg-type]
                "run-1",
                "org-1",
                interval_seconds=0,
                job=None,
                claim_token="tok-a",
                superseded=None,
                health_failed=None,
                consecutive_failures=0,
            )
        assert keep is True
        assert failures == 1

    async def test_mark_complete_lock_timeout_skips_without_advancing(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A 55P03 completion write is SKIPPED (run left ``running`` for the
        reconcile re-dispatch) and must never advance journeys for a write
        that never committed — and never raise into the SAQ job (whose
        after_process would task_failure a run that genuinely completed)."""
        conn = _PgRecordingConn(raise_on_contains="UPDATE runs SET status='complete'")
        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock) as advance,
            caplog.at_level("WARNING", logger=_LOGGER),
            _patched_row_lock_settings(),
        ):
            await mark_complete(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        advance.assert_not_awaited()
        assert any("mark_complete lock timeout" in message for message in caplog.messages)

    async def test_mark_complete_non_lock_failure_still_propagates(self) -> None:
        """Only 55P03 is handled; any other DB failure keeps today's visible
        job-failure path."""
        conn = _PgRecordingConn()
        original_execute = conn.execute

        async def _execute(stmt: object, params: dict[str, Any] | None = None) -> _PgResult:
            if "UPDATE runs SET status='complete'" in str(stmt):
                raise RuntimeError("connection reset")
            return await original_execute(stmt, params)

        conn.execute = _execute  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="connection reset"):
            await mark_complete(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]

    async def test_fail_run_terminal_lock_timeout_propagates(self) -> None:
        """The terminal-fail writer FAILS VISIBLY on 55P03 — never a silent
        skip (a skipped terminal-fail would leave the run ``running`` with no
        error code while the caller believes it classified)."""
        conn = _PgRecordingConn(raise_on_contains="UPDATE runs SET status='failed'")
        with _patched_row_lock_settings(), pytest.raises(OperationalError):
            await fail_run_terminal(  # type: ignore[arg-type]
                _PgEngine(conn),
                "run-1",
                "org-1",
                error_code="executor_stalled",
                error_detail="boom",
            )

    async def test_fail_run_terminal_retry_wrapper_retries_a_lock_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """55P03's driver class (``LockNotAvailableError``) is in the transient
        vocabulary, so the heartbeat-loss path's bounded retry rides out a
        momentary contention."""
        monkeypatch.setattr(pe, "_TERMINAL_WRITE_RETRY_BACKOFF_SECONDS", 0.0)
        fail = AsyncMock(side_effect=[_lock_timeout_error("UPDATE runs"), True])
        with patch.object(pe, "fail_run_terminal", fail):
            ok = await pe._fail_run_terminal_with_retry(
                MagicMock(),  # type: ignore[arg-type]
                "run-1",
                "org-1",
                error_code="executor_heartbeat_lost",
                error_detail="boom",
            )
        assert ok is True
        assert fail.await_count == 2

    async def test_watchdog_firing_counter_lock_timeout_is_fail_soft(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Best-effort counter: 55P03 lands in the existing fail-soft WARNING —
        the watchdog still reaches its terminal write."""
        conn = _PgRecordingConn(
            raise_on_contains="node_deadline_watchdog_fired_count = node_deadline_watchdog_fired_count + 1",
        )
        with caplog.at_level("WARNING", logger=_LOGGER), _patched_row_lock_settings():
            await _record_node_deadline_watchdog_firing(_PgEngine(conn), "run-1", "org-1")  # type: ignore[arg-type]
        assert any("watchdog_firing_record_failed" in message for message in caplog.messages)

    async def test_stale_run_recovery_sweep_lock_timeout_fails_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The periodic sweep PROPAGATES the 55P03 into its existing failure
        handler: the org transaction rolls back, the failure is logged and
        returned as ``error: sweep_failed`` (the health-alert sweeps key on
        sweep FAILURES), and the next tick re-runs the same UPDATE set."""
        org_id = uuid.uuid4()
        conn = _PgRecordingConn(org_rows=[(org_id,)], raise_on_contains="never_dispatched")
        monkeypatch.setattr(pe, "get_settings", lambda: MagicMock())
        with caplog.at_level("ERROR", logger=_LOGGER), _patched_row_lock_settings():
            result = await stale_run_recovery_sweep(_PgEngine(conn))  # type: ignore[arg-type]
        assert "error" in result
        assert str(result["error"]).startswith("sweep_failed")
        assert result["never_dispatched_swept"] == 0
        assert any("Stale run recovery sweep failed" in message for message in caplog.messages)


# ---------------------------------------------------------------------------
# The phase writer is already bounded — deliberately N/A (FAR-1601)
# ---------------------------------------------------------------------------


class TestPhaseWriterAlreadyBounded:
    def test_app_level_bound_is_tighter_than_the_default_db_knob(self) -> None:
        """``DispatchPhaseWriter`` is bounded by
        ``PHASE_WRITE_TIMEOUT_SECONDS`` (2 s) — tighter than the DEFAULT
        ``mutation_row_lock_timeout_ms`` (5 s), so the DB-side bound could
        never fire first; a 55P03 from a lowered knob lands in the writer's
        existing fail-soft handler (WARNING + dropped). No DB bound added."""
        from modulo.settings import Settings

        default_ms = Settings.model_fields["mutation_row_lock_timeout_ms"].default
        assert default_ms is not None
        assert default_ms > pe.PHASE_WRITE_TIMEOUT_SECONDS * 1000
        assert pe.PHASE_WRITE_TIMEOUT_SECONDS == 2.0
